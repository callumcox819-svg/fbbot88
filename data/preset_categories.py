"""Готовые категории Facebook Marketplace — пути от /marketplace/."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

# Marketplace CH из ссылок пользователя (search + filter_location_id)
CH_MARKETPLACE_LOCATION_ID = "103767472995143"


@dataclass(frozen=True)
class PresetCategory:
    key: str
    label: str
    url_path: str
    countries: tuple[str, ...] = ("fi",)


def _ch_search(category_id: str, query: str) -> str:
    q = quote_plus(query)
    return (
        f"{CH_MARKETPLACE_LOCATION_ID}/search"
        f"?category_id={category_id}&query={q}&referral_ui_component=category_menu_item"
    )


# 🇨🇭 — категории из меню Marketplace Switzerland (search URLs)
PRESET_CATEGORIES_CH: tuple[PresetCategory, ...] = (
    PresetCategory(
        "ch_toys",
        "🧸 Игрушки",
        _ch_search("199404184572737", "Toys and games"),
        ("ch",),
    ),
    PresetCategory(
        "ch_sports",
        "⚽ Спорт",
        _ch_search("391335928190702", "Sporting goods"),
        ("ch",),
    ),
    PresetCategory(
        "ch_home",
        "🛋 Дом",
        _ch_search("753380185098614", "Home goods"),
        ("ch",),
    ),
    PresetCategory(
        "ch_hobbies",
        "🎨 Хобби",
        _ch_search("459026188375950", "Hobbies"),
        ("ch",),
    ),
    PresetCategory(
        "ch_family",
        "👶 Семья",
        _ch_search("891748581240437", "Family"),
        ("ch",),
    ),
    PresetCategory(
        "ch_electronics",
        "📱 Электроника",
        _ch_search("479353692612078", "Electronics"),
        ("ch",),
    ),
    PresetCategory(
        "ch_clothing",
        "👕 Одежда",
        _ch_search("677457442746983", "Clothing"),
        ("ch",),
    ),
)

# 🇫🇮 — классические category/… + finland/helsinki в парсере
PRESET_CATEGORIES_FI: tuple[PresetCategory, ...] = (
    PresetCategory("vehicles", "🚗 Транспорт", "category/vehicles", ("fi",)),
    PresetCategory("electronics", "📱 Электроника", "category/electronics", ("fi",)),
    PresetCategory("property_rent", "🏠 Аренда жилья", "category/propertyrentals", ("fi",)),
    PresetCategory("property_sale", "🏢 Продажа жилья", "category/propertyforsale", ("fi",)),
    PresetCategory("apparel", "👕 Одежда", "category/apparel", ("fi",)),
    PresetCategory("baby", "👶 Дети", "category/baby", ("fi",)),
    PresetCategory("toys", "🧸 Игрушки", "category/toys", ("fi",)),
    PresetCategory("home", "🛋 Дом", "category/home", ("fi",)),
    PresetCategory("furniture", "🪑 Мебель", "category/furniture", ("fi",)),
    PresetCategory("appliances", "🔌 Бытовая техника", "category/appliances", ("fi",)),
    PresetCategory("garden", "🌿 Сад", "category/garden", ("fi",)),
    PresetCategory("tools", "🔧 Инструменты", "category/tools", ("fi",)),
    PresetCategory("sports", "⚽ Спорт", "category/sports", ("fi",)),
    PresetCategory("hobbies", "🎨 Хобби", "category/hobbies", ("fi",)),
    PresetCategory("entertainment", "🎮 Развлечения", "category/entertainment", ("fi",)),
    PresetCategory("instruments", "🎸 Музыка", "category/instruments", ("fi",)),
    PresetCategory("books", "📚 Книги", "category/books", ("fi",)),
    PresetCategory("games", "🕹 Видеоигры", "category/games", ("fi",)),
    PresetCategory("pets", "🐾 Животные", "category/pets", ("fi",)),
    PresetCategory("health", "💊 Здоровье", "category/health", ("fi",)),
    PresetCategory("beauty", "💄 Красота", "category/beauty", ("fi",)),
    PresetCategory("jewelry", "💍 Украшения", "category/jewelry", ("fi",)),
    PresetCategory("bags", "👜 Сумки", "category/bags", ("fi",)),
    PresetCategory("autoparts", "⚙️ Автозапчасти", "category/autoparts", ("fi",)),
    PresetCategory("office", "📎 Офис", "category/office", ("fi",)),
    PresetCategory("garage", "🏷 Гаражная распродажа", "category/garagesale", ("fi",)),
    PresetCategory("antiques", "🏺 Антиквариат", "category/antiques", ("fi",)),
    PresetCategory("classifieds", "📦 Разное", "category/classifieds", ("fi",)),
    PresetCategory("free", "🎁 Бесплатно", "category/free", ("fi",)),
)

PRESET_CATEGORIES = PRESET_CATEGORIES_FI + PRESET_CATEGORIES_CH
PRESET_BY_KEY = {c.key: c for c in PRESET_CATEGORIES}

MAX_CATEGORIES_PER_USER = 7


def presets_for_country(country: str | None) -> tuple[PresetCategory, ...]:
    if country == "ch":
        return PRESET_CATEGORIES_CH
    if country == "fi":
        return PRESET_CATEGORIES_FI
    return PRESET_CATEGORIES_FI


def preset_keys_for_country(country: str | None) -> frozenset[str]:
    return frozenset(c.key for c in presets_for_country(country))


COUNTRY_LOCATIONS: dict[str, dict] = {
    "ch": {
        "label": "🇨🇭 Швейцария",
        "latitude": 46.8182,
        "longitude": 8.2275,
        "radius_km": 80,
        "filter_location_id": CH_MARKETPLACE_LOCATION_ID,
        "marketplace_slugs": [CH_MARKETPLACE_LOCATION_ID, "switzerland"],
        "region_hubs": [
            "zurich",
            "geneva",
            "bern",
            "basel",
            "lausanne",
            "lugano",
            "winterthur",
            "lucerne",
            "stgallen",
        ],
    },
    "fi": {
        "label": "🇫🇮 Финляндия",
        "latitude": 60.1699,
        "longitude": 24.9384,
        "radius_km": 80,
        "filter_location_id": "106410786056698",
        "marketplace_slugs": ["finland"],
        "region_hubs": [
            "helsinki",
            "tampere",
            "turku",
            "oulu",
            "espoo",
            "vantaa",
            "jyvaskyla",
        ],
    },
}
