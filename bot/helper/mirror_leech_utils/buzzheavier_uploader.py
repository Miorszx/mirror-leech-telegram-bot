"""BuzzHeavier upload helper.

Used by mirror tasks when the ``-bh`` flag is supplied. Walks the
download directory, streams each file to BuzzHeavier with progress
callbacks that the existing status renderer can read, and finishes by
calling ``listener.on_upload_complete``.
"""

from __future__ import annotations

import asyncio
from html import escape
from logging import getLogger
from os import walk, path as ospath
from time import time
from typing import AsyncIterator

from aiofiles import open as aiopen
from httpx import AsyncClient, HTTPError, Limits, Timeout

from ...core.config_manager import Config
from ..ext_utils.status_utils import get_readable_file_size
from ..ext_utils.telegraph_helper import telegraph
from ..telegram_helper.message_utils import send_message


LOGGER = getLogger(__name__)

_UPLOAD_BASE = "https://w.buzzheavier.com"
_UPLOAD_CHUNK = 16 * 1024 * 1024  # 16 MiB read window
_HTTP_TIMEOUT = Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)
_RETRY_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}
_MAX_UPLOAD_RETRIES = 4


def _auth_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    account_id = (Config.BUZZHEAVIER_ACCOUNT_ID or "").strip()
    if account_id:
        headers["Authorization"] = f"Bearer {account_id}"
    return headers


class BuzzHeavierUploader:
    """Stream files in ``self._path`` to BuzzHeavier sequentially."""

    def __init__(self, listener, path: str):
        self._listener = listener
        self._path = path
        self._processed_bytes = 0
        self._start_time = time()
        self._last_speed_bytes = 0
        self._last_speed_at = self._start_time
        self._speed = 0.0
        self._files_dict: dict[str, str] = {}
        self._total_files = 0
        self._error: str = ""

    # ── status interface ─────────────────────────────────────────────

    @property
    def processed_bytes(self) -> int:
        return self._processed_bytes

    @property
    def speed(self) -> float:
        now = time()
        elapsed = now - self._last_speed_at
        if elapsed >= 1.0:
            delta = self._processed_bytes - self._last_speed_bytes
            self._speed = delta / elapsed if elapsed > 0 else 0.0
            self._last_speed_at = now
            self._last_speed_bytes = self._processed_bytes
        return self._speed

    # ── upload ────────────────────────────────────────────────────────

    async def _stream_file(self, file_path: str, file_size: int) -> AsyncIterator[bytes]:
        async with aiopen(file_path, "rb") as fh:
            while True:
                if self._listener.is_cancelled:
                    return
                chunk = await fh.read(_UPLOAD_CHUNK)
                if not chunk:
                    return
                self._processed_bytes += len(chunk)
                yield chunk

    async def _upload_one(self, client: AsyncClient, file_path: str) -> str:
        """PUT a single file with retry on 5xx / 429 / network errors.

        BuzzHeavier occasionally answers a brief 503 ("Service
        Unavailable") during edge restarts; the previous one-shot
        attempt failed the whole task instead of waiting it out. We
        now retry up to ``_MAX_UPLOAD_RETRIES`` times with
        capped exponential backoff, resetting the on-disk read each
        attempt so the progress bar stays consistent.
        """
        file_name = ospath.basename(file_path)
        file_size = ospath.getsize(file_path)
        url = f"{_UPLOAD_BASE}/{file_name}"
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
            **_auth_headers(),
        }

        LOGGER.info(f"Uploading to BuzzHeavier: {file_name} ({file_size} bytes)")

        last_error: str = ""
        sent_before_attempt = self._processed_bytes
        response = None

        for attempt in range(1, _MAX_UPLOAD_RETRIES + 1):
            if self._listener.is_cancelled:
                raise RuntimeError("Cancelled before BuzzHeavier upload finished")
            # Each retry restarts the stream from byte 0, so reset the
            # progress counter so the bar does not double-count.
            self._processed_bytes = sent_before_attempt
            try:
                response = await client.put(
                    url,
                    content=self._stream_file(file_path, file_size),
                    headers=headers,
                )
            except HTTPError as exc:
                last_error = str(exc)
                if attempt >= _MAX_UPLOAD_RETRIES:
                    raise RuntimeError(
                        f"BuzzHeavier network error after {attempt} attempts: {exc}"
                    ) from exc
                wait = min(30, 2 ** attempt)
                LOGGER.warning(
                    f"BuzzHeavier {file_name} attempt {attempt} hit network error "
                    f"({exc}); retrying in {wait}s"
                )
                await asyncio.sleep(wait)
                continue

            if response.status_code in (200, 201):
                break

            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if (
                response.status_code in _RETRY_STATUS
                and attempt < _MAX_UPLOAD_RETRIES
            ):
                wait = min(30, 2 ** attempt)
                LOGGER.warning(
                    f"BuzzHeavier {file_name} attempt {attempt} -> "
                    f"{response.status_code}; retrying in {wait}s"
                )
                await asyncio.sleep(wait)
                continue

            raise RuntimeError(
                f"BuzzHeavier upload failed [{response.status_code}]: "
                f"{response.text[:200]}"
            )
        else:
            # Loop exhausted without ``break`` -- every attempt was a
            # retryable status that we ran out of patience for.
            raise RuntimeError(
                f"BuzzHeavier upload failed after {_MAX_UPLOAD_RETRIES} attempts: "
                f"{last_error or 'unknown error'}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"BuzzHeavier returned non-JSON response: {response.text[:200]}"
            ) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("BuzzHeavier response missing 'data' object")
        file_id = (data.get("id") or "").strip()
        if not file_id:
            raise RuntimeError("BuzzHeavier response missing file id")
        return f"https://buzzheavier.com/{file_id}"

    async def upload(self) -> None:
        files: list[str] = []
        if ospath.isfile(self._path):
            files.append(self._path)
        else:
            for root, _, names in walk(self._path):
                for name in sorted(names):
                    candidate = ospath.join(root, name)
                    if ospath.isfile(candidate):
                        files.append(candidate)

        if not files:
            await self._listener.on_upload_error(
                "BuzzHeavier: no files were found to upload"
            )
            return

        self._total_files = len(files)
        first_link = ""

        try:
            async with AsyncClient(
                timeout=_HTTP_TIMEOUT,
                limits=Limits(max_connections=4, max_keepalive_connections=2),
            ) as client:
                for file_path in files:
                    if self._listener.is_cancelled:
                        await self._listener.on_upload_error(
                            "BuzzHeavier upload cancelled by user"
                        )
                        return
                    try:
                        link = await self._upload_one(client, file_path)
                    except (HTTPError, RuntimeError) as exc:
                        LOGGER.error(
                            f"BuzzHeavier upload error for "
                            f"{ospath.basename(file_path)}: {exc}"
                        )
                        self._error = str(exc)
                        await self._listener.on_upload_error(
                            f"BuzzHeavier: {exc}"
                        )
                        return
                    self._files_dict[link] = ospath.basename(file_path)
                    if not first_link:
                        first_link = link
        except Exception as exc:  # pragma: no cover - safety net
            LOGGER.error(f"BuzzHeavier session error: {exc}")
            await self._listener.on_upload_error(f"BuzzHeavier: {exc}")
            return

        if self._listener.is_cancelled:
            await self._listener.on_upload_error(
                "BuzzHeavier upload cancelled by user"
            )
            return

        LOGGER.info(
            f"BuzzHeavier upload completed: {self._total_files} file(s)"
        )

        # Mirror branch in ``TaskListener.on_upload_complete`` only
        # renders a single ``link`` button -- the ``files`` dict is
        # ignored unless ``self.is_leech``. For multi-file BuzzHeavier
        # uploads we therefore publish the full link list ourselves so
        # the user does not lose visibility on the rest of the files.
        primary_link = first_link
        if self._total_files > 1:
            await self._post_multi_file_listing()
            telegraph_url = await self._build_telegraph_index()
            if telegraph_url:
                # Hand a single index URL to the listener so the final
                # "Task Done" message links to the Telegraph page that
                # lists every BuzzHeavier file.
                primary_link = telegraph_url

        await self._listener.on_upload_complete(
            primary_link,
            self._files_dict,
            self._total_files,
            "BuzzHeavier",
        )

    # ── multi-file rendering helpers ────────────────────────────────

    async def _post_multi_file_listing(self) -> None:
        """Send the full list of BuzzHeavier links to the user chat.

        Telegram tolerates ~4096 chars per message. We chunk so a
        torrent with hundreds of files still fans out cleanly.
        """
        if not self._files_dict:
            return

        header = (
            f"<b>BuzzHeavier links</b> ({len(self._files_dict)} files)\n"
            f"<b>Name:</b> <code>{escape(self._listener.name)}</code>\n\n"
        )

        chunk = header
        index = 0
        for link, name in self._files_dict.items():
            index += 1
            entry = f"{index}. <a href='{link}'>{escape(name)}</a>\n"
            if len(chunk.encode()) + len(entry.encode()) > 3800:
                try:
                    await send_message(self._listener.message, chunk)
                except Exception as exc:
                    LOGGER.warning(f"BuzzHeavier listing send failed: {exc}")
                # Subsequent chunks omit the header so we do not repeat
                # the name on every message.
                chunk = entry
                continue
            chunk += entry

        if chunk:
            try:
                await send_message(self._listener.message, chunk)
            except Exception as exc:
                LOGGER.warning(f"BuzzHeavier listing send failed: {exc}")

    async def _build_telegraph_index(self) -> str:
        """Create a Telegraph page that indexes every BuzzHeavier link.

        Returns the public URL on success, or an empty string when
        Telegraph is unreachable / fails -- the caller falls back to
        the first BuzzHeavier link in that case.
        """
        if not self._files_dict:
            return ""

        rows: list[str] = []
        for link, name in self._files_dict.items():
            rows.append(
                f"<li><a href='{link}'>{escape(name)}</a></li>"
            )

        title = self._listener.name or "BuzzHeavier upload"
        # Truncate Telegraph titles to their 256 char ceiling.
        title = title[:256]
        body = (
            f"<h3>{escape(title)}</h3>"
            f"<p>{len(self._files_dict)} file(s) uploaded to BuzzHeavier.</p>"
            f"<ol>{''.join(rows)}</ol>"
        )

        try:
            page = await telegraph.create_page(title, body)
        except Exception as exc:
            LOGGER.warning(f"BuzzHeavier Telegraph index failed: {exc}")
            return ""

        path = page.get("path") if isinstance(page, dict) else None
        if not path:
            return ""
        return f"https://graph.org/{path}"
