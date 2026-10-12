"""AllDebrid storage housekeeping.

AllDebrid keeps every magnet you ever add in the account history - cached
torrents stay "Ready" forever (no automatic expiry), and they count against
the per-account magnet limit. Left unchecked the history fills up and new
tasks start failing with ``MAGNET_TOO_MANY``.

The bot previously only removed a magnet when a task FAILED (see the error
handlers). This module closes the loop for the successful path too, with two
complementary mechanisms:

1. :func:`cleanup_after_task` - called once a task finished uploading. The
   magnet for that task is no longer needed, so it is deleted immediately.
2. :func:`sweep_expired` - a periodic sweep that deletes any magnet still
   sitting in the history past a retention window. This catches leftovers
   from before this feature existed (a large pre-existing backlog on first
   run) as well as magnets whose immediate delete failed (network hiccup).

Both are best-effort and never raise into the caller: a housekeeping failure
must not fail a user's task.

Safety: a magnet id that is still referenced by another live task is never
deleted, so two concurrent tasks that happen to share the same cached torrent
(info-hash) do not break each other. Deletion is idempotent on AllDebrid's
side, so races are harmless.
"""

from asyncio import create_task, sleep
from time import time

from .... import LOGGER, task_dict, task_dict_lock
from ....core.config_manager import Config
from .alldebrid_resolver import (
    _MAGNET_STATUS_READY,
    delete_magnet,
    list_magnets,
)

_SECONDS_PER_DAY = 86400
# First sweep waits a little after startup so the account is not hammered
# while the bot finishes booting and any resumed tasks register themselves.
_STARTUP_DELAY = 300
# Re-check the account history every 6 hours.
_SWEEP_INTERVAL = 6 * 3600


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
    # Clear it on the listener first so a later sweep (or a duplicate
    # completion event) never tries to act on the same id twice.
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


async def sweep_expired() -> None:
    """Delete Ready magnets older than ``ALLDEBRID_RETENTION_DAYS``."""
    retention_days = Config.ALLDEBRID_RETENTION_DAYS
    if not retention_days or retention_days <= 0:
        return
    try:
        magnets = await list_magnets()
    except Exception as exc:
        LOGGER.warning(f"AllDebrid retention sweep could not list magnets: {exc}")
        return
    if not magnets:
        return

    in_use = await _magnet_ids_in_use()
    cutoff = time() - retention_days * _SECONDS_PER_DAY
    removed = 0
    for magnet in magnets:
        magnet_id = magnet.get("id")
        if not magnet_id or int(magnet_id) in in_use:
            continue
        # Only expire fully cached torrents; a magnet that is still
        # processing (or already dead) is left for the API to settle.
        if int(magnet.get("statusCode", 0) or 0) != _MAGNET_STATUS_READY:
            continue
        # ``completionDate`` is absent on some entries - fall back to the
        # upload timestamp so an undated magnet is still bounded by age.
        stamp = magnet.get("completionDate") or magnet.get("uploadDate") or 0
        try:
            stamp = int(stamp)
        except (TypeError, ValueError):
            continue
        if not stamp or stamp >= cutoff:
            continue
        if await delete_magnet(int(magnet_id)):
            removed += 1
            # Throttle so a large first-run backlog (hundreds of stale
            # magnets) does not hammer the API and trip rate limits.
            await sleep(0.5)

    if removed:
        LOGGER.info(f"AllDebrid retention sweep removed {removed} old magnet(s)")


async def _sweep_loop() -> None:
    """Run :func:`sweep_expired` forever at a fixed interval.

    A dedicated asyncio loop is used instead of the shared APScheduler
    instance: that instance is owned by the RSS module and is SHUT DOWN
    whenever RSS_CHAT is not configured, which would silently stop the
    sweep for every bot that does not use RSS feeds.
    """
    await sleep(_STARTUP_DELAY)
    while True:
        try:
            await sweep_expired()
        except Exception as exc:  # never let the loop die
            LOGGER.warning(f"AllDebrid retention sweep error: {exc}")
        await sleep(_SWEEP_INTERVAL)


def init_retention_sweep() -> None:
    """Start the background retention sweep if it is enabled."""
    if Config.ALLDEBRID_RETENTION_DAYS <= 0:
        return
    try:
        create_task(_sweep_loop())
    except Exception as exc:
        LOGGER.warning(f"Could not start AllDebrid retention sweep: {exc}")


__all__ = [
    "cleanup_after_task",
    "schedule_cleanup_after_task",
    "sweep_expired",
    "init_retention_sweep",
]
