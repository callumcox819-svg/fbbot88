from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from database import Session
from data.preset_categories import MAX_CATEGORIES_PER_USER
from keyboards.settings_kb import (
    json_limit_menu_kb,
    json_limit_menu_text,
    preset_categories_kb,
    settings_menu_kb_with_country,
)
from models import User
from services import categories as cat_svc
from services import proxies as proxy_svc
from utils.telegram_edit import edit_text_keep_markup

router = Router()


def _preset_categories_text(active_count: int) -> str:
    return (
        "✨ <b>Готовые категории Marketplace</b>\n\n"
        f"Выбрано: <b>{active_count}/{MAX_CATEGORIES_PER_USER}</b>\n"
        "Нажимай категории — окно <b>не закроется</b>.\n"
        "Когда закончишь — <b>◀️ Назад</b>.\n\n"
        "<i>🟢 — в парсинг · 🔴 — выкл.</i>"
    )


class SettingsStates(StatesGroup):
    waiting_proxies = State()
    waiting_json_limit = State()
    waiting_custom_url = State()


async def open_settings(message: Message, db_user: User | None = None) -> None:
    if db_user is None:
        async with Session() as session:
            res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
            db_user = res.scalar_one()
    async with Session() as session:
        n_cats = await cat_svc.count_active_categories(session, db_user.id)
    country = db_user.country
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"📦 Лимит JSON: <b>{db_user.json_limit or 50}</b>\n"
        f"📂 Активных категорий: <b>{n_cats}/{MAX_CATEGORIES_PER_USER}</b>\n"
        f"🌍 Страна: <b>{_country_label(country)}</b>\n\n"
        "<i>🟢 CH/FI — парсинг по всей стране (все регионы), 🔴 — выкл.</i>"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=settings_menu_kb_with_country(country, active_cats=n_cats),
    )


def _country_label(code: str | None) -> str:
    if code == "ch":
        return "Швейцария"
    if code == "fi":
        return "Финляндия"
    return "не выбрана"


@router.callback_query(F.data == "set:close")
async def set_close(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.delete()


@router.callback_query(F.data == "set:back")
async def set_back(callback: CallbackQuery, db_user: User) -> None:
    await callback.answer()
    async with Session() as session:
        n_cats = await cat_svc.count_active_categories(session, db_user.id)
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\n"
        f"📂 Активных категорий: <b>{n_cats}/{MAX_CATEGORIES_PER_USER}</b>\n"
        "<i>🟢 — включено</i>",
        parse_mode="HTML",
        reply_markup=settings_menu_kb_with_country(db_user.country, active_cats=n_cats),
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
    await callback.message.edit_reply_markup(reply_markup=settings_menu_kb_with_country(country))


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
    lines = [f"🌐 <b>Прокси</b> ({len(rows)})", "", "Пришли список (по одному на строку):", "<code>host:port:user:pass</code>"]
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


@router.callback_query(F.data == "set:cat:noop")
async def cat_noop(callback: CallbackQuery) -> None:
    await callback.answer(f"Можно выбрать до {MAX_CATEGORIES_PER_USER} категорий", show_alert=True)


@router.callback_query(F.data == "set:cat:preset")
async def cat_preset(callback: CallbackQuery, db_user: User) -> None:
    await callback.answer()
    async with Session() as session:
        active = await cat_svc.get_active_preset_keys(session, db_user.id)
    await edit_text_keep_markup(
        callback.message,
        _preset_categories_text(len(active)),
        reply_markup=preset_categories_kb(active),
    )


@router.callback_query(F.data.startswith("set:cat:toggle:"))
async def cat_toggle(callback: CallbackQuery, db_user: User) -> None:
    key = callback.data.split(":")[-1]
    async with Session() as session:
        active, err = await cat_svc.toggle_preset_category(session, db_user.id, key)

    if err:
        await callback.answer(err, show_alert=True)
        return

    from data.preset_categories import PRESET_BY_KEY

    preset = PRESET_BY_KEY.get(key)
    name = (preset.label if preset else key).replace("🟢 ", "").replace("🔴 ", "")
    on = key in active
    await callback.answer(f"{'🟢' if on else '🔴'} {name}")

    await edit_text_keep_markup(
        callback.message,
        _preset_categories_text(len(active)),
        reply_markup=preset_categories_kb(active),
    )


@router.callback_query(F.data == "set:cat:custom")
async def cat_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_custom_url)
    await callback.message.answer(
        "Ссылка на категорию Marketplace, например:\n"
        "<code>https://www.facebook.com/marketplace/category/electronics</code>",
        parse_mode="HTML",
    )


@router.message(SettingsStates.waiting_custom_url)
async def cat_custom_save(message: Message, state: FSMContext, db_user: User) -> None:
    async with Session() as session:
        err = await cat_svc.add_custom_category(session, db_user.id, message.text or "")
    await state.clear()
    if err:
        await message.answer(f"❌ {err}")
    else:
        await message.answer("✅ Категория добавлена")


@router.callback_query(F.data == "set:cat:list")
async def cat_list(callback: CallbackQuery, db_user: User) -> None:
    await callback.answer()
    async with Session() as session:
        cats = await cat_svc.list_user_categories(session, db_user.id)
    if not cats:
        await callback.message.answer(
            "Нет активных категорий.\nОткрой ⚙️ Настройки → ✨ Готовые категории."
        )
        return
    lines = [f"<b>🟢 Активные категории ({len(cats)}):</b>"]
    for c in cats:
        mark = "🟢" if c.is_preset else "🔗"
        lines.append(f"{mark} {c.label}")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")
