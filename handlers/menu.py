from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from database import Session
from keyboards.main_menu import BTN_ADMIN, BTN_SETTINGS, BTN_START, BTN_STOP, main_menu_kb
from services.parsing_jobs import is_parsing, request_stop
from services.users import get_or_create_user, user_has_access

router = Router()


async def _send_main_menu(message: Message, *, is_admin: bool) -> None:
    await message.answer(
        "👋 <b>FB Marketplace Parser</b>\n\n"
        "▶️ <b>Старт поиска</b> — токен аккаунта FB → сбор JSON\n"
        "⚙️ <b>Настройки</b> — прокси, страна (CH/FI), лимит JSON\n"
        "⏹ <b>Стоп поиск</b> — остановить парсинг\n\n"
        "<i>Меню всегда можно открыть командой /start</i>",
        parse_mode="HTML",
        reply_markup=main_menu_kb(is_admin=is_admin),
    )


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message) -> None:
    tg_id = message.from_user.id
    async with Session() as session:
        user = await get_or_create_user(session, tg_id, message.from_user.username)

    if not user_has_access(user):
        await message.answer(
            f"🔒 Бот закрыт.\n\n"
            f"Твой ID: <code>{tg_id}</code>\n"
            "Ожидай выдачи доступа от админа.\n\n"
            "<i>После доступа нажми /start — появятся кнопки внизу.</i>",
            parse_mode="HTML",
        )
        return

    await _send_main_menu(message, is_admin=user.is_admin)


@router.message(F.text == BTN_START)
async def btn_start(message: Message, state: FSMContext) -> None:
    from handlers.parse_flow import open_start_flow

    await open_start_flow(message, state)


@router.message(F.text == BTN_SETTINGS)
async def btn_settings(message: Message, db_user) -> None:
    from handlers.settings_handler import open_settings

    await open_settings(message, db_user)


@router.message(F.text == BTN_ADMIN)
async def btn_admin(message: Message, db_user) -> None:
    from handlers.admin import open_admin_panel

    if not db_user.is_admin:
        await message.answer("Только для администратора.")
        return
    await open_admin_panel(message)


@router.message(F.text == BTN_STOP)
async def btn_stop(message: Message) -> None:
    tg_id = message.from_user.id
    if not is_parsing(tg_id):
        await message.answer("Парсинг не запущен.")
        return
    request_stop(tg_id)
    await message.answer("⏹ Останавливаю парсинг…")
