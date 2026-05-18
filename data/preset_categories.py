"""Готовые категории Facebook Marketplace — пути от /marketplace/."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PresetCategory:
    key: str
    label: str
    url_path: str


# Основные разделы Marketplace (facebook.com/marketplace/category/…)
PRESET_CATEGORIES: tuple[PresetCategory, ...] = (
    PresetCategory("vehicles", "🚗 Транспорт", "category/vehicles"),
    PresetCategory("electronics", "📱 Электроника", "category/electronics"),
    PresetCategory("property_rent", "🏠 Аренда жилья", "category/propertyrentals"),
    PresetCategory("property_sale", "🏢 Продажа жилья", "category/propertyforsale"),
    PresetCategory("apparel", "👕 Одежда", "category/apparel"),
    PresetCategory("baby", "👶 Дети", "category/baby"),
    PresetCategory("toys", "🧸 Игрушки", "category/toys"),
    PresetCategory("home", "🛋 Дом", "category/home"),
    PresetCategory("furniture", "🪑 Мебель", "category/furniture"),
    PresetCategory("appliances", "🔌 Бытовая техника", "category/appliances"),
    PresetCategory("garden", "🌿 Сад", "category/garden"),
    PresetCategory("tools", "🔧 Инструменты", "category/tools"),
    PresetCategory("sports", "⚽ Спорт", "category/sports"),
    PresetCategory("hobbies", "🎨 Хобби", "category/hobbies"),
    PresetCategory("entertainment", "🎮 Развлечения", "category/entertainment"),
    PresetCategory("instruments", "🎸 Музыка", "category/instruments"),
    PresetCategory("books", "📚 Книги", "category/books"),
    PresetCategory("games", "🕹 Видеоигры", "category/games"),
    PresetCategory("pets", "🐾 Животные", "category/pets"),
    PresetCategory("health", "💊 Здоровье", "category/health"),
    PresetCategory("beauty", "💄 Красота", "category/beauty"),
    PresetCategory("jewelry", "💍 Украшения", "category/jewelry"),
    PresetCategory("bags", "👜 Сумки", "category/bags"),
    PresetCategory("autoparts", "⚙️ Автозапчасти", "category/autoparts"),
    PresetCategory("office", "📎 Офис", "category/office"),
    PresetCategory("garage", "🏷 Гаражная распродажа", "category/garagesale"),
    PresetCategory("antiques", "🏺 Антиквариат", "category/antiques"),
    PresetCategory("classifieds", "📦 Разное", "category/classifieds"),
    PresetCategory("free", "🎁 Бесплатно", "category/free"),
)

PRESET_BY_KEY = {c.key: c for c in PRESET_CATEGORIES}

MAX_CATEGORIES_PER_USER = 7

COUNTRY_LOCATIONS: dict[str, dict[str, str | list[str]]] = {
    "ch": {
        "label": "🇨🇭 Швейцария",
        "hubs": ["zurich", "geneva", "bern", "basel", "lausanne"],
    },
    "fi": {
        "label": "🇫🇮 Финляндия",
        "hubs": ["helsinki", "tampere", "turku", "oulu"],
    },
}
