from time import time

from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)
from ..download_utils.alldebrid_resolver import _MAGNET_STATUS_LABELS


class AllDebridStatus:
    """Live status for the AllDebrid resolve phase.

    AllDebrid magnet resolution (queue -> downloading -> compressing ->
    uploading -> ready -> generating direct links) can take minutes. This
    object is registered in ``task_dict`` under the SAME ``mid`` as the
    eventual download, and rendered through the SAME aggregated status
    message (see ``get_readable_message``). When the direct links are ready
    the caller replaces this object with a ``DirectStatus`` / ``QueueStatus``
    under that same ``mid``, so the single status line visibly evolves from
    "AllDebrid" into "Download" instead of a throwaway message being created
    and deleted.
    """

    def __init__(self, listener, gid):
        self.listener = listener
        self._gid = gid
        self.tool = "alldebrid"
        self._name = ""
        self._phase = "In queue"
        self._size = 0
        self._downloaded = 0
        self._seeders = 0
        self._has_seeders = False
        self._speed = 0
        self._last_time = 0.0
        self._last_downloaded = 0

    def update(self, snapshot):
        """Feed a resolver progress snapshot into this status object."""
        now = time()
        if "unlock_done" in snapshot:
            done = int(snapshot.get("unlock_done", 0) or 0)
            total = int(snapshot.get("unlock_total", 0) or 0)
            self._phase = f"generating direct links {done}/{total}"
            self._speed = 0
            self._last_time = now
            return

        name = snapshot.get("filename") or snapshot.get("title")
        if name:
            self._name = str(name)
        size = int(snapshot.get("size", 0) or 0)
        if size:
            self._size = size
        downloaded = int(snapshot.get("downloaded", 0) or 0)

        code = int(snapshot.get("statusCode", 0) or 0)
        self._phase = _MAGNET_STATUS_LABELS.get(
            code, str(snapshot.get("status", "processing"))
        )

        # AllDebrid only includes the ``seeders`` field while it is actively
        # downloading an uncached (or partially cached) torrent. Its absence
        # simply means "no live swarm figure for this phase" (queued, ready,
        # compressing, ...), so the peer line is presence-driven: it appears
        # with the swarm and disappears once the swarm figure is gone.
        if "seeders" in snapshot:
            try:
                self._seeders = int(snapshot.get("seeders", 0) or 0)
                self._has_seeders = True
            except (TypeError, ValueError):
                self._has_seeders = False
        else:
            self._has_seeders = False

        speed = snapshot.get("downloadSpeed")
        if speed is None:
            elapsed = now - self._last_time
            # Only derive a delta speed over a meaningful window; a burst of
            # snapshots arriving <1s apart would divide by a near-zero dt and
            # print an absurd TB/s figure.
            if elapsed >= 1.0 and downloaded >= self._last_downloaded:
                self._speed = (downloaded - self._last_downloaded) / elapsed
        else:
            try:
                self._speed = float(speed)
            except (TypeError, ValueError):
                self._speed = 0

        self._downloaded = downloaded
        self._last_downloaded = downloaded
        self._last_time = now

    def gid(self):
        return self._gid

    def name(self):
        return self._name or self.listener.name or "AllDebrid"

    def phase(self):
        return self._phase

    def size(self):
        return get_readable_file_size(self._size)

    def progress_raw(self):
        try:
            return self._downloaded / self._size * 100
        except ZeroDivisionError:
            return 0

    def progress(self):
        return f"{round(self.progress_raw(), 2)}%"

    def processed_bytes(self):
        return get_readable_file_size(self._downloaded)

    def speed(self):
        return f"{get_readable_file_size(self._speed)}/s"

    def has_peers(self):
        return self._has_seeders

    def seeders_num(self):
        return self._seeders

    def leechers_num(self):
        # The v4.1 magnet status payload exposes active ``seeders`` only; it
        # carries no leecher figure, so we report it as unknown rather than
        # inventing a 0.
        return "-"

    def eta(self):
        try:
            seconds = (self._size - self._downloaded) / self._speed
            return get_readable_time(int(seconds)) or "-"
        except Exception:
            return "-"

    def status(self):
        return MirrorStatus.STATUS_ALLDEBRID

    def task(self):
        return self

    async def cancel_task(self):
        self.listener.is_cancelled = True
