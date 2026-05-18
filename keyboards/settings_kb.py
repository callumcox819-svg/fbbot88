from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.preset_categories import COUNTRY_LOCATIONS, MAX_CATEGORIES_PER_USER, PRESET_CATEGORIES


def settings_menu_kb_with_country(country: str | None, *, active_cats: int = 0) -> InlineKeyboardMarkup:
    ch_on = country == "ch"
    fi_on = country == "fi"
    cat_hint = f" ({active_cats}/{MAX_CATEGORIES_PER_USER})" if active_cats else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Прокси", callback_data="set:proxies")],
            [
                InlineKeyboardButton(
                    text=f"✨ Готовые категории{cat_hint}",
                    callback_data="set:cat:preset",
                )
            ],
            [InlineKeyboardButton(text="🔗 Своя ссылка категории", callback_data="set:cat:custom")],
            [InlineKeyboardButton(text="📋 Мои активные категории", callback_data="set:cat:list")],
            [InlineKeyboardButton(text="📦 Кол-во объявлений (JSON)", callback_data="set:json_limit")],
            [
                InlineKeyboardButton(text=country_btn_label("ch", ch_on), callback_data="set:country:ch"),
                InlineKeyboardButton(text=country_btn_label("fi", fi_on), callback_data="set:country:fi"),
            ],
            [InlineKeyboardButton(text="◀️ Закрыть", callback_data="set:close")],
        ]
    )


def country_btn_label(code: str, active: bool) -> str:
    base = COUNTRY_LOCATIONS[code]["label"]
    return f"🟢 {base}" if active else f"🔴 {base}"


def preset_cat_btn_label(label: str, active: bool) -> str:
    return f"{'🟢' if active else '🔴'} {label}"


def preset_categories_kb(active_keys: set[str]) -> InlineKeyboardMarkup:
    """Тумблеры: 🟢 — будет парситься, 🔴 — выключено."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for cat in PRESET_CATEGORIES:
        on = cat.key in active_keys
        row.append(
            InlineKeyboardButton(
                text=preset_cat_btn_label(cat.label, on),
                callback_data=f"set:cat:toggle:{cat.key}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    n = len(active_keys)
    rows.append(
        [
            InlineKeyboardButton(
                text=f"✅ Активно: {n}/{MAX_CATEGORIES_PER_USER}",
                callback_data="set:cat:noop",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="◀️ Назад в настройки", callback_data="set:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
