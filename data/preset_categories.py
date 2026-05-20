"""Категории Marketplace как у VOID: finland/category/… и switzerland/category/…"""

from __future__ import annotations

from dataclasses import dataclass

CH_MARKETPLACE_LOCATION_ID = "103767472995143"
FI_MARKETPLACE_LOCATION_ID = "104042359631581"

# Те же slug, что в VoidParser (marketplace/category/…)
_VOID_CATEGORY_SLUGS: tuple[tuple[str, str], ...] = (
    ("electronics", "📱 Электроника"),
    ("apparel", "👕 Одежда"),
    ("home", "🛋 Дом"),
    ("sports", "⚽ Спорт"),
    ("hobbies", "🎨 Хобби"),
    ("family", "👶 Семья"),
)


@dataclass(frozen=True)
class PresetCategory:
    key: str
    label: str
    url_path: str
    countries: tuple[str, ...] = ("fi",)


def _country_category(country_slug: str, fb_slug: str) -> str:
    return f"{country_slug}/category/{fb_slug}"


def _build_presets(country: str, country_slug: str) -> tuple[PresetCategory, ...]:
    return tuple(
        PresetCategory(
            f"{country}_{slug}",
            label,
            _country_category(country_slug, slug),
            (country,),
        )
        for slug, label in _VOID_CATEGORY_SLUGS
    )


PRESET_CATEGORIES_FI = _build_presets("fi", "finland")
PRESET_CATEGORIES_CH = _build_presets("ch", "switzerland")

PRESET_CATEGORIES = PRESET_CATEGORIES_FI + PRESET_CATEGORIES_CH
PRESET_BY_KEY = {c.key: c for c in PRESET_CATEGORIES}


@dataclass(frozen=True)
class ParseCategory:
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
