from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_START = "▶️ Старт поиска"
BTN_SETTINGS = "⚙️ Настройки"
BTN_ADMIN = "👑 Админ панель"
BTN_STOP = "⏹ Стоп поиск"

MAIN_BUTTONS = frozenset({BTN_START, BTN_SETTINGS, BTN_ADMIN, BTN_STOP})


def main_menu_kb(*, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_START), KeyboardButton(text=BTN_SETTINGS)],
        [KeyboardButton(text=BTN_STOP)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
