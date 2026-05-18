"""Готовые категории Facebook Marketplace — search URLs CH/FI от пользователя."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

# ID Marketplace из ссылок браузера (search + GraphQL filter_location_id)
CH_MARKETPLACE_LOCATION_ID = "103767472995143"
FI_MARKETPLACE_LOCATION_ID = "104042359631581"


@dataclass(frozen=True)
class PresetCategory:
    key: str
    label: str
    url_path: str
    countries: tuple[str, ...] = ("fi",)


def _marketplace_search(location_id: str, category_id: str, query: str) -> str:
    q = quote_plus(query)
    return (
        f"{location_id}/search"
        f"?category_id={category_id}&query={q}&referral_ui_component=category_menu_item"
    )


def _ch_search(category_id: str, query: str) -> str:
    return _marketplace_search(CH_MARKETPLACE_LOCATION_ID, category_id, query)


def _fi_search(category_id: str, query: str) -> str:
    return _marketplace_search(FI_MARKETPLACE_LOCATION_ID, category_id, query)


# 🇨🇭 — Marketplace Switzerland
PRESET_CATEGORIES_CH: tuple[PresetCategory, ...] = (
    PresetCategory("ch_toys", "🧸 Игрушки", _ch_search("199404184572737", "Toys and games"), ("ch",)),
    PresetCategory("ch_sports", "⚽ Спорт", _ch_search("391335928190702", "Sporting goods"), ("ch",)),
    PresetCategory("ch_home", "🛋 Дом", _ch_search("753380185098614", "Home goods"), ("ch",)),
    PresetCategory("ch_hobbies", "🎨 Хобби", _ch_search("459026188375950", "Hobbies"), ("ch",)),
    PresetCategory("ch_family", "👶 Семья", _ch_search("891748581240437", "Family"), ("ch",)),
    PresetCategory(
        "ch_electronics", "📱 Электроника", _ch_search("479353692612078", "Electronics"), ("ch",)
    ),
    PresetCategory("ch_clothing", "👕 Одежда", _ch_search("677457442746983", "Clothing"), ("ch",)),
)

# 🇫🇮 — Marketplace Finland (те же category_id, другой location_id)
PRESET_CATEGORIES_FI: tuple[PresetCategory, ...] = (
    PresetCategory("fi_toys", "🧸 Игрушки", _fi_search("199404184572737", "Toys and games"), ("fi",)),
    PresetCategory("fi_sports", "⚽ Спорт", _fi_search("391335928190702", "Sporting goods"), ("fi",)),
    PresetCategory("fi_home", "🛋 Дом", _fi_search("753380185098614", "Home goods"), ("fi",)),
    PresetCategory("fi_hobbies", "🎨 Хобби", _fi_search("459026188375950", "Hobbies"), ("fi",)),
    PresetCategory("fi_family", "👶 Семья", _fi_search("891748581240437", "Family"), ("fi",)),
    PresetCategory(
        "fi_electronics", "📱 Электроника", _fi_search("479353692612078", "Electronics"), ("fi",)
    ),
    PresetCategory("fi_clothing", "👕 Одежда", _fi_search("677457442746983", "Clothing"), ("fi",)),
)

PRESET_CATEGORIES = PRESET_CATEGORIES_FI + PRESET_CATEGORIES_CH
PRESET_BY_KEY = {c.key: c for c in PRESET_CATEGORIES}


@dataclass(frozen=True)
class ParseCategory:
    """Категория для парсинга (из пресетов страны, без выбора в UI)."""

    key: str
    label: str
    url_path: str


def presets_for_country(country: str | None) -> tuple[PresetCategory, ...]:
    if country == "ch":
        return PRESET_CATEGORIES_CH
    if country == "fi":
        return PRESET_CATEGORIES_FI
    return PRESET_CATEGORIES_FI


def preset_keys_for_country(country: str | None) -> frozenset[str]:
    return frozenset(c.key for c in presets_for_country(country))


def parse_categories_for_country(country: str) -> tuple[ParseCategory, ...]:
    """Все search-категории страны — парсинг без ручного выбора."""
    return tuple(
        ParseCategory(key=c.key, label=c.label, url_path=c.url_path)
        for c in presets_for_country(country)
    )


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
        "filter_location_id": FI_MARKETPLACE_LOCATION_ID,
        "marketplace_slugs": [FI_MARKETPLACE_LOCATION_ID, "finland"],
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
