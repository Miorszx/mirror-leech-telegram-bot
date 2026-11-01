"""Provider-family shortlink resolvers.

The old model was *one shortener, one handler*: `bypass_shortener()` matched a
hostname and called a bespoke function for it (ouo.io, shrinkme, ...). That
model breaks the moment a provider rotates its front door, because the same
destination appears under five different domains and the resolver only knows
one of them.

The family model instead resolves **the journey**: a slug is entered at a
canonical gate, and the walker follows every hop until it lands on a file host.
Domain matching is only used to pick the *entry point* and the *expected gate
vocabulary*, never the whole strategy.

Measured topology (2026-11, nakalituindah.io corpus, 2970 slugs):

    tpi.li / tii.la / tvi.la / srnky.com   -> converged front doors
    shrinkme.click/<slug>                  Turnstile, auto-solved by patchright
        themezon.net/<art>                 .smg- 1/2  #j-start -> #j-next
        themezon.net/<art>                 .smg- 2/2  GENERATE LINK
    shrinkme.click/<slug>                  round 2, countdown -> Get Link
        tech8s.net/<art>                   .step-btn stepOne / stepTwo
        game5s.com/<art>                   3/3
        droplink.co/<Slug>                 countdown -> Get Link
    mediafire.com/file/<key>/<name>        -> downloadNNNN.mediafire.com/...

Why this needs a real browser and cannot be done with curl_cffi
---------------------------------------------------------------
The reverted commit (e14cce9) tried an external human captcha service. It was
reverted because it needs an API key, bills per solve, and still needs form
replay. patchright drives the actual widget in a real Chromium: the Turnstile
token mounts by itself in ~3s, costs nothing, and needs no key. The trade is
that it needs a display, which the container has via Xvfb.

What actually blocks naive automation (all of these were live bugs)
------------------------------------------------------------------
1. The submit control is ``#invisibleCaptchaShortlink``, not ``#continue``.
   A resolver that looks for ``#continue`` never presses anything.
2. ``form.submit()`` is shadowed by ``<button name="submit">``. Must be
   ``HTMLFormElement.prototype.submit.call(form)``.
3. ``#j-adblock`` overlays the ``.smg-`` gate and swallows clicks.
4. ``#j-next`` is ``disabled`` when the gate first renders. It only enables
   after ``#j-start`` arms the 8 second bar, so its state must be polled.
5. The ``.smg-`` gate keeps the SAME url across step 1/2 -> 2/2. A loop guard
   keyed on url alone kills a healthy walk. Key on url + the "STEP n OF m"
   label.
6. ``droplink.co/<slug>`` cold returns a 457-byte bounce to
   ``tech8s.net/safe2.php?link=<slug>``. ``safe2.php`` is a shell: the real
   hand-off is in the ad tab that ``step-btn`` opened.
7. The last page is a countdown ("YOUR LINK IS ALMOST READY. 6 Seconds").
   It is not a dead end; wait for the "Get Link" button to appear.

Session rules
-------------
* One browser context per slug. A jar shared between slugs poisons the walk:
  the ``tp2`` cookie from a previous run makes droplink hand back a stub, and
  the resolver ping-pongs between tech8s and droplink forever.
* Never reuse a ``tp2`` across slugs. It encodes the slug.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ...ext_utils.exceptions import DirectDownloadLinkException

# Front doors that all converge on the same provider family.
_FAMILY = (
    "tpi.li",
    "tii.la",
    "tvi.la",
    "srnky.com",
    "shrinkme.",
    "shrinke.",
    "shrinkearn.",
    "shrinkforearn.",
)

# Canonical entry. Every family member was verified to be case-insensitive on
# the slug, so the slug is passed through unchanged.
_ENTRY = "https://shrinkme.click/{slug}"

FILE_HOST_RE = re.compile(
    r"(?:^|//)(?:[\w-]+\.)*mediafire\.com/|modsfire\.com|mega\.nz/|"
    r"drive\.google\.com/file|pixeldrain\.com|gofile\.io|dropbox\.com/s/|"
    r"krakenfiles|workupload",
    re.I,
)

_CDN_RE = re.compile(r'https://download\d*\.mediafire\.com/[^"\'\\\s]+')

# Turnstile submit control. Falls back through the older ids on purpose.
_SOLVE_JS = """() => {
  const b = document.querySelector('#link-view button[type=submit]')
         || document.getElementById('invisibleCaptchaShortlink')
         || document.getElementById('continue');
  if (b) { b.disabled = false; b.click(); return 'btn:' + (b.id || 'submit'); }
  const fs = [...document.querySelectorAll('form')]
    .filter(f => f.querySelector('input[name=token]'));
  if (fs.length) {
    HTMLFormElement.prototype.submit.call(fs[fs.length - 1]);
    return 'tokenform';
  }
  return null;
}"""

_STATE_JS = """() => {
  const bar  = document.querySelector('.smg-cbar');
  const body = document.body;
  return {
    url:   location.href,
    title: document.title,
    body:  (body.innerText || '').replace(/\\s+/g, ' ').slice(0, 260),
    token: (document.querySelector('[name=cf-turnstile-response]') || {}).value || '',
    smg:   document.querySelectorAll('.smg-btn').length,
    stepBtn: document.querySelectorAll('.step-btn').length,
    dropN: document.querySelectorAll('[id^=droplink-step]').length,
    goLink: !!document.getElementById('go-link'),
    jStart: !!document.getElementById('j-start'),
    jNext:  (() => { const b = document.getElementById('j-next');
                     return b ? !b.disabled : null; })(),
    captchaBtn: !!document.querySelector(
        '#link-view button[type=submit],#invisibleCaptchaShortlink'),
    ab: (() => { const e = document.getElementById('j-adblock');
                 return !!(e && getComputedStyle(e).display !== 'none'); })(),
    stepTxt: (() => { const m = (body.innerText || '')
                        .match(/STEP\\s*(\\d)\\s*OF\\s*(\\d)/i);
                      return m ? m[0] : null; })(),
    bounce: (body.innerHTML.match(
        /window\\.location\\.href\\s*=\\s*["']([^"']+)["']/) || [])[1] || null,
    cdn: (body.innerHTML.match(
        /https:\\/\\/download[0-9]*\\.mediafire\\.com\\/[^"'\\\\ ]+/) || [])[0] || null
  };
}"""

_GENERIC_JS = """() => {
  const w = /get ?link|continue to|continue|proceed|click here|verify|generate link|unlock|i'?m human|download|next/i;
  for (const e of document.querySelectorAll('a,button,input[type=submit]')) {
    const t = (e.innerText || e.value || '').trim();
    if (!t || t.length > 46 || !w.test(t)) continue;
    const c = getComputedStyle(e);
    if (c.display === 'none' || c.visibility === 'hidden') continue;
    if (e.getBoundingClientRect().width < 4) continue;
    e.click();
    return t.slice(0, 34);
  }
  return null;
}"""

_HIDE_TAB_JS = """(() => {
  for (const k of ['hidden', 'webkitHidden', 'visibilityState'])
    try {
      Object.defineProperty(Document.prototype, k, {
        get: () => (k === 'visibilityState' ? 'visible' : false),
        configurable: true,
      });
    } catch (e) {}
  window.dispatchEvent(new Event('visibilitychange'));
})()"""


@dataclass
class WalkResult:
    """Outcome of a single slug journey."""

    slug: str
    final: str | None = None
    cdn: str | None = None
    trail: list = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.cdn or (self.final and FILE_HOST_RE.search(self.final)))


def is_provider_family(domain: str) -> bool:
    """True when the domain belongs to a family this module can resolve."""
    if not domain:
        return False
    return any(d in domain for d in _FAMILY)


def slug_from_url(link: str) -> str:
    """Pull the shortlink slug out of any family member's URL.

    The slug is case sensitive on the destination side but every family front
    door was verified case-insensitive, so it is passed through as written.

    Fails when the last path segment is empty *or* is a known gate page: a URL
    like ``shrinkme.click/themezon.net`` has a non-empty tail that is not a
    slug, and treating it as one produces a confident 404 instead of an error.
    """
    tail = link.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not tail or "." in tail:
        raise DirectDownloadLinkException(f"ERROR: shortlink: no slug in {link!r}")
    return tail


def browser_available() -> bool:
    """`SHORTLINK_BROWSER=1` opts in; default off so headless nodes fail clearly."""
    return os.environ.get("SHORTLINK_BROWSER", "").strip() in ("1", "true", "yes")


def resolve(link: str, budget: int = 230, headful: bool | None = None) -> str:
    """Resolve one provider-family shortlink to its file host URL.

    Returns the MediaFire (or other file host) URL. Raises
    ``DirectDownloadLinkException`` with a precise reason on any failure, so a
    caller can distinguish "gate changed" from "network died".
    """
    if not browser_available():
        raise DirectDownloadLinkException(
            "ERROR: shortlink bypass needs a browser; set SHORTLINK_BROWSER=1 "
            "and run under Xvfb (headful Chromium is mandatory for Turnstile)"
        )
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - environment guard
        raise DirectDownloadLinkException(
            f"ERROR: shortlink bypass requires patchright: {e}"
        ) from e

    slug = slug_from_url(link)
    res = WalkResult(slug=slug)

    with sync_playwright() as pw:
        br = pw.chromium.launch(
            headless=False if headful is None else not headful,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = None
        try:
            ctx = br.new_context(viewport={"width": 1440, "height": 950})
            pg = ctx.new_page()
            pg.on("popup", lambda p: p.close())
            _walk(pg, ctx, _ENTRY.format(slug=slug), res, budget)
            try:
                st = pg.evaluate(_STATE_JS)
                res.cdn = res.cdn or st.get("cdn")
            except Exception:
                pass
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
            br.close()

    out = res.cdn or res.final
    if res.ok and out:
        return out
    raise DirectDownloadLinkException(
        f"ERROR: shortlink {slug} did not reach a file host "
        f"({res.seconds:.0f}s, last hops: {' -> '.join(res.trail[-3:])})"
    )


def _hide_tab(ctx, page):
    """Make the page believe it is foregrounded, in the CDP main world.

    Isolated-world overrides do not stick: the ad-timer scripts read
    ``document.hidden`` from the main world and never see the patch. Page-level
    add_init_script is also wrong here, because it re-runs on every navigation
    and suppresses the Turnstile widget on the gates that carry one.
    """
    try:
        c = ctx.new_cdp_session(page)
        c.send("Runtime.enable")
        c.send(
            "Runtime.evaluate",
            {"expression": _HIDE_TAB_JS, "returnByValue": True},
        )
    except Exception:
        pass


def _walk(pg, ctx, start, res, budget):
    import time

    t0 = time.time()
    seen = {}
    try:
        pg.goto(start, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        res.trail.append(f"goto-fail:{str(e)[:40]}")
        return

    for _ in range(45):
        if time.time() - t0 > budget:
            res.trail.append("budget")
            break

        st = None
        for _ in range(3):
            try:
                st = pg.evaluate(_STATE_JS)
                break
            except Exception:
                pg.wait_for_timeout(1200)
        if st is None:
            res.trail.append("eval-fail")
            pg.wait_for_timeout(1500)
            continue

        url = st["url"]
        res.trail.append(url[:80])
        if FILE_HOST_RE.search(url) or st["cdn"]:
            res.final = url
            res.cdn = st["cdn"]
            break

        # The .smg- gate keeps its url across steps, so the loop guard has to
        # include the step label or a healthy walk looks like a stall.
        key = url.split("#")[0] + "|" + (st["stepTxt"] or "")
        seen[key] = seen.get(key, 0) + 1
        if seen[key] >= 4:
            res.trail.append("repeat")
            break

        for p in ctx.pages[1:]:
            try:
                p.close()
            except Exception:
                pass

        # Turnstile gate: the token mounts by itself in ~3s, headful only.
        if st["captchaBtn"] or "check the captcha" in st["body"].lower():
            for _ in range(22):
                tk = pg.evaluate(
                    "() => (document.querySelector('[name=cf-turnstile-response]')||{}).value||''"
                )
                if len(tk) > 100:
                    break
                pg.wait_for_timeout(600)
            try:
                with pg.expect_navigation(timeout=30000):
                    pg.evaluate(_SOLVE_JS)
            except Exception:
                pass
            pg.wait_for_timeout(1400)
            continue

        # themezon .smg- gate
        if st["smg"]:
            _hide_tab(ctx, pg)
            if st["ab"]:
                pg.evaluate(
                    "() => { const e=document.getElementById('j-adblock');"
                    " if (e) { e.style.display='none'; e.remove(); } }"
                )
                pg.wait_for_timeout(500)
            if st["jStart"]:
                pg.evaluate("() => document.getElementById('j-start').click()")
                # j-next is disabled until the 8s bar arms; poll, do not read once
                for _ in range(24):
                    pg.wait_for_timeout(600)
                    if pg.evaluate(
                        "() => {const b=document.getElementById('j-next');"
                        "return b ? !b.disabled : false}"
                    ):
                        break
            if pg.evaluate(
                "() => {const b=document.getElementById('j-next');"
                "return b ? !b.disabled : false}"
            ):
                pg.evaluate("() => document.getElementById('j-next').click()")
                pg.wait_for_timeout(3600)
            pg.evaluate(
                """() => {
                    const b=[...document.querySelectorAll('button,a')].find(e=>
                      /generate link|continue to your link|get ?link/i
                        .test(e.innerText||'') &&
                      getComputedStyle(e).display!=='none' && !e.disabled);
                    if (b) b.click();
                }"""
            )
            pg.wait_for_timeout(2400)
            continue

        # tech8s step gate (stepOne / stepTwo, 8s each)
        if st["stepBtn"] >= 1:
            _hide_tab(ctx, pg)
            for sid in pg.evaluate(
                "() => [...document.querySelectorAll('.step-btn')].map(e=>e.id)"
            ):
                pg.evaluate(
                    f"() => {{const e=document.getElementById('{sid}'); if(e) e.click();}}"
                )
                for _ in range(16):
                    pg.wait_for_timeout(750)
                    if "1/1" in pg.evaluate(
                        f"() => (document.getElementById('{sid}')||{{}}).innerText||''"
                    ):
                        break
            pg.evaluate(
                "() => { for (const id of ['t_modal','clickHereContinue'])"
                " { const e=document.getElementById(id); if (e) e.style.display='block'; } }"
            )
            pg.wait_for_timeout(800)
            try:
                with pg.expect_navigation(timeout=25000):
                    pg.evaluate(
                        "() => {const a=document.getElementById('go_d2'); if(a) a.click();}"
                    )
            except Exception:
                pass
            pg.wait_for_timeout(2000)
            continue

        # droplink step gate
        if st["dropN"] >= 1:
            _hide_tab(ctx, pg)
            for sid in ("droplink-step-one", "droplink-step-two"):
                pg.evaluate(
                    f"() => {{const e=document.getElementById('{sid}'); if(e) e.click();}}"
                )
                for _ in range(16):
                    pg.wait_for_timeout(750)
                    if "1/1" in pg.evaluate(
                        f"() => (document.getElementById('{sid}')||{{}}).innerText||''"
                    ):
                        break
            href = pg.evaluate(
                "() => {const a=document.getElementById('go_d2');"
                "return a ? a.getAttribute('href') : null}"
            )
            if href:
                if href.startswith("/"):
                    href = "https://" + url.split("/")[2] + href
                pg.goto(href, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(2000)
                continue

        # JS bounce stub / meta refresh
        if st["bounce"]:
            nxt = st["bounce"]
            if "google.com/url" in nxt:
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(nxt).query).get("url")
                if q:
                    nxt = q[0]
            pg.goto(nxt, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(2200)
            continue

        # Countdown page: not a dead end, wait for "Get Link" to appear.
        if st["goLink"] and re.search(r"(seconds|please wait)", st["body"], re.I):
            for _ in range(28):
                pg.wait_for_timeout(1000)
                if pg.evaluate(
                    """() => {
                        const b=[...document.querySelectorAll('button,a')].find(e=>
                          /get ?link/i.test(e.innerText||'') &&
                          getComputedStyle(e).display!=='none' &&
                          e.getBoundingClientRect().width>3);
                        if (b) { b.click(); return true; }
                        return false;
                    }"""
                ):
                    pg.wait_for_timeout(3000)
                    break
            continue

        # safe2.php shell: follow the ad tab's hand-off, never dead-end here.
        if "safe2.php" in url and not st["stepBtn"] and not st["smg"]:
            alt = pg.evaluate(
                """() => {
                    for (const a of document.querySelectorAll('a')) {
                        const h = a.getAttribute('href') || '';
                        if (!h || h.startsWith('#') || /safe2/.test(h)) continue;
                        if (/droplink|tech8s|game5s|mediafire/i.test(h)) return h;
                    }
                    return null;
                }"""
            )
            if alt:
                pg.goto(
                    alt if alt.startswith("http") else "https://" + url.split("/")[2] + alt,
                    wait_until="domcontentloaded",
                    timeout=35000,
                )
                pg.wait_for_timeout(2000)
                continue
            pg.goto(
                _ENTRY.format(slug=res.slug),
                wait_until="domcontentloaded",
                timeout=35000,
            )
            pg.wait_for_timeout(2200)
            continue

        before = pg.url
        clicked = pg.evaluate(_GENERIC_JS)
        pg.wait_for_timeout(2300)
        if not clicked and pg.url == before:
            pg.wait_for_timeout(2500)
            clicked = pg.evaluate(_GENERIC_JS)
            pg.wait_for_timeout(2000)
            if not clicked and pg.url == before:
                res.trail.append("dead-end")
                break

    res.seconds = time.time() - t0


@dataclass
class BatchStats:
    ok: int = 0
    fail: int = 0
    links: list = field(default_factory=list)


def resolve_batch(slugs, workers: int = 6, budget: int = 230, on_result=None) -> BatchStats:
    """Resolve many slugs. One context per slug; no cookie reuse, ever.

    Threaded because the bottleneck is the 8 second ad bars, not the network:
    the page sits idle waiting for a timer, so six browsers cost barely more
    than one.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    stats = BatchStats()

    def _one(slug):
        try:
            return slug, resolve(_ENTRY.format(slug=slug), budget=budget), None
        except DirectDownloadLinkException as e:
            return slug, None, str(e)[:120]
        except Exception as e:  # pragma: no cover - transport level
            return slug, None, f"{type(e).__name__}: {str(e)[:100]}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, s) for s in slugs]
        for f in as_completed(futs):
            slug, link, err = f.result()
            if link:
                stats.ok += 1
                stats.links.append(link)
            else:
                stats.fail += 1
            if on_result:
                on_result(slug, link, err)
    return stats
