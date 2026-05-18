from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from database import Session
from data.preset_categories import presets_for_country
from keyboards.settings_kb import (
    json_limit_menu_kb,
    json_limit_menu_text,
    settings_menu_kb_with_country,
)
from models import User
from services import proxies as proxy_svc
from utils.telegram_edit import edit_text_keep_markup

router = Router()


class SettingsStates(StatesGroup):
    waiting_proxies = State()
    waiting_json_limit = State()


def _country_label(code: str | None) -> str:
    if code == "ch":
        return "Швейцария"
    if code == "fi":
        return "Финляндия"
    return "не выбрана"


def _settings_text(db_user: User) -> str:
    country = db_user.country
    lines = [
        "⚙️ <b>Настройки</b>\n",
        f"📦 Лимит JSON: <b>{db_user.json_limit or 50}</b>",
        f"🌍 Страна: <b>{_country_label(country)}</b>\n",
    ]
    if country in ("ch", "fi"):
        cats = presets_for_country(country)
        names = ", ".join(c.label for c in cats)
        lines.append(
            f"<i>Парсинг автоматически по <b>{len(cats)}</b> категориям Marketplace:</i>\n"
            f"{names}"
        )
    else:
        lines.append(
            "<i>Выбери 🇨🇭 или 🇫🇮 — все категории страны подставятся сами.</i>"
        )
    return "\n".join(lines)


async def open_settings(message: Message, db_user: User | None = None) -> None:
    if db_user is None:
        async with Session() as session:
            res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
            db_user = res.scalar_one()
    await message.answer(
        _settings_text(db_user),
        parse_mode="HTML",
        reply_markup=settings_menu_kb_with_country(db_user.country),
    )


@router.callback_query(F.data == "set:close")
async def set_close(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.delete()


@router.callback_query(F.data == "set:back")
async def set_back(callback: CallbackQuery, db_user: User) -> None:
    await callback.answer()
    await callback.message.edit_text(
        _settings_text(db_user),
        parse_mode="HTML",
        reply_markup=settings_menu_kb_with_country(db_user.country),
    )


@router.callback_query(F.data.startswith("set:country:"))
async def toggle_country(callback: CallbackQuery, db_user: User) -> None:
    code = callback.data.split(":")[-1]
    async with Session() as session:
        user = (await session.execute(select(User).where(User.id == db_user.id))).scalar_one()
        if user.country == code:
            user.country = None
        else:
            user.country = code
        await session.commit()
        country = user.country

    await callback.answer("Обновлено")
    await callback.message.edit_text(
        _settings_text(user),
        parse_mode="HTML",
        reply_markup=settings_menu_kb_with_country(country),
    )


@router.callback_query(F.data == "set:json_limit")
async def json_limit_menu(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await state.clear()
    await callback.answer()
    current = int(db_user.json_limit or 50)
    await edit_text_keep_markup(
        callback.message,
        json_limit_menu_text(current),
        reply_markup=json_limit_menu_kb(),
    )


@router.callback_query(F.data == "set:json_limit:edit")
async def json_limit_edit(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_json_limit)
    current = int(db_user.json_limit or 50)
    await callback.message.answer(
        f"Введи новое число объявлений в JSON (1–500).\n"
        f"Сейчас: <b>{current}</b>\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
    )


@router.message(SettingsStates.waiting_json_limit)
async def save_json_limit(message: Message, state: FSMContext, db_user: User) -> None:
    if (message.text or "").strip().lower() in ("/cancel", "отмена"):
        await state.clear()
        current = int(db_user.json_limit or 50)
        await message.answer(
            json_limit_menu_text(current),
            parse_mode="HTML",
            reply_markup=json_limit_menu_kb(),
        )
        return

    try:
        n = int((message.text or "").strip())
        if not 1 <= n <= 500:
            raise ValueError
    except ValueError:
        await message.answer("Введи число от 1 до 500.")
        return

    async with Session() as session:
        user = (await session.execute(select(User).where(User.id == db_user.id))).scalar_one()
        user.json_limit = n
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Сохранено.\n\n{json_limit_menu_text(n)}",
        parse_mode="HTML",
        reply_markup=json_limit_menu_kb(),
    )


@router.callback_query(F.data == "set:proxies")
async def proxies_menu(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    async with Session() as session:
        rows = await proxy_svc.list_proxies(session, db_user.id)
    lines = [
        f"🌐 <b>Прокси</b> ({len(rows)})",
        "",
        "Пришли список (по одному на строку):",
        "<code>host:port:user:pass</code>",
    ]
    for p in rows[:15]:
        lines.append(f"• <code>{p.host}:{p.port}</code> (id={p.id})")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="set:proxy:add")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="set:back")],
        ]
    )
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "set:proxy:add")
async def proxy_add(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_proxies)
    await callback.message.answer("Вставь прокси (каждый с новой строки). /cancel — отмена.")


@router.message(SettingsStates.waiting_proxies)
async def proxy_save(message: Message, state: FSMContext, db_user: User) -> None:
    if (message.text or "").strip().lower() == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    lines = [l.strip() for l in (message.text or "").splitlines() if l.strip()]
    async with Session() as session:
        added, failed = await proxy_svc.add_proxies(session, db_user.id, lines)
    await state.clear()
    await message.answer(f"✅ Добавлено: {added}, ошибок: {failed}")
