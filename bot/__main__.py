from . import LOGGER, bot_loop
from .core.telegram_manager import TgClient
from .core.config_manager import Config

Config.load()


async def _notify_alldebrid_status():
    """Probe the AllDebrid key once on startup and DM the owner if it is broken.

    Silent when the key is fine or not configured at all - only warns when the
    configured key is invalid/blocked/expired so the owner learns about it
    before the first failed task.
    """
    if not (Config.ALLDEBRID_API_KEY or "").strip():
        return
    try:
        from .helper.mirror_leech_utils.download_utils.alldebrid_resolver import (
            alldebrid_check_account,
        )

        result = await alldebrid_check_account()
    except Exception as e:  # never break startup over a health probe
        LOGGER.warning(f"AllDebrid startup check failed: {e}")
        return

    if result.get("ok"):
        LOGGER.info(
            f"AllDebrid key OK (user={result.get('username')}, "
            f"premium until {result.get('premium_until') or 'n/a'})"
        )
        return

    LOGGER.warning(
        f"AllDebrid key problem: {result.get('code')} - {result.get('message')}"
    )
    if Config.OWNER_ID and TgClient.bot is not None:
        try:
            await TgClient.bot.send_message(
                Config.OWNER_ID,
                "⚠️ <b>AllDebrid key problem</b>\n\n"
                f"{result.get('message')}\n\n"
                f"<code>{result.get('code')}</code>\n\n"
                "Update <code>ALLDEBRID_API_KEY</code> and restart, "
                "or renew the subscription.",
            )
        except Exception as e:
            LOGGER.warning(f"Could not DM owner about AllDebrid key: {e}")


async def main():
    from asyncio import gather
    from .core.startup import (
        load_settings,
        load_configurations,
        save_settings,
        update_aria2_options,
        update_nzb_options,
        update_qb_options,
        update_variables,
    )

    await load_settings()

    await gather(TgClient.start_bot(), TgClient.start_user())
    await gather(load_configurations(), update_variables())

    from .core.torrent_manager import TorrentManager

    await TorrentManager.initiate()
    await gather(
        update_qb_options(),
        update_aria2_options(),
        update_nzb_options(),
    )
    from .helper.ext_utils.files_utils import clean_all
    from .core.jdownloader_booter import jdownloader
    from .helper.ext_utils.telegraph_helper import telegraph
    from .helper.mirror_leech_utils.rclone_utils.serve import rclone_serve_booter
    from .modules import (
        initiate_search_tools,
        get_packages_version,
        restart_notification,
    )

    await gather(
        save_settings(),
        jdownloader.boot(),
        clean_all(),
        initiate_search_tools(),
        get_packages_version(),
        restart_notification(),
        telegraph.create_account(),
        rclone_serve_booter(),
        _notify_alldebrid_status(),
    )


bot_loop.run_until_complete(main())

from .helper.ext_utils.bot_utils import create_help_buttons
from .helper.listeners.aria2_listener import add_aria2_callbacks
from .core.handlers import add_handlers

add_aria2_callbacks()
create_help_buttons()
add_handlers()

LOGGER.info("Bot Started!")
bot_loop.run_forever()
