from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.preset_categories import COUNTRY_LOCATIONS


def settings_menu_kb_with_country(country: str | None) -> InlineKeyboardMarkup:
    ch_on = country == "ch"
    fi_on = country == "fi"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Прокси", callback_data="set:proxies")],
            [InlineKeyboardButton(text="🚫 Чёрный список продавцов", callback_data="set:chs")],
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


def json_limit_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="set:json_limit:edit")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="set:back")],
        ]
    )


def json_limit_menu_text(current: int) -> str:
    return (
        "📦 <b>Количество объявлений в JSON</b>\n\n"
        f"Текущее количество для JSON: <b>{current}</b>"
    )
