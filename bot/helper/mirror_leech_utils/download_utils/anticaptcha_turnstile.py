"""Cloudflare Turnstile solver via anti-captcha.com.

Turnstile cannot be solved in code: the widget runs inside a Cloudflare iframe
and the response token is issued only after behavioural analysis. A headless
browser is refused the iframe outright, and submitting the form without a
token is answered with "The CAPTCHA was incorrect". The only working route is
to hand the challenge to a human worker and receive the token back.

The API contract used here is TurnstileTaskProxyless:

    POST https://api.anti-captcha.com/createTask
      {"clientKey": "...", "task": {"type": "TurnstileTaskProxyless",
                                    "websiteURL": "...", "websiteKey": "..."}}
      -> {"errorId": 0, "taskId": 12345}

    POST https://api.anti-captcha.com/getTaskResult
      {"clientKey": "...", "taskId": 12345}
      -> {"errorId": 0, "status": "processing"}
      -> {"errorId": 0, "status": "ready",
          "solution": {"token": "...", "userAgent": "..."}}

The returned token is then posted to the target site as the
``cf-turnstile-response`` form field.

Deliberately synchronous: ``direct_link_generator`` runs inside a worker thread
(via ``sync_to_async``), so blocking here is correct and avoids nesting an
event loop inside a thread that may not own one.
"""

from __future__ import annotations

from time import sleep

from httpx import Client, HTTPError

from bot import LOGGER
from bot.core.config_manager import Config
from bot.helper.ext_utils.exceptions import DirectDownloadLinkException

_API_BASE = "https://api.anti-captcha.com"
_TIMEOUT = 30.0
# Workers normally answer in 5-15 s; the poll budget is deliberately generous
# so a slow queue does not look like a hard failure.
_POLL_INTERVAL = 3.0
_POLL_ATTEMPTS = 40


def solver_configured() -> bool:
    """True when an API key is present, i.e. captcha solving is possible."""
    return bool(getattr(Config, "ANTICAPTCHA_KEY", ""))


def solve_turnstile(
    website_url: str, website_key: str, action: str = "", cdata: str = ""
) -> str:
    """Return a Turnstile response token for ``website_url``.

    Raises ``DirectDownloadLinkException`` when no key is configured, the API
    rejects the task, or the poll budget runs out. The caller is expected to
    post the token back to the site as ``cf-turnstile-response``.
    """
    key = getattr(Config, "ANTICAPTCHA_KEY", "")
    if not key:
        raise DirectDownloadLinkException(
            "ERROR: ANTICAPTCHA_KEY is not configured, cannot solve Turnstile"
        )

    task = {
        "type": "TurnstileTaskProxyless",
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if action:
        task["action"] = action
    if cdata:
        task["cData"] = cdata

    try:
        with Client(base_url=_API_BASE, timeout=_TIMEOUT) as client:
            resp = client.post(
                "/createTask", json={"clientKey": key, "task": task}
            )
            resp.raise_for_status()
            payload = resp.json()

            if payload.get("errorId") != 0:
                raise DirectDownloadLinkException(
                    "ERROR: anti-captcha createTask failed: "
                    f"{payload.get('errorCode')} ({payload.get('errorDescription')})"
                )

            task_id = payload.get("taskId")
            if not task_id:
                raise DirectDownloadLinkException(
                    "ERROR: anti-captcha returned no taskId"
                )
            LOGGER.info("Turnstile task %s queued for %s", task_id, website_url)

            for _ in range(_POLL_ATTEMPTS):
                sleep(_POLL_INTERVAL)
                resp = client.post(
                    "/getTaskResult", json={"clientKey": key, "taskId": task_id}
                )
                resp.raise_for_status()
                result = resp.json()

                if result.get("errorId") not in (0, None):
                    raise DirectDownloadLinkException(
                        "ERROR: anti-captcha getTaskResult failed: "
                        f"{result.get('errorCode')} "
                        f"({result.get('errorDescription')})"
                    )

                if result.get("status") == "ready":
                    token = (result.get("solution") or {}).get("token")
                    if not token:
                        raise DirectDownloadLinkException(
                            "ERROR: anti-captcha returned an empty token"
                        )
                    LOGGER.info("Turnstile task %s solved", task_id)
                    return token

    except DirectDownloadLinkException:
        raise
    except HTTPError as e:
        raise DirectDownloadLinkException(
            f"ERROR: anti-captcha request failed: {e.__class__.__name__}"
        ) from e

    raise DirectDownloadLinkException(
        f"ERROR: anti-captcha timed out after {_POLL_ATTEMPTS} polls"
    )
