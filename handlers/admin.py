from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import Session
from services.users import admin_stats, grant_access, revoke_access

router = Router()


class AdminStates(StatesGroup):
    waiting_grant_id = State()
    waiting_revoke_id = State()


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
            [InlineKeyboardButton(text="✅ Выдать доступ", callback_data="adm:grant")],
            [InlineKeyboardButton(text="🚫 Убрать доступ", callback_data="adm:revoke")],
            [InlineKeyboardButton(text="◀️ Закрыть", callback_data="adm:close")],
        ]
    )


async def open_admin_panel(message: Message) -> None:
    await message.answer("👑 <b>Админ панель</b>", parse_mode="HTML", reply_markup=admin_kb())


@router.callback_query(F.data == "adm:close")
async def adm_close(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.delete()


def _admin_only(db_user) -> bool:
    return bool(db_user and db_user.is_admin)


@router.callback_query(F.data == "adm:stats")
async def adm_stats(callback: CallbackQuery, db_user) -> None:
    if not _admin_only(db_user):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with Session() as session:
        s = await admin_stats(session)
    text = (
        "<b>📊 Статистика</b>\n\n"
        f"👥 Пользователей: <b>{s['total_users']}</b>\n"
        f"✅ С доступом: <b>{s['with_access']}</b>\n"
        f"🚫 Заблокировано: <b>{s['banned']}</b>\n"
        f"▶️ Всего запусков парсинга: <b>{s['total_parses']}</b>\n"
        f"📦 Собрано объявлений: <b>{s['total_listings']}</b>\n"
        f"📅 Запусков сегодня: <b>{s['runs_today']}</b>"
    )
    await callback.answer()
    await callback.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "adm:grant")
async def adm_grant_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_grant_id)
    await callback.message.answer("Telegram ID пользователя для выдачи доступа:")


@router.message(AdminStates.waiting_grant_id)
async def adm_grant_do(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой ID.")
        return
    tid = int(raw)
    async with Session() as session:
        ok = await grant_access(session, tid)
    await state.clear()
    if ok:
        await message.answer(f"✅ Доступ выдан: <code>{tid}</code>", parse_mode="HTML")
        try:
            await message.bot.send_message(tid, "✅ Вам выдан доступ к боту. Нажмите /start")
        except Exception:
            pass
    else:
        await message.answer("Пользователь не найден. Он должен нажать /start.")


@router.callback_query(F.data == "adm:revoke")
async def adm_revoke_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_revoke_id)
    await callback.message.answer("Telegram ID для отзыва доступа:")


@router.message(AdminStates.waiting_revoke_id)
async def adm_revoke_do(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой ID.")
        return
    tid = int(raw)
    async with Session() as session:
        ok = await revoke_access(session, tid)
    await state.clear()
    if ok:
        await message.answer(f"🚫 Доступ снят: <code>{tid}</code>", parse_mode="HTML")
    else:
        await message.answer("Не удалось (нет пользователя или это админ).")
