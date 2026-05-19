import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config
from database import init_db
from handlers import admin, menu, parse_flow, settings_handler
from middlewares.access import AccessMiddleware
from services.bot_setup import configure_bot_ui

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()
    logger.info("БД готова")

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())

    dp.include_router(menu.router)
    dp.include_router(parse_flow.router)
    dp.include_router(settings_handler.router)
    dp.include_router(admin.router)

    await configure_bot_ui(bot)
    logger.info("Бот запущен (админы: %s)", config.admin_ids or "—")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
