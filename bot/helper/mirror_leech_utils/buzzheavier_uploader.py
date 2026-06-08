from logging import getLogger
from time import time
from os import path as ospath, walk
from asyncio import CancelledError, sleep as asleep
from aiofiles.os import path as aiopath
from aiofiles import open as aiopen
from httpx import AsyncClient, HTTPError, Limits, Timeout

from ..ext_utils.bot_utils import sync_to_async
from ...core.config_manager import Config

LOGGER = getLogger(__name__)

_UPLOAD_BASE = "https://w.buzzheavier.com"
_UPLOAD_CHUNK = 16 * 1024 * 1024
_HTTP_TIMEOUT = Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)
# BuzzHeavier edges occasionally answer a brief 5xx/429 during restarts;
# retry instead of failing the whole task.
_RETRY_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}
_MAX_UPLOAD_RETRIES = 4

def _auth_headers():
    headers = {}
    account_id = (Config.BUZZHEAVIER_ACCOUNT_ID or "").strip()
    if account_id:
        headers["Authorization"] = f"Bearer {account_id}"
    return headers

class BuzzHeavierUploader:

    def __init__(self, listener, path):
        self._listener = listener
        self._path = path
        self._processed_bytes = 0
        self._start_time = time()

    @property
    def processed_bytes(self):
        return self._processed_bytes

    @property
    def speed(self):
        try:
            return self._processed_bytes / (time() - self._start_time)
        except:
            return 0

    async def _stream_file(self, file_path):
        async with aiopen(file_path, "rb") as fh:
            while True:
                if self._listener.is_cancelled:
                    raise CancelledError()
                chunk = await fh.read(_UPLOAD_CHUNK)
                if not chunk:
                    return
                self._processed_bytes += len(chunk)
                yield chunk

    async def _upload_one(self, client, file_path):
        file_name = ospath.basename(file_path)
        file_size = await aiopath.getsize(file_path)
        url = f"{_UPLOAD_BASE}/{file_name}"
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
            **_auth_headers(),
        }

        LOGGER.info(f"Uploading to BuzzHeavier: {file_path}")

        last_error = ""
        sent_before_attempt = self._processed_bytes
        response = None

        for attempt in range(1, _MAX_UPLOAD_RETRIES + 1):
            if self._listener.is_cancelled:
                raise CancelledError()
            # Each retry restarts the stream from byte 0, so reset the
            # progress counter to avoid double-counting on the bar.
            self._processed_bytes = sent_before_attempt
            try:
                response = await client.put(
                    url,
                    content=self._stream_file(file_path),
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
                await asleep(wait)
                continue

            if response.status_code in (200, 201):
                break

            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code in _RETRY_STATUS and attempt < _MAX_UPLOAD_RETRIES:
                wait = min(30, 2 ** attempt)
                LOGGER.warning(
                    f"BuzzHeavier {file_name} attempt {attempt} -> "
                    f"{response.status_code}; retrying in {wait}s"
                )
                await asleep(wait)
                continue

            raise RuntimeError(
                f"BuzzHeavier upload failed [{response.status_code}]: "
                f"{response.text[:200]}"
            )
        else:
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
        if file_id := (data.get("id") or "").strip():
            return f"https://buzzheavier.com/{file_id}"
        else:
            raise RuntimeError("BuzzHeavier response missing file id")

    async def upload(self):
        files = []
        corrupted = 0
        error = ""
        files_dict = {}
        if await aiopath.isfile(self._path):
            files.append(self._path)
        else:
            walk_data = await sync_to_async(lambda: list(walk(self._path)))
            for root, _, names in walk_data:
                for name in sorted(names):
                    candidate = ospath.join(root, name)
                    if await aiopath.isfile(candidate):
                        files.append(candidate)
        if not files:
            await self._listener.on_upload_error(
                "BuzzHeavier: no files were found to upload"
            )
            return
        total_files = len(files)
        try:
            async with AsyncClient(
                timeout=_HTTP_TIMEOUT,
                limits=Limits(max_connections=4, max_keepalive_connections=2),
            ) as client:
                for file_path in files:
                    try:
                        link = await self._upload_one(client, file_path)
                    except (HTTPError, RuntimeError) as exc:
                        LOGGER.error(
                            f"BuzzHeavier Upload Error: {exc} - File Path: {file_path}"
                        )
                        error = str(exc)
                        corrupted += 1
                        continue
                    except CancelledError:
                        return
                    if self._listener.is_cancelled:
                        return
                    if self._listener.files_links:
                        files_dict[link] = ospath.basename(file_path)
        except Exception as exc:
            LOGGER.error(f"BuzzHeavier session error: {exc}")
            await self._listener.on_upload_error(f"BuzzHeavier: {exc}")
            return

        if total_files <= corrupted:
            await self._listener.on_upload_error(
                f"Files Corrupted or unable to upload. {error or 'Check logs!'}"
            )
            return

        if self._listener.is_cancelled:
            return
        LOGGER.info(
            f"Uploaded To BuzzHeavier: {self._listener.name} - {total_files - corrupted} files"
        )
        await self._listener.on_upload_complete(
            None,
            files_dict,
            total_files,
            corrupted,
        )

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self._listener.name}")
        await self._listener.on_upload_error("your upload has been stopped!")