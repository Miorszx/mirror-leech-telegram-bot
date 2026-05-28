"""Status reporter for an in-progress AllDebrid magnet/torrent unlock.

While the bot waits for AllDebrid to finish torrenting (or cache hit
processing), this status object exposes the same shape used by
``QbittorrentStatus`` so the existing status renderer in
``bot/helper/ext_utils/status_utils.py`` can show the user real
progress instead of a silent wait.

The status reads its values from a mutable ``state`` dict that the
resolver's progress callback keeps fresh.
"""

from __future__ import annotations

from typing import Any

from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


class AllDebridMagnetStatus:
    """Visible-to-user status object during the AllDebrid pre-download phase."""

    # AllDebrid statusCode ⇒ readable label
    _PHASE_LABELS = {
        0: "Queued",
        1: "Downloading on AllDebrid",
        2: "Compressing on AllDebrid",
        3: "Uploading to AllDebrid",
        4: "Ready",
    }

    def __init__(self, listener, gid: str, state: dict[str, Any]):
        self.listener = listener
        self._gid = gid
        self._state = state
        self.tool = "alldebrid"

    # ── plumbing helpers ─────────────────────────────────────────────

    def _status_code(self) -> int:
        try:
            return int(self._state.get("statusCode") or 0)
        except (TypeError, ValueError):
            return 0

    def _phase(self) -> str:
        return self._state.get("phase") or "torrent"

    # ── status-renderer interface (matches QbittorrentStatus shape) ─

    def gid(self) -> str:
        return self._gid

    def name(self) -> str:
        return self._state.get("name") or self.listener.name or "AllDebrid Magnet"

    def size(self) -> str:
        return get_readable_file_size(int(self._state.get("size") or 0))

    def progress(self) -> str:
        try:
            done = int(self._state.get("downloaded") or 0)
            total = int(self._state.get("size") or 0)
            if total <= 0:
                return "0%"
            return f"{round(done / total * 100, 2)}%"
        except Exception:
            return "0%"

    def processed_bytes(self) -> str:
        return get_readable_file_size(int(self._state.get("downloaded") or 0))

    def speed(self) -> str:
        return f"{get_readable_file_size(int(self._state.get('downloadSpeed') or 0))}/s"

    def eta(self) -> str:
        try:
            done = int(self._state.get("downloaded") or 0)
            total = int(self._state.get("size") or 0)
            speed = int(self._state.get("downloadSpeed") or 0)
            if speed <= 0 or total <= done:
                return "-"
            return get_readable_time((total - done) / speed)
        except Exception:
            return "-"

    def status(self) -> str:
        # Differentiate the two phases so the renderer picks the right
        # icon. Unlock phase is short; map it to "downloading" so the
        # status row stays.
        phase = self._phase()
        if phase == "unlock":
            return MirrorStatus.STATUS_DOWNLOAD
        return MirrorStatus.STATUS_DOWNLOAD

    def seeders_num(self) -> int:
        try:
            return int(self._state.get("seeders") or 0)
        except (TypeError, ValueError):
            return 0

    def leechers_num(self) -> int:
        try:
            return int(self._state.get("downloaders") or self._state.get("peers") or 0)
        except (TypeError, ValueError):
            return 0

    def task(self):
        return self

    async def cancel_task(self):
        """Cooperative cancel: flip the listener flag and best-effort
        delete the magnet from AllDebrid history.

        The polling loop checks ``listener.is_cancelled`` on every
        iteration, so toggling it is enough to escape ``poll_until_ready``.
        """
        self.listener.is_cancelled = True
        magnet_id = int(getattr(self.listener, "_alldebrid_magnet_id", 0) or 0)
        if magnet_id:
            try:
                from ..download_utils.alldebrid_resolver import delete_magnet

                await delete_magnet(magnet_id)
            except Exception:
                pass
        await self.listener.on_download_error("AllDebrid magnet cancelled by user")
