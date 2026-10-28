"""Tests for the ShrinkMe resolver and the Turnstile solver.

The resolver's job is narrow and fully testable offline:

* detect ShrinkMe-family hosts,
* replay ``form#link-view`` verbatim (a partial payload is rejected by the
  site before the captcha is even inspected),
* read the rotating Turnstile sitekey from the page instead of hardcoding it,
* post the solved token as ``cf-turnstile-response``,
* follow the shortlink's own redirect chain until it leaves the host.

What cannot be tested offline is the captcha itself -- that is a human worker
answering a Cloudflare challenge. Everything around it is covered here.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# A landing page trimmed to the parts the resolver reads. The sitekey lives in
# the inline settings blob, exactly as the live page serves it.
LANDING_HTML = """
<html><body>
<script>
  var siteConfig = {"enable_captcha":"yes","captcha_type":"turnstile",
                    "turnstile_site_key":"0x4AAAAAAA_test_key"};
</script>
<form method="post" accept-charset="utf-8" id="link-view" action="/abc123">
  <input type="hidden" name="_method" value="POST"/>
  <input type="hidden" name="_csrfToken" value="csrf-value-abc"/>
  <input type="hidden" name="action" value="captcha"/>
  <input type="hidden" name="f_n" value="slc"/>
  <input type="hidden" name="_Token[fields]" value="fields-value-xyz"/>
  <input type="hidden" name="_Token[unlocked]"
         value="adcopy_challenge%7Cadcopy_response%7Ccf-turnstile-response"/>
  <button class="btn btn-primary btn-captcha" id="invisibleCaptchaShortlink"
          type="submit">Click here to continue</button>
</form>
</body></html>
"""


@pytest.fixture
def resolver(monkeypatch):
    """Import the resolver with ``bot`` package stubs, like the other tests."""
    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(*a, **k):
            pass

    bot_pkg.LOGGER = _Logger()

    config_pkg = ModuleType("bot.core")
    config_pkg.__path__ = []
    config_manager = ModuleType("bot.core.config_manager")

    class Config:
        ANTICAPTCHA_KEY = "test-key"

    config_manager.Config = Config

    helper_pkg = ModuleType("bot.helper")
    helper_pkg.__path__ = []
    ext_utils_pkg = ModuleType("bot.helper.ext_utils")
    ext_utils_pkg.__path__ = []
    exceptions_mod = ModuleType("bot.helper.ext_utils.exceptions")

    class DirectDownloadLinkException(Exception):
        pass

    exceptions_mod.DirectDownloadLinkException = DirectDownloadLinkException

    mlu_pkg = ModuleType("bot.helper.mirror_leech_utils")
    mlu_pkg.__path__ = []
    du_pkg = ModuleType("bot.helper.mirror_leech_utils.download_utils")
    du_pkg.__path__ = [str(PROJECT_ROOT / "bot" / "helper" / "mirror_leech_utils"
                           / "download_utils")]

    for name, mod in [
        ("bot", bot_pkg),
        ("bot.core", config_pkg),
        ("bot.core.config_manager", config_manager),
        ("bot.helper", helper_pkg),
        ("bot.helper.ext_utils", ext_utils_pkg),
        ("bot.helper.ext_utils.exceptions", exceptions_mod),
        ("bot.helper.mirror_leech_utils", mlu_pkg),
        ("bot.helper.mirror_leech_utils.download_utils", du_pkg),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    modname = "bot.helper.mirror_leech_utils.download_utils.shrinkme_resolver"
    acname = "bot.helper.mirror_leech_utils.download_utils.anticaptcha_turnstile"
    sys.modules.pop(modname, None)
    sys.modules.pop(acname, None)
    ac_mod = importlib.import_module(acname)
    mod = importlib.import_module(modname)
    # ``solver_configured`` and ``solve_turnstile`` read the key from the
    # anticaptcha module's own ``Config`` binding, so tests must reach it there
    # rather than through the resolver's namespace.
    mod.anticaptcha = ac_mod
    return mod


# ------------------------------------------------------------------ detection
@pytest.mark.parametrize(
    "domain,expected",
    [
        ("shrinkme.click", True),
        ("shrinkme.io", True),
        ("shrinkme.cc", True),
        ("shrinke.me", True),
        ("shrinkearn.com", True),
        ("shrinkforearn.in", True),
        ("example.com", False),
        ("", False),
        ("notshrinkme.click.evil.com", True),  # substring match is intentional
    ],
)
def test_is_shrinkme(resolver, domain, expected):
    assert resolver.is_shrinkme(domain) is expected


# --------------------------------------------------------------- form parsing
def test_parse_form_collects_every_hidden_field(resolver):
    fields = resolver._parse_form(LANDING_HTML)
    assert fields["_method"] == "POST"
    assert fields["_csrfToken"] == "csrf-value-abc"
    assert fields["action"] == "captcha"
    assert fields["f_n"] == "slc"
    assert fields["_Token[fields]"] == "fields-value-xyz"
    assert "_Token[unlocked]" in fields


def test_parse_form_skips_submit_buttons(resolver):
    """A <button> must not appear as a field; only inputs are replayed."""
    fields = resolver._parse_form(LANDING_HTML)
    assert "invisibleCaptchaShortlink" not in fields


def test_parse_form_raises_without_form(resolver):
    with pytest.raises(resolver.DirectDownloadLinkException) as e:
        resolver._parse_form("<html><body>no form here</body></html>")
    assert "link-view" in str(e.value)


# ------------------------------------------------------------------- sitekey
def test_sitekey_read_from_inline_settings(resolver):
    assert resolver._sitekey(LANDING_HTML) == "0x4AAAAAAA_test_key"


def test_sitekey_read_from_data_attribute(resolver):
    html = '<div class="cf-turnstile" data-sitekey="0xFROMDIV"></div>'
    assert resolver._sitekey(html) == "0xFROMDIV"


def test_sitekey_missing_raises(resolver):
    with pytest.raises(resolver.DirectDownloadLinkException) as e:
        resolver._sitekey("<html><body>nothing</body></html>")
    assert "sitekey" in str(e.value)


# ------------------------------------------------------------------- solver
def test_solver_reports_missing_key(resolver):
    resolver.anticaptcha.Config.ANTICAPTCHA_KEY = ""
    try:
        assert resolver.solver_configured() is False
    finally:
        resolver.anticaptcha.Config.ANTICAPTCHA_KEY = "test-key"


def test_solver_configured_true_with_key(resolver):
    assert resolver.solver_configured() is True


# ------------------------------------------------------------- end-to-end flow
class _FakeResponse:
    def __init__(self, status=200, text="", headers=None, url="https://x/"):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url


class _FakeSession:
    """Records what the resolver posts so the payload can be asserted."""

    def __init__(self, landing, submit, hops=None):
        self._landing = landing
        self._submit = submit
        self._hops = hops or {}
        self.posted = None
        self.post_url = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, allow_redirects=True):
        if url in self._hops:
            return self._hops[url]
        return self._landing

    def post(self, url, data=None, headers=None, allow_redirects=False):
        self.post_url = url
        self.posted = dict(data or {})
        return self._submit


def test_resolve_posts_token_and_returns_destination(resolver, monkeypatch):
    """The token must land in cf-turnstile-response and the 302 must be used."""
    destination = "https://files.example.com/video.mp4"

    landing = _FakeResponse(text=LANDING_HTML, url="https://shrinkme.click/abc123")
    submit = _FakeResponse(
        status=302, headers={"Location": destination},
        url="https://shrinkme.click/abc123",
    )
    session = _FakeSession(landing, submit)

    monkeypatch.setattr(
        resolver.cffi_requests, "Session", lambda **kw: session
    )

    def fake_solve(website_url, website_key, action="", cdata=""):
        assert website_key == "0x4AAAAAAA_test_key"
        return "SOLVED_TOKEN"

    monkeypatch.setattr(resolver, "solve_turnstile", fake_solve)
    out = resolver.resolve_shrinkme("https://shrinkme.click/abc123")

    assert out == destination
    # the solved token must be posted under the field Cloudflare validates
    assert session.posted["cf-turnstile-response"] == "SOLVED_TOKEN"
    # and the CSRF material must survive intact
    assert session.posted["_csrfToken"] == "csrf-value-abc"
    assert session.posted["_Token[fields]"] == "fields-value-xyz"


def test_resolve_rejects_when_site_says_captcha_wrong(resolver, monkeypatch):
    landing = _FakeResponse(text=LANDING_HTML, url="https://shrinkme.click/abc123")
    submit = _FakeResponse(
        status=200, text="<p>The CAPTCHA was incorrect. Try again</p>",
        url="https://shrinkme.click/abc123",
    )
    session = _FakeSession(landing, submit)
    monkeypatch.setattr(resolver.cffi_requests, "Session", lambda **kw: session)

    def fake_solve(**kw):
        return "BAD_TOKEN"

    monkeypatch.setattr(resolver, "solve_turnstile", fake_solve)

    with pytest.raises(resolver.DirectDownloadLinkException) as e:
        resolver.resolve_shrinkme("https://shrinkme.click/abc123")
    assert "rejected" in str(e.value)


def test_resolve_requires_key_before_network(resolver, monkeypatch):
    """No key must fail fast, without touching the network at all."""
    resolver.anticaptcha.Config.ANTICAPTCHA_KEY = ""
    try:
        called = []
        monkeypatch.setattr(
            resolver.cffi_requests, "Session",
            lambda **kw: called.append(1) or _FakeSession(None, None),
        )
        with pytest.raises(resolver.DirectDownloadLinkException) as e:
            resolver.resolve_shrinkme("https://shrinkme.click/abc123")
        assert "ANTICAPTCHA_KEY" in str(e.value)
        assert called == []
    finally:
        resolver.anticaptcha.Config.ANTICAPTCHA_KEY = "test-key"


def test_follow_stops_at_foreign_host(resolver):
    """A redirect that leaves the shortlink host is the destination."""
    own = "https://shrinkme.click/abc123"
    hops = {
        "https://shrinkme.click/st?api=x":
            _FakeResponse(status=302,
                          headers={"Location": "https://cdn.example.com/f.mp4"}),
    }
    session = _FakeSession(None, None, hops=hops)
    out = resolver._follow(session, "https://shrinkme.click/st?api=x", own)
    assert out == "https://cdn.example.com/f.mp4"


def test_follow_returns_url_when_no_location(resolver):
    own = "https://shrinkme.click/abc123"
    session = _FakeSession(
        None, None, hops={"https://shrinkme.click/st": _FakeResponse(status=200)}
    )
    out = resolver._follow(session, "https://shrinkme.click/st", own)
    assert out == "https://shrinkme.click/st"


def test_resolve_refuses_to_return_shortlink_host(resolver, monkeypatch):
    """Returning a ShrinkMe URL would make the dispatcher recurse forever."""
    landing = _FakeResponse(text=LANDING_HTML, url="https://shrinkme.click/abc123")
    submit = _FakeResponse(
        status=302,
        headers={"Location": "https://shrinkme.click/again"},
        url="https://shrinkme.click/abc123",
    )
    session = _FakeSession(landing, submit)
    monkeypatch.setattr(resolver.cffi_requests, "Session", lambda **kw: session)
    monkeypatch.setattr(resolver, "solve_turnstile", lambda **kw: "TOKEN")

    with pytest.raises(resolver.DirectDownloadLinkException) as e:
        resolver.resolve_shrinkme("https://shrinkme.click/abc123")
    assert "still on shortlink host" in str(e.value)
