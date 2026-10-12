"""AllDebrid storage housekeeping.

AllDebrid keeps every magnet you ever add in the account history - cached
torrents stay "Ready" forever (no automatic expiry), and they count against
the per-account magnet limit. Left unchecked the history fills up and new
tasks start failing with ``MAGNET_TOO_MANY``.

The bot previously only removed a magnet when a task FAILED (see the error
handlers). This module closes the loop for the successful path too: as soon as
a task finishes uploading, its magnet is no longer needed and is deleted
immediately.

The cleanup is best-effort and never raises into the caller: a housekeeping
failure must not fail a user's task.

Safety: a magnet id that is still referenced by another live task is never
deleted, so two concurrent tasks that happen to share the same cached torrent
(info-hash) do not break each other. Deletion is idempotent on AllDebrid's
side, so races are harmless.
"""

from asyncio import create_task, sleep

from .... import LOGGER, task_dict, task_dict_lock
from ....core.config_manager import Config
from .alldebrid_resolver import delete_magnet


async def _magnet_ids_in_use() -> set[int]:
    """Return the AllDebrid magnet ids referenced by live tasks."""
    in_use: set[int] = set()
    async with task_dict_lock:
        for status in task_dict.values():
            listener = getattr(status, "listener", None)
            magnet_id = getattr(listener, "_alldebrid_magnet_id", 0) or 0
            if magnet_id:
                in_use.add(int(magnet_id))
    return in_use


async def cleanup_after_task(listener) -> None:
    """Delete the AllDebrid magnet owned by a finished task.

    Runs in the background (spawned via ``create_task``) so a slow API call
    never delays the user-facing completion message. The magnet is released
    only when no other live task still points at it.
    """
    if not Config.ALLDEBRID_AUTO_DELETE:
        return
    magnet_id = getattr(listener, "_alldebrid_magnet_id", 0) or 0
    if not magnet_id:
        return
    # Clear it on the listener first so a duplicate completion event never
    # tries to act on the same id twice.
    listener._alldebrid_magnet_id = 0
    magnet_id = int(magnet_id)
    try:
        # Small grace window so a sibling task that shares the same cached
        # torrent has time to register before we look at the live set.
        await sleep(5)
        if magnet_id in await _magnet_ids_in_use():
            LOGGER.info(
                f"AllDebrid magnet {magnet_id} still used by another task; "
                "deferring cleanup"
            )
            return
        await delete_magnet(magnet_id)
    except Exception as exc:
        LOGGER.warning(f"AllDebrid post-task cleanup failed for {magnet_id}: {exc}")


def schedule_cleanup_after_task(listener) -> None:
    """Fire-and-forget wrapper so callers need not await the cleanup."""
    try:
        create_task(cleanup_after_task(listener))
    except Exception as exc:
        LOGGER.warning(f"Could not schedule AllDebrid cleanup: {exc}")


__all__ = [
    "cleanup_after_task",
    "schedule_cleanup_after_task",
]
