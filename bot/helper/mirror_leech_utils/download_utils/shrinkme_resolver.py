"""ShrinkMe.io shortlink resolver.

ShrinkMe gates the destination behind a Cloudflare Turnstile checkbox. The
page is a plain POST form (``form#link-view``) carrying the usual CSRF
material plus one invisible field the widget fills in:

    <form method="post" id="link-view" action="/<id>">
      _method=POST
      _csrfToken=<token>
      action=captcha
      f_n=slc
      _Token[fields]=...
      _Token[unlocked]=adcopy_challenge|adcopy_response|
                       cf-turnstile-response|g-recaptcha-response|
                       h-captcha-response

    <button id="invisibleCaptchaShortlink" type="submit">Click here to continue</button>

Submitting that form without a solved captcha is answered with
"The CAPTCHA was incorrect. Try again" and the page reloads. A valid token in
``cf-turnstile-response`` is the only difference between failure and the 302
that carries the destination.

The site announces its own settings inline:

    "enable_captcha":"yes","captcha_type":"turnstile",
    "turnstile_site_key":"0x4AAA..."

so the sitekey is read from the page rather than hardcoded -- it can rotate.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests

from bot.helper.ext_utils.exceptions import DirectDownloadLinkException

from .anticaptcha_turnstile import solve_turnstile, solver_configured

_DOMAINS = ("shrinkme.", "shrinke.", "shrinkearn.", "shrinkforearn.")

_IMPERSONATE = "chrome131"

_SITEKEY_RE = re.compile(
    r'"turnstile_site_key"\s*:\s*"([^"]+)"'
    r'|data-sitekey="([^"]+)"'
    r'|<div[^>]*class="[^"]*cf-turnstile[^"]*"[^>]*data-sitekey="([^"]+)"',
    re.I,
)

_FORM_RE = re.compile(
    r'<form[^>]*id="link-view"[^>]*>(.*?)</form>', re.S | re.I
)
_INPUT_RE = re.compile(r"<input[^>]*>", re.I)
_NAME_RE = re.compile(r'name="([^"]*)"')
_VALUE_RE = re.compile(r'value="([^"]*)"')
_TYPE_RE = re.compile(r'type="([^"]*)"')

_CAPTCHA_FIELD = "cf-turnstile-response"


def is_shrinkme(domain: str) -> bool:
    """True for ShrinkMe-family hosts."""
    return any(d in (domain or "") for d in _DOMAINS)


def _parse_form(html: str) -> dict:
    """Return every hidden field of form#link-view, exactly as a browser would.

    Replaying the form verbatim matters: the CSRF material is tied to the
    session cookie, so a partial payload is rejected before the captcha is
    even looked at.
    """
    m = _FORM_RE.search(html)
    if not m:
        raise DirectDownloadLinkException(
            "ERROR: shrinkme: form#link-view not found"
        )

    fields = {}
    for tag in _INPUT_RE.finditer(m.group(1)):
        raw = tag.group(0)
        name = _NAME_RE.search(raw)
        if not name:
            continue
        typ = _TYPE_RE.search(raw)
        if typ and typ.group(1).lower() == "submit":
            continue
        value = _VALUE_RE.search(raw)
        fields[name.group(1)] = value.group(1) if value else ""
    return fields


def _sitekey(html: str) -> str:
    """Read the Turnstile sitekey out of the page's own config."""
    for m in _SITEKEY_RE.finditer(html):
        for group in m.groups():
            if group:
                return group
    raise DirectDownloadLinkException(
        "ERROR: shrinkme: turnstile sitekey not found on page"
    )


def resolve_shrinkme(link: str) -> str:
    """Resolve a ShrinkMe shortlink to its destination URL.

    Returns the destination string so the caller can re-dispatch it through the
    regular host handlers, matching the ``str`` contract used by other
    resolvers in this package.
    """
    if not solver_configured():
        raise DirectDownloadLinkException(
            "ERROR: shrinkme: Turnstile captcha requires ANTICAPTCHA_KEY"
        )

    try:
        with cffi_requests.Session(impersonate=_IMPERSONATE, timeout=30) as s:
            landing = s.get(link, allow_redirects=True)
            if landing.status_code == 403:
                raise DirectDownloadLinkException(
                    "ERROR: shrinkme blocked the request (403)"
                )
            if landing.status_code != 200:
                raise DirectDownloadLinkException(
                    f"ERROR: shrinkme: landing status {landing.status_code}"
                )

            fields = _parse_form(landing.text)
            sitekey = _sitekey(landing.text)

            # Solved out-of-band by a human worker; this is the only step that
            # cannot be done with code. The solver is synchronous because this
            # whole function runs in a worker thread.
            token = solve_turnstile(website_url=link, website_key=sitekey)

            fields[_CAPTCHA_FIELD] = token
            action = urlparse(landing.url).path or urlparse(link).path

            submit = s.post(
                landing.url or link,
                data=fields,
                headers={"Referer": landing.url or link, "Origin":
                         f"{urlparse(link).scheme}://{urlparse(link).netloc}"},
                allow_redirects=False,
            )

            location = submit.headers.get("Location", "")
            if submit.status_code in (301, 302, 303, 307) and location:
                # A shortlink usually bounces through its own /st? page before
                # the real destination; follow until the host changes.
                if location.startswith("/"):
                    location = f"{urlparse(link).scheme}://{urlparse(link).netloc}{location}"
                dest = _follow(s, location, link)
                # The dispatcher re-dispatches whatever we return. Handing back
                # another ShrinkMe URL would make it call us again forever, so
                # treat that as a failed resolve instead of recursing.
                if is_shrinkme(urlparse(dest).hostname or ""):
                    raise DirectDownloadLinkException(
                        "ERROR: shrinkme: still on shortlink host after solve "
                        f"({dest})"
                    )
                return dest

            if "captcha was incorrect" in submit.text.lower():
                raise DirectDownloadLinkException(
                    "ERROR: shrinkme rejected the captcha token"
                )
            raise DirectDownloadLinkException(
                f"ERROR: shrinkme: submit status {submit.status_code} "
                f"(action={action})"
            )

    except DirectDownloadLinkException:
        raise
    except Exception as e:
        raise DirectDownloadLinkException(
            f"ERROR: shrinkme resolve failed: {e.__class__.__name__}"
        ) from e


def _follow(session, url: str, original: str) -> str:
    """Follow the shortlink's own redirect chain until it leaves the host."""
    own_host = urlparse(original).hostname or ""
    seen = set()
    for _ in range(6):
        if url in seen:
            break
        seen.add(url)
        host = urlparse(url).hostname or ""
        if host and own_host and not (
            host == own_host or host.endswith("." + own_host.split(".", 1)[-1])
        ):
            return url
        resp = session.get(url, allow_redirects=False)
        nxt = resp.headers.get("Location", "")
        if not nxt:
            return url
        if nxt.startswith("/"):
            p = urlparse(url)
            nxt = f"{p.scheme}://{p.netloc}{nxt}"
        url = nxt
    return url
