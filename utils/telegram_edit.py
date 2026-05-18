from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message


async def edit_text_keep_markup(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup,
    parse_mode: str = "HTML",
) -> None:
    """edit_text всегда с reply_markup — иначе Telegram снимает inline-клавиатуру."""
    try:
        await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            try:
                await message.edit_reply_markup(reply_markup=reply_markup)
            except TelegramBadRequest:
                pass
            return
        raise
