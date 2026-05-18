from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from database import Session
from services.users import get_or_create_user, user_has_access

_DENY = (
    "🔒 Бот закрыт.\n\n"
    "Доступ выдаёт администратор.\n"
    "Напиши админу свой ID: <code>{uid}</code>"
)

# Без проверки доступа
_PUBLIC = {"/start"}


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None
        if isinstance(event, Message) and event.from_user:
            user = event.from_user
        elif isinstance(event, CallbackQuery) and event.from_user:
            user = event.from_user

        if not user:
            return await handler(event, data)

        if isinstance(event, Message) and event.text:
            cmd = (event.text.split()[0] or "").split("@")[0].lower()
            if cmd in _PUBLIC:
                return await handler(event, data)

        async with Session() as session:
            db_user = await get_or_create_user(session, user.id, user.username)

        if user_has_access(db_user):
            data["db_user"] = db_user
            return await handler(event, data)

        text = _DENY.format(uid=user.id)
        if isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
            if event.message:
                await event.message.answer(text, parse_mode="HTML")
            return None
        if isinstance(event, Message):
            await event.answer(text, parse_mode="HTML")
        return None
