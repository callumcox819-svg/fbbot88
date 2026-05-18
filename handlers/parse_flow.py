from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import Session
from keyboards.main_menu import main_menu_kb
from models import User
from parser.account_token import is_account_token_line, parse_account_token
from services.parsing_jobs import is_parsing, start_parsing
from services.users import get_or_create_user

router = Router()


class ParseStates(StatesGroup):
    waiting_account_token = State()


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

    from services.categories import list_user_categories

    async with Session() as session:
        cats = await list_user_categories(session, user.id)

    if not cats:
        await message.answer("Сначала выбери категории в ⚙️ Настройки → 📂 Категории.")
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
    status_msg = await message.answer(
        "⏳ Запуск…",
        reply_markup=main_menu_kb(is_admin=db_user.is_admin),
        disable_web_page_preview=True,
    )

    async def on_status(text: str) -> None:
        try:
            await status_msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            pass

    try:
        await start_parsing(
            message.bot,
            telegram_id=telegram_id,
            user_id=db_user.id,
            token_raw=token_raw,
            on_status=on_status,
        )
    except RuntimeError as e:
        await status_msg.edit_text(str(e))
