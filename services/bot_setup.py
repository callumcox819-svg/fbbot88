"""Регистрация команд и кнопки меню в Telegram (чтобы не вводить /start вручную)."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonCommands

logger = logging.getLogger(__name__)

BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="🏠 Главное меню и кнопки"),
    BotCommand(command="menu", description="🏠 То же, что /start"),
)


async def configure_bot_ui(bot: Bot) -> None:
    """Список команд слева от поля ввода + кнопка «Меню»."""
    await bot.set_my_commands(list(BOT_COMMANDS), scope=BotCommandScopeAllPrivateChats())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info("Telegram commands and menu button configured")
