from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError

from app.bot.handlers import configure_bot_ui, router
from app.bot.keyboards import main_menu
from app.config import settings
from app.db.session import init_db, sessionmaker
from app.services.bootstrap import bootstrap_defaults
from app.services.login_email_protection import recover_incomplete_events
from app.tg.client_pool import ClientPool
from app.webapp.server import start_webapp

logger = logging.getLogger(__name__)


async def restore_admin_keyboards(bot: Bot) -> None:
    """Re-send the canonical reply keyboard after every successful service start."""
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                "服务已启动。主菜单可通过输入框旁的小键盘按钮随时展开或收起。",
                reply_markup=main_menu(),
            )
        except TelegramAPIError:
            logger.warning("Failed to restore reply keyboard for admin %s", admin_id, exc_info=True)


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    await bootstrap_defaults()
    recovered_events = await recover_incomplete_events(sessionmaker)
    if recovered_events:
        logger.warning("Marked %s incomplete login email protection events as interrupted", recovered_events)
    logger.info(
        "Login email protection: enabled=%s, credentials_configured=%s, domains=%s",
        settings.login_email_protection_enabled,
        bool(settings.login_email_gmail_username and settings.login_email_gmail_app_password),
        len(settings.login_email_alias_domains),
    )

    bot = Bot(token=settings.bot_token)
    try:
        await configure_bot_ui(bot)
    except TelegramAPIError:
        logger.warning("Failed to configure Telegram command menu", exc_info=True)
    pool = ClientPool(sessionmaker=sessionmaker, bot=bot)
    dp = Dispatcher(sessionmaker=sessionmaker, client_pool=pool)
    dp.include_router(router)

    web_runner = await start_webapp(sessionmaker, pool)
    if web_runner is not None:
        logger.info("Mini App server started on %s:%s", settings.mini_app_host, settings.mini_app_port)

    await pool.start_service_monitor()
    await restore_admin_keyboards(bot)
    try:
        await dp.start_polling(bot)
    finally:
        if web_runner is not None:
            await web_runner.cleanup()
        await pool.stop_service_monitor()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
