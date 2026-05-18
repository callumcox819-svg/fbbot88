import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import Session
from data.preset_categories import parse_categories_for_country
from keyboards.main_menu import main_menu_kb
from models import User
from parser.account_token import is_account_token_line, parse_account_token
from services.parsing_jobs import is_parsing, refresh_parse_status, start_parsing
from services.users import get_or_create_user

router = Router()
logger = logging.getLogger(__name__)


class ParseStates(StatesGroup):
    waiting_account_token = State()


def _parse_progress_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить статистику", callback_data="parse:refresh")],
        ]
    )


def _token_kb(has_last: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_last:
        rows.append(
            [InlineKeyboardButton(text="♻️ Использовать последний токен", callback_data="parse:last_token")]
        )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="parse:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def open_start_flow(message: Message, state: FSMContext | None = None) -> None:
    tg_id = message.from_user.id
    if is_parsing(tg_id):
        await message.answer("Уже идёт парсинг. Нажми ⏹ Стоп поиск.")
        return

    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        has_last = bool(user.last_account_token)

    if not user.country or user.country not in ("ch", "fi"):
        await message.answer(
            "Сначала выбери страну в ⚙️ Настройки:\n"
            "🇨🇭 Швейцария или 🇫🇮 Финляндия — категории подставятся автоматически."
        )
        return

    text = (
        "🔑 <b>Токен аккаунта Facebook</b>\n\n"
        "Вставь строку аккаунта (как в VOID):\n"
        "<code>uid|xs|datr|fr|access_token</code>\n\n"
        "<i>Пример: 61588728046344|1%3ABchMU...|LyT_...|LyT_...|1MOxHY...</i>"
    )
    if state:
        await state.set_state(ParseStates.waiting_account_token)
    await message.answer(text, parse_mode="HTML", reply_markup=_token_kb(has_last))


@router.callback_query(F.data == "parse:refresh")
async def parse_refresh_stats(callback: CallbackQuery) -> None:
    tg_id = callback.from_user.id
    if not is_parsing(tg_id):
        await callback.answer("Парсинг не запущен", show_alert=True)
        return
    ok = await refresh_parse_status(tg_id)
    await callback.answer("Обновлено" if ok else "Нет данных", show_alert=not ok)


@router.callback_query(F.data == "parse:cancel")
async def parse_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("Отменено.")


@router.callback_query(F.data == "parse:last_token")
async def parse_last_token(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    from sqlalchemy import select

    async with Session() as session:
        user = (await session.execute(select(User).where(User.id == db_user.id))).scalar_one()
        saved = user.last_account_token
    if not saved:
        await callback.message.answer("Нет сохранённого токена аккаунта.")
        return
    await state.clear()
    await _launch_parse(callback.message, callback.from_user.id, db_user, saved)


@router.message(ParseStates.waiting_account_token)
async def on_token_input(message: Message, state: FSMContext, db_user: User) -> None:
    raw = (message.text or "").strip()
    if not is_account_token_line(raw):
        await message.answer("❌ Неверный формат. Нужна строка uid|xs|datr|fr|token")
        return
    try:
        parse_account_token(raw)
    except Exception as e:
        await message.answer(f"❌ {e}")
        return

    await state.clear()
    await _launch_parse(message, message.from_user.id, db_user, raw)


async def _launch_parse(message: Message, telegram_id: int, db_user: User, token_raw: str) -> None:
    # Статус без reply-клавиатуры — иначе edit_text часто не работает
    async with Session() as session:
        from sqlalchemy import select
        from models import User as U

        u = (await session.execute(select(U).where(U.id == db_user.id))).scalar_one()
        lim = int(u.json_limit or 50)
        country = u.country
        if country not in ("ch", "fi"):
            await message.answer(
                "Сначала выбери 🇨🇭 или 🇫🇮 в ⚙️ Настройки.",
                parse_mode="HTML",
            )
            return
        n_cat = len(parse_categories_for_country(country))

    status_msg = await message.answer(
        f"🔎 <b>В JSON: 0/{lim}</b>\n"
        f"<i>Парсинг: {n_cat} категорий Marketplace. Статистика обновляется по ходу.</i>\n"
        f"<i>⏹ Стоп поиск — отменить.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_parse_progress_kb(),
    )

    async def on_status(text: str) -> None:
        try:
            await status_msg.edit_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_parse_progress_kb(),
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logger.warning("status edit failed: %s", e)
            try:
                await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                pass
        except Exception:
            logger.exception("status update failed")

    try:
        await start_parsing(
            message.bot,
            telegram_id=telegram_id,
            user_id=db_user.id,
            token_raw=token_raw,
            on_status=on_status,
        )
    except RuntimeError as e:
        await status_msg.edit_text(str(e), parse_mode="HTML")
