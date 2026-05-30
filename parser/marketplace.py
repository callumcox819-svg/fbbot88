"""Сбор объявлений с Facebook Marketplace."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import aiohttp
from aiohttp_socks import ProxyConnector

from config import config
from data.preset_categories import (
    CH_MARKETPLACE_LOCATION_ID,
    COUNTRY_LOCATIONS,
    FI_MARKETPLACE_LOCATION_ID,
)
from parser.marketplace_region import append_geo_to_marketplace_url
from parser.account_token import AccountToken, AccountTokenDeadError, cookies_header

logger = logging.getLogger(__name__)

_FB_BASE = "https://www.facebook.com/marketplace/"
_LISTING_ID_RE = re.compile(r"/marketplace/item/(\d+)")
_LISTING_ID_JSON_RE = re.compile(r'"(?:listing_)?id"\s*:\s*"(\d{8,})"')
_TITLE_RE = re.compile(
    r'"marketplace_listing_title"\s*:\s*"((?:\\.|[^"\\])*)"|"title"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_PRICE_RE = re.compile(
    r'"formatted_amount"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"amount"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"formatted_price"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"listing_price"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_SELLER_RE = re.compile(
    r'"marketplace_listing_seller_name"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"marketplace_listing_seller"\s*:\s*\{[^}]{0,400}?"name"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"seller"\s*:\s*\{[^}]{0,400}?"name"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"marketplace_listing_owner"\s*:\s*\{[^}]{0,400}?"name"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"listing_creator"\s*:\s*\{[^}]{0,400}?"name"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"owner"\s*:\s*\{[^}]{0,600}?"name"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"short_name"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"display_name"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_SELLER_BLOB_KEYS = (
    "marketplace_listing_seller",
    "seller",
    "marketplace_listing_owner",
    "listing_creator",
    "owner",
    "actor",
    "marketplace_user",
    "profile",
    "listing_seller",
)
_PHOTO_RE = re.compile(r'"uri"\s*:\s*"(https://[^"]*scontent[^"]*)"')
_LOCATION_RE = re.compile(r'"city"\s*:\s*"([^"]+)"|"location_text"\s*:\s*\{\s*"text"\s*:\s*"([^"]+)"')
_DESC_RE = re.compile(
    r'"marketplace_listing_description"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"redacted_description"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_SELLER_ID_RE = re.compile(r'"marketplace_listing_seller_id"\s*:\s*"(\d+)"')
_PROFILE_LINK_RE = re.compile(r"/marketplace/profile/(\d{8,})")
_SELLER_ID_ALT_RE = re.compile(
    r'"(?:marketplace_listing_seller|listing_seller|seller)"\s*:\s*\{[^}]{0,600}?"id"\s*:\s*"(\d{8,})"'
)
_SELLER_NEAR_LISTING_RE = re.compile(
    r'"(?P<lid>\d{10,})"[\s\S]{0,25000}?/marketplace/profile/(?P<pid>\d{8,})'
)
_PROFILE_BEFORE_LISTING_RE = re.compile(
    r'/marketplace/profile/(?P<pid>\d{8,})[\s\S]{0,25000}?"(?P<lid>\d{10,})"'
)
_PROFILE_PHP_RE = re.compile(r'profile\.php\?id=(\d{8,})')
_REL_TIME_RE = re.compile(
    r'"(?:creation_time|listing_created_time|time_created)"\s*:\s*\{[^}]{0,400}?"text"\s*:\s*"([^"]+)"'
)
_CREATION_UNIX_RE = re.compile(
    r'"(?:creation_time|listing_created_time|time_created)"\s*:\s*\{[^}]{0,200}?"timestamp"\s*:\s*(\d{9,11})'
)
_MAX_CATEGORY_FEED_PAGES = 25
_FEED_PAGE_SIZE = 24


def _pages_per_category() -> int:
    return max(1, min(int(config.marketplace_pages_per_category), _MAX_CATEGORY_FEED_PAGES))


def _graphql_doc_for_feed(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return config.fb_marketplace_browse_doc_id


_FEED_DOC_PATTERNS = (
    re.compile(
        r"MarketplaceCategoryFeedPaginationQuery[^}]{0,800}?\"doc_id\"\s*:\s*\"(\d{15,20})\"",
        re.I | re.DOTALL,
    ),
    re.compile(
        r"CometMarketplaceCategoryFeed[^}]{0,800}?\"doc_id\"\s*:\s*\"(\d{15,20})\"",
        re.I | re.DOTALL,
    ),
    re.compile(
        r"marketplace[^\"]{0,40}pagination[^}]{0,800}?\"doc_id\"\s*:\s*\"(\d{15,20})\"",
        re.I | re.DOTALL,
    ),
)


def _extract_feed_doc_id_from_html(html: str) -> str | None:
    """doc_id ленты категории из HTML — для 2+ страницы без ручного FB_MARKETPLACE_DOC_ID."""
    for pat in _FEED_DOC_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    for raw in _SCRIPT_JSON_RE.findall(html):
        if "marketplace" not in raw.lower() or "doc_id" not in raw:
            continue
        for m in re.finditer(r"\"doc_id\"\s*:\s*\"(\d{15,20})\"", raw):
            if "Marketplace" in raw or "marketplace" in raw:
                return m.group(1)
    return None


_JOIN_TIME_RE = re.compile(
    r'"(?:join_time|seller_join_time|marketplace_seller_join_time)"\s*:\s*\{[^}]{0,400}?"text"\s*:\s*"([^"]+)"'
)
_ADS_COUNT_RE = re.compile(
    r'"(?:active_listing_count|marketplace_listing_count|listing_count)"\s*:\s*(\d+)'
)
_GENDER_RE = re.compile(r'"gender"\s*:\s*"([^"]+)"')
_RATING_RE = re.compile(r'"(?:marketplace_rating|rating)"\s*:\s*(\d+(?:\.\d+)?)')
_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]+type="application/json"[^>]*>([^<]+)</script>',
    re.IGNORECASE,
)
_WINDOW_BEFORE = 900
_WINDOW_AFTER = 6500
_LISTING_PROFILE_MAX_DIST = 80_000

_category_feed_html: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "category_feed_html", default=None
)
_cached_item_doc_id: str | None = None

_ITEM_DOC_PATTERNS = (
    re.compile(
        r"CometMarketplaceListingDetails[^}]{0,2000}?\"doc_id\"\s*:\s*\"(\d{15,20})\"",
        re.I | re.DOTALL,
    ),
    re.compile(
        r"MarketplaceProductDetailsPage[^}]{0,2000}?\"doc_id\"\s*:\s*\"(\d{15,20})\"",
        re.I | re.DOTALL,
    ),
    re.compile(
        r"ListingDetails[^}]{0,2000}?\"doc_id\"\s*:\s*\"(\d{15,20})\"",
        re.I | re.DOTALL,
    ),
)

_GENDER_RU = {
    "MALE": "Мужской",
    "FEMALE": "Женский",
    "male": "Мужской",
    "female": "Женский",
    "OTHER": "Другое",
}


@dataclass
class MarketplaceListing:
    listing_id: str
    title: str = ""
    price: str = ""
    link: str = ""
    seller_name: str = ""
    seller_id: str = ""
    photo: str = ""
    location: str = ""
    category: str = ""
    item_desc: str = ""
    person_link: str = ""
    created_date: str = ""
    created_timestamp: int | None = None
    created_real_date: str = ""
    person_reg_date: str = ""
    ads_number: int | None = None
    ads_number_bought: int | None = None
    ads_number_sold: int | None = None
    gender: str = ""
    email: str = ""
    phone: str = ""
    views: int | None = None
    parser_views: int = 0
    rating: float | int = 0


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s.replace("\\u0027", "'").replace('\\"', '"')


_CH_OK = (
    "switzerland",
    "schweiz",
    "suisse",
    "svizzera",
    "ch-",
    " chf",
    " zürich",
    " zurich",
    " geneva",
    " genève",
    " genf",
    " bern",
    " berne",
    " basel",
    " bâle",
    " lausanne",
    " lugano",
    " winterthur",
    " luzern",
    " lucerne",
    " st. gallen",
    " st gallen",
    " thun",
    " biel",
    " fribourg",
    " neuchâtel",
    " sion",
    ", vs",
    ", vd",
    ", ge",
    ", zh",
    ", be",
    ", bs",
    ", bl",
    ", ag",
    ", sg",
    ", gr",
    ", lu",
    ", ne",
    ", fr",
    ", ju",
    ", ti",
    ", sz",
    ", nw",
    ", ow",
    ", gl",
    ", zg",
    ", ur",
    ", sh",
    ", ar",
    ", ai",
)
_CH_REJECT = (
    "germany",
    "deutschland",
    "france",
    "italy",
    "italia",
    "austria",
    "österreich",
    "united kingdom",
    " uk",
    "finland",
    "suomi",
    "ukraine",
    "україн",
    "украин",
    "київ",
    "киев",
    "kyiv",
    "poland",
    "polska",
    "romania",
)
_FI_OK = (
    "finland",
    "suomi",
    "helsinki",
    "tampere",
    "turku",
    "oulu",
    "espoo",
    "vantaa",
    "jyväskylä",
    "jyvaskyla",
    "lahti",
    "pori",
    "kuopio",
    "joensuu",
    "vaasa",
    "rovaniemi",
    "mikkeli",
    "seinäjoki",
    "seinajoki",
    "hämeenlinna",
    "hameenlinna",
    "lappeenranta",
    "kotka",
    "porvoo",
    " kemi",
    " raahe",
    " uusimaa",
    " pirkanmaa",
    " varsinais",
    " pohjanmaa",
    " satakunta",
    " €",
    " eur",
)
_FI_TITLE_REJECT = (
    "schweiz",
    "schweizer",
    " svizzera",
    " suisse",
    " chf",
    "switzerland",
    "zürich",
    "zurich",
    "basel",
    "bern",
    "lausanne",
)
_CH_TITLE_REJECT = (
    "suomi",
    "finland",
    "helsinki",
    "tampere",
    "espoo",
    "vantaa",
)
_CH_CANTON_SUFFIX_RE = re.compile(
    r",\s*(ZH|BE|GE|VD|VS|AG|SG|BL|LU|SZ|TG|GR|FR|SO|BS|AR|AI|NW|OW|GL|ZG|UR|SH|JU|NE|TI)\s*$",
    re.IGNORECASE,
)
_CH_PLACE_RE = re.compile(
    r"\b(glarus|liestal|zurzach|opfikon|freienbach|rapperswil|schötz|schotz|arth|"
    r"gipf|oberfrick|kirchberg|winterthur|basel|bern|zürich|zurich|luzern|lucerne|"
    r"genf|geneva|genève|lausanne|lugano|biel|thun|fribourg|sion|neuchatel|neuchâtel|"
    r"yverdon|bulle|herisau|frauenfeld|bellinzona|locarno|montreux|vevey|nyon|morges|"
    r"pully|renens|uster|dübendorf|dubendorf|kloten|baden|wettingen|brugg|aarau|"
    r"solothurn|grenchen|burgdorf|langenthal|wil\b|gossau|spiez|interlaken|brig|visp|"
    r"martigny|sierre|zermatt|crans|olten|muttenz|reinach|allschwil|pratteln|emmen|"
    r"kriens|lenzburg|root\b|chur|davos|st\.?\s*moritz|appenzell|stans|sarnen|altdorf)\b",
    re.IGNORECASE,
)
_FI_REJECT = (
    "sweden",
    "sverige",
    "norway",
    "norge",
    "estonia",
    "germany",
    "deutschland",
    "switzerland",
    "schweiz",
    "russia",
    "ukraine",
    "україн",
    "украин",
)
_UA_SPAM_HINTS = (
    "україн",
    "украин",
    "київ",
    "киев",
    "kyiv",
    "kiev",
    "львів",
    "львов",
    "lviv",
    "lwow",
    "одес",
    "odesa",
    "харків",
    "харьков",
    "kharkiv",
    "dnipro",
    "дніпр",
    "запоріж",
    "zaporizh",
    "poltava",
    "полтав",
    "украина",
    "ukraine",
    "рогатин",
    "золочів",
    "золочев",
    "област",
    "районський",
    "районский",
)
_MIN_LISTING_ID_LEN = 12


def _accept_language(country: str | None) -> str:
    if country == "fi":
        return "fi-FI,fi;q=0.9,en;q=0.8"
    if country == "ch":
        return "de-CH,fr-CH,de;q=0.9,fr;q=0.8,en;q=0.7"
    return "en-US,en;q=0.9"


def text_has_ua_markers(text: str) -> bool:
    return _text_has_ua_markers_impl(text)


def _text_has_ua_markers(text: str) -> bool:
    return _text_has_ua_markers_impl(text)


def _text_has_ua_markers_impl(text: str) -> bool:
    t = (text or "").lower()
    if not t.strip():
        return False
    if any(m in t for m in _UA_SPAM_HINTS):
        return True
    if re.search(r"ський|ский|івськ|овськ", t):
        return True
    return False


def _location_suggests_ch(location: str) -> bool:
    loc = f" {(location or '').lower()} "
    if not loc.strip():
        return False
    if any(h in loc for h in _CH_OK):
        return True
    if _CH_CANTON_SUFFIX_RE.search((location or "").strip()):
        return True
    if _CH_PLACE_RE.search(location or ""):
        return True
    return False


def _location_suggests_fi(location: str) -> bool:
    loc = f" {(location or '').lower()} "
    if not loc.strip():
        return False
    if any(h in loc for h in _FI_OK):
        return True
    return False


def _location_matches_country(location: str, country: str) -> bool:
    """
    Режем только явно чужую страну (UA/CH при FI и наоборот).
    Неизвестный пригород без «Helsinki» в тексте — ок (URL finland/helsinki).
    """
    loc = (location or "").strip()
    if not loc:
        return True
    blob = f" {loc.lower()} "
    if country == "fi":
        if _text_has_ua_markers(loc):
            return False
        if any(r in blob for r in _FI_REJECT):
            return False
        if _location_suggests_ch(loc):
            return False
        return True
    if country == "ch":
        if any(r in blob for r in _CH_REJECT):
            return False
        if _location_suggests_fi(loc):
            return False
        return True
    return True


def listing_is_wrong_country(
    item: MarketplaceListing,
    country: str | None,
    *,
    void_mode: bool = False,
) -> bool:
    """
    VOID в JSON почти всегда с пустым location — не режем по заголовку/городу.
    void_mode: только UA и явное «Germany/Sweden/…» в location (после finland/ в URL).
    """
    if not country:
        return False
    if _text_has_ua_markers(item.location) or _text_has_ua_markers(item.title):
        return True
    loc = (item.location or "").strip()
    if not loc:
        return False
    if void_mode:
        return _location_explicitly_foreign(loc, country)
    title = (item.title or "").lower()
    if country == "fi" and any(x in title for x in _FI_TITLE_REJECT):
        return True
    if country == "ch" and any(x in title for x in _CH_TITLE_REJECT):
        return True
    if not _location_matches_country(loc, country):
        return True
    return _location_explicitly_foreign(loc, country)


def listing_is_valid(item: MarketplaceListing) -> bool:
    """Реальное объявление: не плейсхолдер и id из /marketplace/item/."""
    title = (item.title or "").strip()
    if not title or title.startswith("Listing "):
        return False
    lid = (item.listing_id or "").strip()
    if not lid.isdigit() or len(lid) < _MIN_LISTING_ID_LEN:
        return False
    link = item.link or ""
    if f"/marketplace/item/{lid}" not in link:
        return False
    return True


def listing_is_export_ready(
    item: MarketplaceListing,
    country: str | None = None,
    *,
    max_age_hours: float | None = None,
) -> bool:
    """Карточка как в ленте VOID: название + цена и/или фото (не пустышка)."""
    return export_reject_reason(item, country, max_age_hours=max_age_hours) is None


def _dig_timestamp(ct: Any) -> int | None:
    if isinstance(ct, (int, float)) and ct > 1_000_000_000:
        return int(ct)
    if not isinstance(ct, dict):
        return None
    for key in ("timestamp", "unix", "time"):
        val = ct.get(key)
        if isinstance(val, (int, float)) and val > 1_000_000_000:
            return int(val)
    rel = _dig_str(ct, "text")
    if rel:
        hours = _parse_relative_time_hours(rel)
        if hours is not None:
            return int(time.time() - hours * 3600)
    return None


def _parse_relative_time_hours(text: str) -> float | None:
    t = (text or "").lower().strip()
    if not t:
        return None
    if any(x in t for x in ("just now", "gerade", "à l'instant", "maintenant", "aujourd")):
        return 0.0
    if any(x in t for x in ("yesterday", "gestern", "hier")):
        return 24.0
    m = re.search(r"(\d+)\s*(?:min(?:ute)?s?|min\.?|мин|minuuttia)", t)
    if m:
        return int(m.group(1)) / 60.0
    m = re.search(
        r"(?:il y a|vor)\s*(\d+)\s*(?:heures?|h|std\.?|stunden?)|"
        r"(\d+)\s*(?:h|hr|hours?|heures?|std\.?|stunden?|tuntia)|"
        r"(\d+)\s*t\s+sitten|"
        r"(\d+)\s*tuntia\s+sitten",
        t,
    )
    if m:
        return float(next(g for g in m.groups() if g))
    m = re.search(
        r"(\d+)\s*(?:d|days?|tage?|jours?|päivää?|paivaa?)|"
        r"(\d+)\s*päivää?\s+sitten",
        t,
    )
    if m:
        g = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
        if g:
            return float(g) * 24.0
    return None


def listing_age_hours(item: MarketplaceListing) -> float | None:
    if item.created_timestamp:
        return max(0.0, (time.time() - item.created_timestamp) / 3600.0)
    if item.created_date:
        return _parse_relative_time_hours(item.created_date)
    return None


def listing_is_too_old(item: MarketplaceListing, max_age_hours: float) -> bool:
    """
    Лимит по тексту «5 tuntia sitten» / «2 days ago».
    Без даты или только unix из ленты категории — не режем (там часто мусор).
    """
    if max_age_hours <= 0:
        return False
    cd = (item.created_date or "").lower().strip()
    if not cd:
        return False
    hours = _parse_relative_time_hours(cd)
    if hours is not None:
        return hours > max_age_hours
    if any(x in cd for x in ("yesterday", "gestern", "hier", "eilen")):
        return True
    if re.search(r"\d+\s*(?:d|days?|tage?|jours?|päiv)", cd):
        return True
    if any(
        x in cd
        for x in ("week", "woche", "semaine", "month", "monat", "mois", "kuukaus", "viikko")
    ):
        return True
    return False


def _feed_page_all_too_old(batch: list[MarketplaceListing], max_age_hours: float) -> bool:
    """Лента обычно от новых к старым — если страница целиком старше лимита, дальше не листаем."""
    checked = 0
    old = 0
    for item in batch:
        age = listing_age_hours(item)
        if age is None:
            continue
        checked += 1
        if age > max_age_hours:
            old += 1
    return checked >= 3 and old == checked


def _price_hints_country(price: str, country: str) -> bool:
    p = price.lower()
    if country == "ch":
        return "chf" in p or "fr." in p or "sfr" in p
    if country == "fi":
        return "eur" in p or "€" in p
    return False


def _location_explicitly_foreign(location: str, country: str) -> bool:
    """Только явно чужая страна в тексте локации (не «нет Zürich в списке»)."""
    loc = (location or "").strip()
    if not loc:
        return False
    blob = f" {loc.lower()} "
    if country == "ch":
        return any(r in blob for r in _CH_REJECT)
    if country == "fi":
        return any(r in blob for r in _FI_REJECT)
    return False


def export_reject_reason(
    item: MarketplaceListing,
    country: str | None = None,
    *,
    max_age_hours: float | None = None,
) -> str | None:
    if not listing_is_valid(item):
        return "нет_заголовка"
    if max_age_hours is not None and listing_is_too_old(item, max_age_hours):
        return "старше_24ч"
    price = (item.price or "").strip()
    photo = (item.photo or "").strip()
    seller = (item.seller_name or "").strip()

    if price and photo:
        return None
    if price and seller:
        return None
    if photo and seller:
        return None
    if price:
        return None
    if photo:
        return None
    return "мало_полей"


def void_export_reject_reason(
    item: MarketplaceListing,
    country: str | None = None,
    *,
    max_age_hours: float | None = None,
) -> str | None:
    """
    Как VOID: title + price + photo + person_link (profile/1000…).
    Имя и gender/ads_number — с карточки объявления после enrich.
    """
    from services.seller_blacklist import _profile_id

    base = export_reject_reason(item, country, max_age_hours=max_age_hours)
    if base:
        return base
    if _text_has_ua_markers(item.location) or _text_has_ua_markers(item.title):
        return "чужая_страна"
    if not _profile_id(item):
        return "нет_продавца"
    return None


def void_complete_from_feed(
    item: MarketplaceListing,
    country: str | None,
    *,
    max_age_hours: float,
) -> bool:
    """Пропускаем enrich только если в ленте уже есть всё как в VOID JSON."""
    from services.seller_blacklist import _profile_id

    if not _profile_id(item):
        return False
    return export_reject_reason(item, country, max_age_hours=max_age_hours) is None


def _country_location_ok(location: str, country: str | None) -> bool:
    if not country:
        return True
    return _location_matches_country(location or "", country)


def format_price_for_country(price: str, country: str | None) -> str:
    p = (price or "").strip()
    if not p or not country:
        return p
    low = p.lower()
    if country == "fi":
        if "€" in p or "eur" in low:
            return p
        if re.match(r"^[\d\s.,]+$", p.replace(",", ".")):
            return f"{p} €"
    if country == "ch":
        if "chf" in low or "fr." in low or "sfr" in low:
            return p
        if re.match(r"^[\d\s.,]+$", p.replace(",", ".")):
            return f"{p} CHF"
    return p


def normalize_listing_for_export(item: MarketplaceListing, country: str | None) -> None:
    if country:
        item.price = format_price_for_country(item.price, country)


def _all_marketplace_roots() -> frozenset[str]:
    roots: set[str] = {CH_MARKETPLACE_LOCATION_ID, FI_MARKETPLACE_LOCATION_ID}
    for cfg in COUNTRY_LOCATIONS.values():
        roots.update(str(x) for x in (cfg.get("marketplace_slugs") or []))
        roots.update(cfg.get("region_hubs") or [])
        lid = cfg.get("filter_location_id")
        if lid:
            roots.add(str(lid))
    return frozenset(roots)


def _is_search_category_path(path: str) -> bool:
    return "/search" in path or "category_id=" in path


def _normalize_search_path(path: str) -> str:
    """103767472995143/search?category_id=…&query=…"""
    if path.startswith("http"):
        parsed = urlparse(path)
        tail = parsed.path
        if "facebook.com/marketplace/" in tail:
            tail = tail.split("facebook.com/marketplace/", 1)[1]
        elif "/marketplace/" in path:
            tail = path.split("/marketplace/", 1)[1]
        else:
            tail = parsed.path.lstrip("/")
        qs = parse_qs(parsed.query)
    else:
        if "facebook.com/marketplace/" in path:
            path = path.split("facebook.com/marketplace/", 1)[1]
        path = path.strip("/")
        if "?" in path:
            tail, q = path.split("?", 1)
            qs = parse_qs(q)
        else:
            tail, qs = path, {}

    parts = tail.strip("/").split("/")
    loc_id = parts[0] if parts else FI_MARKETPLACE_LOCATION_ID
    cat_id = (qs.get("category_id") or [""])[0]
    query = (qs.get("query") or [""])[0]
    if not cat_id:
        raise ValueError("Нет category_id в ссылке search")
    q = quote_plus(query) if query else ""
    out = f"{loc_id}/search?category_id={cat_id}"
    if q:
        out += f"&query={q}"
    ref = (qs.get("referral_ui_component") or ["category_menu_item"])[0]
    out += f"&referral_ui_component={quote_plus(ref)}"
    return out


def _split_marketplace_path(path: str) -> tuple[str | None, str | None]:
    """
    finland/category/baby → (finland, baby)
    category/baby → (None, baby)
    baby → (None, baby)
    """
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return None, None
    roots = _all_marketplace_roots()
    if len(parts) >= 3 and parts[0] in roots and parts[1] == "category":
        return parts[0], parts[2]
    if len(parts) >= 2 and parts[0] == "category":
        return None, parts[1]
    if parts[0] not in roots:
        return None, parts[-1]
    return None, None


def normalize_category_path(url_or_path: str) -> str:
    """
    …/marketplace/finland/category/baby → finland/category/baby
    …/marketplace/103767472995143/search/?category_id=… → search path
    """
    raw = (url_or_path or "").strip()
    if not raw:
        raise ValueError("Пустой путь категории")
    if _is_search_category_path(raw):
        return _normalize_search_path(raw)
    path = raw
    if "facebook.com/marketplace/" in path:
        path = path.split("facebook.com/marketplace/", 1)[1]
    path = path.strip("/").split("?")[0]
    if not path:
        raise ValueError("Пустой путь категории")
    root, slug = _split_marketplace_path(path)
    if not slug:
        raise ValueError("Не удалось определить категорию")
    if root:
        return f"{root}/category/{slug}"
    return f"category/{slug}"


def _category_slug(url_path: str) -> str:
    _, slug = _split_marketplace_path(url_path.strip("/"))
    if slug:
        return slug
    parts = url_path.strip("/").split("/")
    return parts[-1] if parts else ""


def build_category_url(url_path: str, *, marketplace_root: str | None = None) -> str:
    """
    marketplace/category/sports/
    marketplace/finland/category/baby/
    marketplace/103767472995143/search?category_id=…
    """
    norm = normalize_category_path(url_path)
    if _is_search_category_path(norm):
        return urljoin(_FB_BASE, norm if norm.endswith("/") else f"{norm}/")
    root, slug = _split_marketplace_path(norm)
    if root and slug:
        return urljoin(_FB_BASE, f"{root}/category/{slug}/")
    if marketplace_root and slug:
        return urljoin(_FB_BASE, f"{marketplace_root.strip('/')}/category/{slug}/")
    return urljoin(_FB_BASE, f"{norm}/")


def with_country_geo(url: str, country: str | None) -> str:
    if country:
        return append_geo_to_marketplace_url(url, country)
    return url


def urls_for_country_category(
    country: str,
    url_path: str,
    *,
    hub_round: int | None = None,
) -> list[str]:
    """Регионы выбранной страны. hub_round — один хаб за проход (не все 10 сразу)."""
    cfg = COUNTRY_LOCATIONS.get(country) or {}
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    norm = normalize_category_path(url_path)
    if _is_search_category_path(norm):
        add(build_category_url(norm))
        return urls

    embed_root, cat_slug = _split_marketplace_path(norm)
    country_slugs = set(str(x) for x in (cfg.get("marketplace_slugs") or []))
    country_hubs = set(cfg.get("region_hubs") or [])
    loc_id = str(cfg.get("filter_location_id") or "")
    if loc_id:
        country_slugs.add(loc_id)
    if embed_root and (embed_root in country_slugs or embed_root in country_hubs):
        if cat_slug and loc_id:
            add(build_category_url(f"{loc_id}/category/{cat_slug}"))
        add(build_category_url(norm))
        return urls

    slugs = cfg.get("marketplace_slugs") or []
    hubs = cfg.get("region_hubs") or []

    if hub_round is not None:
        if slugs:
            add(build_category_url(url_path, marketplace_root=slugs[0]))
        if hubs:
            add(build_category_url(url_path, marketplace_root=hubs[hub_round % len(hubs)]))
        if urls:
            return urls

    for slug in slugs:
        add(build_category_url(url_path, marketplace_root=slug))
    for hub in hubs:
        add(build_category_url(url_path, marketplace_root=hub))
    if not urls:
        add(build_category_url(url_path))
    return urls


async def fetch_category_listings(
    token: AccountToken,
    *,
    url_path: str,
    category_label: str,
    user_agent: str,
    country: str | None,
    proxy_url: str | None,
    limit: int,
    timeout_sec: float = 22.0,
    on_url_progress: Callable[[int, int, str], Awaitable[None]] | None = None,
    on_page_found: Callable[[int], Awaitable[None]] | None = None,
    on_page_items: Callable[[list[MarketplaceListing]], Awaitable[None]] | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_stop_pagination: Callable[[], bool] | None = None,
    hub_round: int | None = None,
    graphql_doc_id: str | None = None,
) -> tuple[list[MarketplaceListing], dict[str, Any]]:
    """Категория CH/FI: несколько страниц ленты (как VOID), без лишнего листания."""
    if country and country in COUNTRY_LOCATIONS:
        urls = urls_for_country_category(country, url_path, hub_round=hub_round)
    else:
        urls = [build_category_url(url_path)]

    seen_ids: set[str] = set()
    out: list[MarketplaceListing] = []
    total_urls = len(urls)
    feed_doc = _graphql_doc_for_feed(graphql_doc_id)
    if urls:
        logger.info(
            "feed country=%s primary=%s (VOID loc_id, не только finland/)",
            country or "?",
            urls[0].replace(_FB_BASE, "")[:64],
        )
    max_pages = _pages_per_category()
    pages_fetched = 0
    has_next_at_end = False
    stopped_early = False
    stopped_dup_page = False

    for i, url in enumerate(urls, start=1):
        if should_stop and should_stop():
            break
        if len(out) >= limit:
            break
        short = url.replace("https://www.facebook.com/marketplace/", "")[:48]
        if on_url_progress:
            await on_url_progress(i, total_urls, short)
        fetch_url = with_country_geo(url, country)
        cursor: str | None = None
        discovered_doc: str | None = None
        for page_idx in range(max_pages):
            if should_stop and should_stop():
                break
            if len(out) >= limit:
                break
            page_doc = feed_doc or discovered_doc
            logger.info("GET %s page=%s doc=%s", fetch_url, page_idx + 1, bool(page_doc))
            try:
                batch, meta, cursor, has_next = await _fetch_page(
                    token,
                    url=fetch_url,
                    user_agent=user_agent,
                    proxy_url=proxy_url,
                    timeout_sec=timeout_sec,
                    category_label=category_label,
                    graphql_doc_id=page_doc,
                    cursor=cursor,
                    country=country,
                )
                if meta.get("discovered_doc_id") and not discovered_doc:
                    discovered_doc = str(meta["discovered_doc_id"])
                    logger.info("discovered feed doc_id %s for %s", discovered_doc, short)
                pages_fetched += 1
                has_next_at_end = bool(has_next and cursor)
                logger.info(
                    "parsed %s items from %s p%s (src=%s, links=%s, next=%s)",
                    len(batch),
                    short,
                    page_idx + 1,
                    meta.get("source"),
                    meta.get("link_count"),
                    has_next,
                )
                page_new: list[MarketplaceListing] = []
                for item in batch:
                    if not listing_is_valid(item):
                        continue
                    if item.listing_id in seen_ids:
                        continue
                    seen_ids.add(item.listing_id)
                    page_new.append(item)
                    out.append(item)
                    if len(out) >= limit:
                        break

                page_html = meta.get("raw_html")
                if page_html:
                    html_s = str(page_html)
                    _category_feed_html.set(html_s)
                    seller_index = build_feed_seller_index(html_s)
                    n_prof = len(seller_index)
                    if n_prof:
                        logger.info(
                            "feed sellers indexed: %s / %s from %s",
                            n_prof,
                            len(page_new),
                            short,
                        )
                    for it in page_new:
                        apply_feed_seller_index(it, seller_index)
                        sp = _patch_seller_from_html(html_s, it.listing_id)
                        if sp:
                            _merge_listing(it, sp)
                        if not (it.seller_name or "").strip():
                            nm = _seller_name_near_listing_in_html(html_s, it.listing_id)
                            if nm:
                                it.seller_name = nm

                if on_page_found and page_new:
                    await on_page_found(len(page_new))
                if on_page_items and page_new:
                    await on_page_items(page_new)
                if should_stop_pagination and should_stop_pagination():
                    stopped_dup_page = True
                    has_next_at_end = False
                    break
                if len(out) >= limit:
                    stopped_early = True
                    break
                if should_stop and should_stop():
                    stopped_early = True
                    break
                if not has_next or not cursor:
                    has_next_at_end = False
                    break
                if config.parse_page_delay_sec > 0:
                    await asyncio.sleep(config.parse_page_delay_sec)
            except RuntimeError as e:
                if "HTTP 400" in str(e) or "HTTP 404" in str(e):
                    logger.info("skip url %s: %s", url, e)
                    break
                if page_idx == 0:
                    raise
                logger.info("stop pagination %s after p%s: %s", short, page_idx + 1, e)
                break
            except Exception as e:
                if page_idx == 0:
                    raise
                logger.warning("stop pagination %s after p%s: %s", short, page_idx + 1, e)
                break

    exhausted = (
        pages_fetched > 0
        and not has_next_at_end
        and not stopped_early
    )
    fetch_meta = {
        "pages_fetched": pages_fetched,
        "listings_seen": len(seen_ids),
        "exhausted": exhausted,
        "stopped_dup_page": stopped_dup_page,
        "urls_tried": len(urls),
    }
    logger.info(
        "category %s country=%s urls=%s pages=%s seen=%s collected=%s exhausted=%s",
        category_label,
        country or "all",
        len(urls),
        pages_fetched,
        len(seen_ids),
        len(out),
        exhausted,
    )
    return out[:limit], fetch_meta


def _session_for_proxy(proxy_url: str | None) -> aiohttp.ClientSession:
    """SOCKS5/HTTP прокси — через aiohttp-socks (иначе Cannot connect to host)."""
    if not proxy_url:
        return aiohttp.ClientSession()
    connector = ProxyConnector.from_url(proxy_url)
    return aiohttp.ClientSession(connector=connector)


def is_connection_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(
        x in s
        for x in (
            "cannot connect",
            "connection refused",
            "connection reset",
            "timed out",
            "timeout",
            "ssl",
            "proxy",
            "network is unreachable",
        )
    )


def _gender_ru(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    return _GENDER_RU.get(s, s)


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    if not m:
        return ""
    g = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else "")
    return _unescape(g) if g else ""


def _int_or_none(s: str) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _merge_listing(base: MarketplaceListing, patch: dict[str, Any]) -> MarketplaceListing:
    for key, val in patch.items():
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        if hasattr(base, key):
            setattr(base, key, val)
    if base.listing_id and not base.link:
        base.link = f"https://www.facebook.com/marketplace/item/{base.listing_id}/"
    return base


def _looks_like_login_wall(html: str) -> bool:
    h = html.lower()
    if "id=\"loginform\"" in h or 'id="loginform"' in h:
        return True
    if "checkpoint" in h and len(html) < 800_000:
        return True
    if "marketplace/item/" not in h and ("login" in h[:8000] or "log in" in h[:8000]):
        return True
    return False


def _seller_name_from_blob(blob: dict[str, Any]) -> str:
    name = _dig_str(
        blob,
        "name",
        "marketplace_listing_seller_name",
        "short_name",
        "display_name",
    )
    if not name or name.startswith("Listing "):
        return ""
    return name


def _apply_seller_blob(blob: dict[str, Any], listing_id: str, out: dict[str, Any]) -> bool:
    """Имя продавца достаточно; ID — бонус для person_link."""
    name = _seller_name_from_blob(blob)
    if name:
        out["seller_name"] = name
    sid = str(blob.get("id") or "").strip()
    if sid.isdigit() and sid != listing_id:
        out["seller_id"] = sid
        out["person_link"] = f"https://www.facebook.com/marketplace/profile/{sid}/"
    return bool(out.get("seller_name") or out.get("seller_id"))


def _deep_seller_patch(obj: Any, listing_id: str, out: dict[str, Any]) -> bool:
    """Ищем seller в любом вложенном JSON (Comet/GraphQL)."""
    if isinstance(obj, dict):
        node_id = str(obj.get("id") or obj.get("listing_id") or "").strip()
        if node_id == listing_id:
            for key in _SELLER_BLOB_KEYS:
                blob = obj.get(key)
                if isinstance(blob, dict) and _apply_seller_blob(blob, listing_id, out):
                    return True
        for val in obj.values():
            if _deep_seller_patch(val, listing_id, out):
                return True
    elif isinstance(obj, list):
        for val in obj:
            if _deep_seller_patch(val, listing_id, out):
                return True
    return False


def _index_sellers_from_relay_json(obj: Any, out: dict[str, dict[str, str]]) -> None:
    """Все listing id + seller из больших Relay/Comet JSON на странице ленты."""
    if isinstance(obj, dict):
        lid = ""
        title = _dig_str(
            obj,
            "marketplace_listing_title",
            "group_commerce_item_title",
            "title",
        )
        typename = str(obj.get("__typename") or "")
        if not title and "Listing" in typename:
            title = _dig_str(obj, "marketplace_listing_title", "title") or "x"
        for key in ("id", "listing_id", "legacy_listing_id"):
            val = str(obj.get(key) or "").strip()
            if val.isdigit() and len(val) >= _MIN_LISTING_ID_LEN and title:
                lid = val
                break
        if lid:
            patch: dict[str, str] = {}
            for sk in _SELLER_BLOB_KEYS:
                blob = obj.get(sk)
                if isinstance(blob, dict):
                    tmp: dict[str, Any] = {}
                    if _apply_seller_blob(blob, lid, tmp):
                        for k in ("seller_id", "seller_name", "person_link"):
                            if tmp.get(k):
                                patch[k] = str(tmp[k])
            if patch:
                prev = out.get(lid, {})
                prev.update(patch)
                out[lid] = prev
        for val in obj.values():
            _index_sellers_from_relay_json(val, out)
    elif isinstance(obj, list):
        for val in obj:
            _index_sellers_from_relay_json(val, out)


_ITEM_NEAR_PROFILE_RE = (
    re.compile(
        rf"/marketplace/item/(?P<lid>\d{{{_MIN_LISTING_ID_LEN},}})"
        rf"[\s\S]{{0,55000}}?"
        rf"/marketplace/profile/(?P<pid>\d{{8,}})",
        re.I,
    ),
    re.compile(
        rf"/marketplace/profile/(?P<pid>\d{{8,}})"
        rf"[\s\S]{{0,55000}}?"
        rf"/marketplace/item/(?P<lid>\d{{{_MIN_LISTING_ID_LEN},}})",
        re.I,
    ),
)


def _seller_name_for_profile_in_html(html: str, profile_id: str) -> str:
    for pat in (
        re.compile(
            rf'"/marketplace/profile/{re.escape(profile_id)}"'
            rf'[\s\S]{{0,8000}}?"name"\s*:\s*"((?:\\.|[^"\\])*)"',
            re.I,
        ),
        re.compile(
            rf"/marketplace/profile/{re.escape(profile_id)}[^>]*>([^<]{{2,80}})<",
            re.I,
        ),
        re.compile(
            rf'aria-label="(?:View|Näytä|Voir|See)\s+([^"]{{2,80}})"'
            rf'[\s\S]{{0,3000}}?/marketplace/profile/{re.escape(profile_id)}',
            re.I,
        ),
    ):
        m = pat.search(html)
        if m:
            name = _unescape(m.group(1)).strip()
            if len(name) >= 2 and not name.startswith("Listing "):
                return name
    return ""


def _index_sellers_from_dom(html: str) -> dict[str, dict[str, str]]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict[str, str]] = {}
    for item_a in soup.select('a[href*="/marketplace/item/"]'):
        href = item_a.get("href") or ""
        m = _LISTING_ID_RE.search(href)
        if not m:
            continue
        lid = m.group(1)
        if len(lid) < _MIN_LISTING_ID_LEN:
            continue
        node = item_a.parent
        for _ in range(12):
            if not node:
                break
            prof = node.select_one('a[href*="/marketplace/profile/"]') if hasattr(
                node, "select_one"
            ) else None
            if prof:
                href2 = prof.get("href") or ""
                m2 = _PROFILE_LINK_RE.search(href2)
                if m2:
                    pid = m2.group(1)
                    if pid != lid:
                        name = (prof.get_text(strip=True) or "")[:80]
                        out[lid] = {
                            "seller_id": pid,
                            "person_link": (
                                f"https://www.facebook.com/marketplace/profile/{pid}/"
                            ),
                            "seller_name": name,
                        }
                break
            node = getattr(node, "parent", None)
    return out


def _proximity_listing_sellers(html: str) -> dict[str, dict[str, str]]:
    """
    VOID: в ленте listing и profile часто в разных JSON-блоках.
    Связываем ближайший profile/ID к каждому /marketplace/item/ID.
    """
    if not html:
        return {}
    listings: list[tuple[int, str]] = []
    for m in _LISTING_ID_RE.finditer(html):
        lid = m.group(1)
        if len(lid) >= _MIN_LISTING_ID_LEN and lid not in {x[1] for x in listings}:
            listings.append((m.start(), lid))
    profiles: list[tuple[int, str]] = []
    for m in _PROFILE_LINK_RE.finditer(html):
        pid = m.group(1)
        if pid.isdigit():
            profiles.append((m.start(), pid))
    if not listings or not profiles:
        return {}
    out: dict[str, dict[str, str]] = {}
    for lpos, lid in listings:
        best_pid = ""
        best_dist = _LISTING_PROFILE_MAX_DIST + 1
        for ppos, pid in profiles:
            if pid == lid:
                continue
            dist = abs(ppos - lpos)
            if dist < best_dist:
                best_dist = dist
                best_pid = pid
        if best_pid and best_dist <= _LISTING_PROFILE_MAX_DIST:
            ent = {
                "seller_id": best_pid,
                "person_link": (
                    f"https://www.facebook.com/marketplace/profile/{best_pid}/"
                ),
            }
            nm = _seller_name_for_profile_in_html(html, best_pid)
            if nm:
                ent["seller_name"] = nm
            out[lid] = ent
    return out


def _extract_item_doc_id(html: str) -> str | None:
    global _cached_item_doc_id
    env = (os.getenv("FB_MARKETPLACE_ITEM_DOC_ID") or "").strip()
    if env:
        return env
    if _cached_item_doc_id:
        return _cached_item_doc_id
    if not html:
        return None
    for pat in _ITEM_DOC_PATTERNS:
        m = pat.search(html)
        if m:
            _cached_item_doc_id = m.group(1)
            logger.info("discovered item doc_id %s", _cached_item_doc_id)
            return _cached_item_doc_id
    return None


def _seller_patch_from_graphql_payload(
    data: Any, listing_id: str
) -> dict[str, Any]:
    found: dict[str, Any] = {}
    if _deep_seller_patch(data, listing_id, found):
        return found

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if str(node.get("id") or "") == listing_id:
                for key in _SELLER_BLOB_KEYS:
                    blob = node.get(key)
                    if isinstance(blob, dict):
                        _apply_seller_blob(blob, listing_id, found)
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)

    walk(data)
    return found


async def _enrich_via_graphql_item(
    token: AccountToken,
    listing_id: str,
    *,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float,
    country: str | None,
    hint_html: str = "",
) -> dict[str, Any]:
    doc_id = _extract_item_doc_id(hint_html)
    if not doc_id:
        return {}
    if not token.access_token and not token.cookies:
        return {}

    variable_sets = [
        {"listingID": listing_id, "scale": 2},
        {"listing_id": listing_id, "scale": 2},
        {"id": listing_id},
        {"forSaleItemID": listing_id},
    ]
    referer = f"https://www.facebook.com/marketplace/item/{listing_id}/"
    if country:
        referer = with_country_geo(referer, country)
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Accept-Language": _accept_language(country),
        "Origin": "https://www.facebook.com",
        "Referer": referer,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with _session_for_proxy(proxy_url) as session:
        for variables in variable_sets:
            body: dict[str, str] = {
                "doc_id": doc_id,
                "variables": json.dumps(variables),
            }
            if token.access_token:
                body["access_token"] = token.access_token
            try:
                async with session.post(
                    "https://www.facebook.com/api/graphql/",
                    headers=headers,
                    data=body,
                    timeout=timeout,
                ) as resp:
                    raw = await resp.text(errors="ignore")
                    if resp.status >= 400:
                        continue
                    data = json.loads(raw)
            except Exception as e:
                logger.debug("item graphql %s: %s", listing_id, e)
                continue
            patch = _seller_patch_from_graphql_payload(data, listing_id)
            if patch.get("seller_id") or patch.get("person_link"):
                logger.info("item graphql ok %s profile=%s", listing_id, patch.get("seller_id", "")[:12])
                return patch
    return {}


def build_feed_seller_index(html: str) -> dict[str, dict[str, str]]:
    """Сводная карта listing_id → seller с целой страницы ленты."""
    if not html:
        return {}
    out: dict[str, dict[str, str]] = {}
    for lid, ent in _proximity_listing_sellers(html).items():
        out[lid] = dict(ent)
    for raw in _SCRIPT_JSON_RE.findall(html):
        try:
            _index_sellers_from_relay_json(json.loads(raw), out)
        except json.JSONDecodeError:
            continue
    for pat in _ITEM_NEAR_PROFILE_RE:
        for m in pat.finditer(html):
            lid, pid = m.group("lid"), m.group("pid")
            if pid == lid:
                continue
            ent = out.setdefault(lid, {})
            ent.setdefault("seller_id", pid)
            ent.setdefault(
                "person_link",
                f"https://www.facebook.com/marketplace/profile/{pid}/",
            )
    for lid, ent in list(out.items()):
        pid = ent.get("seller_id", "")
        if pid and not ent.get("seller_name"):
            nm = _seller_name_for_profile_in_html(html, pid)
            if nm:
                ent["seller_name"] = nm
    for lid, ent in _index_sellers_from_dom(html).items():
        prev = out.setdefault(lid, {})
        for k, v in ent.items():
            if v and not prev.get(k):
                prev[k] = v
    return out


def apply_feed_seller_index(item: MarketplaceListing, index: dict[str, dict[str, str]]) -> None:
    patch = index.get(item.listing_id)
    if patch:
        _merge_listing(item, patch)


def _patch_seller_from_item_page(html: str, listing_id: str) -> dict[str, Any]:
    """Страница item: person_link и item_person_name как в VOID (первый profile/ в HTML)."""
    if not html or f"/marketplace/item/{listing_id}" not in html:
        return {}
    patch = _patch_seller_from_html(html, listing_id)
    if patch.get("seller_id") or patch.get("person_link"):
        return patch
    for m in _PROFILE_LINK_RE.finditer(html):
        pid = m.group(1)
        if pid == listing_id:
            continue
        out: dict[str, Any] = {
            "seller_id": pid,
            "person_link": f"https://www.facebook.com/marketplace/profile/{pid}/",
        }
        name = _seller_name_for_profile_in_html(html, pid)
        if not name:
            name = _first_match(_SELLER_RE, html)
        if name:
            out["seller_name"] = name
        return out
    return patch


def _seller_name_near_listing_in_html(html: str, listing_id: str) -> str:
    """Имя из aria-label / JSON в окне вокруг ссылки на объявление."""
    pos = html.find(f"/marketplace/item/{listing_id}")
    if pos < 0:
        return ""
    chunk = html[max(0, pos - _WINDOW_BEFORE) : pos + _WINDOW_AFTER]
    for pat in (
        re.compile(
            r'aria-label="(?:View|Näytä|Voir|Profil von|Profilo di)\s+([^"]{2,80})',
            re.I,
        ),
        re.compile(
            rf'"{listing_id}"[\s\S]{{0,12000}}?"marketplace_listing_seller_name"\s*:\s*"((?:\\.|[^"\\])*)"',
            re.I,
        ),
        re.compile(
            rf'"{listing_id}"[\s\S]{{0,12000}}?"name"\s*:\s*"((?:\\.|[^"\\])*)"',
        ),
    ):
        m = pat.search(chunk)
        if m:
            name = _unescape(m.group(1)).strip()
            if len(name) >= 2 and not name.startswith("Listing "):
                return name
    return ""


def _patch_seller_from_html(html: str, listing_id: str) -> dict[str, Any]:
    """ID продавца из JSON/HTML рядом с listing_id (как VOID person_link)."""
    if not html or not listing_id:
        return {}
    for raw in _SCRIPT_JSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found: dict[str, Any] = {}
        if _deep_seller_patch(data, listing_id, found):
            return found
    pos = html.find(f"/marketplace/item/{listing_id}")
    if pos >= 0:
        chunk = html[max(0, pos - _WINDOW_BEFORE) : pos + _WINDOW_AFTER]
        for m in _PROFILE_PHP_RE.finditer(chunk):
            sid = m.group(1)
            if sid != listing_id:
                return {
                    "seller_id": sid,
                    "person_link": f"https://www.facebook.com/profile.php?id={sid}",
                }
    for pat in (_SELLER_NEAR_LISTING_RE, _PROFILE_BEFORE_LISTING_RE):
        for m in pat.finditer(html):
            if m.group("lid") == listing_id:
                pid = m.group("pid")
                if pid != listing_id:
                    return {
                        "seller_id": pid,
                        "person_link": (
                            f"https://www.facebook.com/marketplace/profile/{pid}/"
                        ),
                    }
    pos = html.find(f"/marketplace/item/{listing_id}")
    if pos >= 0:
        chunk = html[max(0, pos - _WINDOW_BEFORE) : pos + _WINDOW_AFTER]
        patch = _parse_chunk(chunk, listing_id)
        if patch.get("seller_id") or patch.get("person_link") or patch.get("seller_name"):
            return {
                k: patch[k]
                for k in ("seller_id", "person_link", "seller_name")
                if patch.get(k)
            }
    name = _seller_name_near_listing_in_html(html, listing_id)
    if name:
        return {"seller_name": name}
    return {}


def _parse_chunk(chunk: str, lid: str) -> dict[str, Any]:
    seller_id = _first_match(_SELLER_ID_RE, chunk)
    if not seller_id:
        seller_id = _first_match(_SELLER_ID_ALT_RE, chunk)
    profile_m = _PROFILE_LINK_RE.search(chunk)
    person_link = ""
    if profile_m:
        person_link = f"https://www.facebook.com/marketplace/profile/{profile_m.group(1)}/"
    elif seller_id:
        person_link = f"https://www.facebook.com/marketplace/profile/{seller_id}/"

    ads_raw = _first_match(_ADS_COUNT_RE, chunk)
    rating_raw = _first_match(_RATING_RE, chunk)
    created_date = _first_match(_REL_TIME_RE, chunk)
    created_ts_raw = _first_match(_CREATION_UNIX_RE, chunk)
    created_ts = int(created_ts_raw) if created_ts_raw else None
    if not created_ts and created_date:
        hours = _parse_relative_time_hours(created_date)
        if hours is not None:
            created_ts = int(time.time() - hours * 3600)

    return {
        "title": _first_match(_TITLE_RE, chunk),
        "price": _first_match(_PRICE_RE, chunk),
        "seller_id": seller_id,
        "seller_name": _first_match(_SELLER_RE, chunk),
        "photo": _first_match(_PHOTO_RE, chunk),
        "location": _first_match(_LOCATION_RE, chunk),
        "item_desc": _first_match(_DESC_RE, chunk),
        "person_link": person_link,
        "created_date": created_date,
        "created_timestamp": created_ts,
        "person_reg_date": _first_match(_JOIN_TIME_RE, chunk),
        "gender": _gender_ru(_first_match(_GENDER_RE, chunk)),
        "ads_number": _int_or_none(ads_raw) if ads_raw else None,
        "rating": float(rating_raw) if rating_raw else None,
    }


def _dig_str(obj: Any, *keys: str) -> str:
    if not isinstance(obj, dict):
        return ""
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            t = val.get("text")
            if isinstance(t, str) and t.strip():
                return t.strip()
    return ""


def _listing_from_graph_node(node: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(node.get("product_item"), dict):
        pi = node["product_item"]
        if isinstance(pi.get("for_sale_item"), dict):
            return _listing_from_graph_node(pi["for_sale_item"])

    fs = node.get("for_sale_item") or node.get("listing") or node
    if not isinstance(fs, dict):
        fs = node

    lid = str(fs.get("id") or node.get("id") or "").strip()
    title = _dig_str(fs, "marketplace_listing_title", "group_commerce_item_title", "title")
    if not title:
        return None
    if not lid or not lid.isdigit() or len(lid) < _MIN_LISTING_ID_LEN:
        return None

    price = _dig_str(fs, "formatted_price", "formatted_amount", "listing_price")
    if not price:
        for key in ("formatted_price", "listing_price", "price"):
            price_obj = fs.get(key)
            if isinstance(price_obj, dict):
                price = _dig_str(price_obj, "text", "amount")
            if price:
                break

    location = _dig_str(fs, "marketplace_listing_location", "location")
    seller_name = ""
    seller_id = ""
    for key in _SELLER_BLOB_KEYS:
        blob = fs.get(key) or node.get(key)
        if not isinstance(blob, dict):
            continue
        seller_name = seller_name or _seller_name_from_blob(blob)
        sid = str(blob.get("id") or "").strip()
        if sid.isdigit() and sid != lid:
            seller_id = sid
            break

    photo = ""
    photo_obj = fs.get("primary_listing_photo") or fs.get("listing_photo")
    if isinstance(photo_obj, dict):
        img = photo_obj.get("image") or photo_obj
        if isinstance(img, dict):
            photo = str(img.get("uri") or "").strip()

    person_link = ""
    if seller_id:
        person_link = f"https://www.facebook.com/marketplace/profile/{seller_id}/"

    created = ""
    created_ts: int | None = None
    ct = fs.get("creation_time") or fs.get("listing_created_time")
    if isinstance(ct, dict):
        created = _dig_str(ct, "text")
        created_ts = _dig_timestamp(ct)
    elif isinstance(ct, (int, float)):
        created_ts = int(ct)

    reg = ""
    jt = fs.get("join_time") or fs.get("seller_join_time")
    if isinstance(jt, dict):
        reg = _dig_str(jt, "text")

    ads = fs.get("active_listing_count") or fs.get("marketplace_listing_count")
    ads_n = int(ads) if isinstance(ads, (int, float)) else None

    rating = fs.get("marketplace_rating") or fs.get("rating")
    rating_v: float | int = 0
    if isinstance(rating, (int, float)):
        rating_v = rating

    gender = _gender_ru(_dig_str(fs, "gender"))

    return {
        "listing_id": lid,
        "title": title,
        "price": price,
        "seller_id": seller_id,
        "seller_name": seller_name,
        "photo": photo,
        "location": location,
        "item_desc": _dig_str(fs, "marketplace_listing_description", "description"),
        "person_link": person_link,
        "created_date": created,
        "created_timestamp": created_ts,
        "person_reg_date": reg,
        "ads_number": ads_n,
        "gender": gender,
        "rating": rating_v,
    }


def _extract_feed_cursor(obj: Any) -> tuple[str | None, bool]:
    found: dict[str, Any] = {"cursor": None, "has_next": False}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            pi = node.get("page_info")
            if isinstance(pi, dict):
                ec = pi.get("end_cursor")
                if isinstance(ec, str) and ec:
                    found["cursor"] = ec
                if pi.get("has_next_page"):
                    found["has_next"] = True
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)

    walk(obj)
    return found["cursor"], bool(found["has_next"])


def _extract_cursor_from_html(html: str) -> tuple[str | None, bool]:
    for raw in _SCRIPT_JSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cursor, has_next = _extract_feed_cursor(data)
        if cursor:
            return cursor, has_next
    return None, False


def _walk_marketplace_json(obj: Any, out: dict[str, MarketplaceListing]) -> None:
    if isinstance(obj, dict):
        patch = _listing_from_graph_node(obj)
        if patch and patch.get("listing_id"):
            lid = str(patch["listing_id"])
            if lid not in out:
                out[lid] = MarketplaceListing(listing_id=lid, category="")
            _merge_listing(out[lid], patch)
        for val in obj.values():
            _walk_marketplace_json(val, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_marketplace_json(item, out)


def _parse_embedded_scripts(html: str) -> dict[str, MarketplaceListing]:
    by_id: dict[str, MarketplaceListing] = {}
    for raw in _SCRIPT_JSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _walk_marketplace_json(data, by_id)
    return by_id


def _first_group(matches: list) -> list[str]:
    out: list[str] = []
    for m in matches:
        g = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else "")
        if g:
            out.append(_unescape(g))
    return out


def _collect_listing_ids(html: str) -> list[str]:
    """Только id из ссылок /marketplace/item/ — без мусора из JSON."""
    seen: list[str] = []
    for m in _LISTING_ID_RE.finditer(html):
        lid = m.group(1)
        if len(lid) < _MIN_LISTING_ID_LEN:
            continue
        if lid not in seen:
            seen.append(lid)
    return seen


def _merge_item_from_html(item: MarketplaceListing, html: str) -> None:
    embedded = _parse_embedded_scripts(html)
    best = embedded.get(item.listing_id)
    if not best:
        parsed = _parse_html(html, item.category)
        for p in parsed:
            if p.listing_id == item.listing_id:
                best = p
                break
    if best:
        _merge_listing(
            item,
            {
                "title": best.title,
                "price": best.price,
                "seller_id": best.seller_id,
                "seller_name": best.seller_name,
                "photo": best.photo,
                "location": best.location,
                "item_desc": best.item_desc,
                "person_link": best.person_link,
                "created_date": best.created_date,
                "created_timestamp": best.created_timestamp,
                "person_reg_date": best.person_reg_date,
                "gender": best.gender,
                "ads_number": best.ads_number,
                "rating": best.rating,
            },
        )
    seller_patch = _patch_seller_from_item_page(html, item.listing_id)
    if seller_patch:
        _merge_listing(item, seller_patch)
    idx = build_feed_seller_index(html)
    apply_feed_seller_index(item, idx)


async def enrich_listing(
    token: AccountToken,
    item: MarketplaceListing,
    *,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float = 20.0,
    country: str | None = None,
) -> MarketplaceListing:
    """Догружает цену, описание, продавца с карточки объявления."""
    from services.seller_blacklist import _profile_id

    lid = item.listing_id
    feed_hint = _category_feed_html.get() or ""
    apply_feed_seller_index(item, build_feed_seller_index(feed_hint))
    if _profile_id(item):
        return item

    gql_patch = await _enrich_via_graphql_item(
        token,
        lid,
        user_agent=user_agent,
        proxy_url=proxy_url,
        timeout_sec=timeout_sec,
        country=country,
        hint_html=feed_hint,
    )
    if gql_patch:
        _merge_listing(item, gql_patch)
        if _profile_id(item):
            return item

    locale_q = ""
    if country == "fi":
        locale_q = "?locale=fi_FI"
    elif country == "ch":
        locale_q = "?locale=de_CH"
    urls = [
        f"https://mbasic.facebook.com/marketplace/item/{lid}/",
        f"https://www.facebook.com/marketplace/item/{lid}{locale_q}",
        f"https://m.facebook.com/marketplace/item/{lid}/",
    ]
    if item.link:
        clean = item.link.split("?")[0].rstrip("/")
        urls.insert(1, f"{clean}{locale_q}")
    referer = "https://www.facebook.com/marketplace/"
    if country and country in COUNTRY_LOCATIONS:
        slugs = COUNTRY_LOCATIONS[country].get("marketplace_slugs") or []
        if slugs:
            referer = f"https://www.facebook.com/marketplace/{str(slugs[0]).strip('/')}/"
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": _accept_language(country),
        "Referer": referer,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    for url in urls:
        try:
            async with _session_for_proxy(proxy_url) as session:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status >= 400:
                        continue
                    html = await resp.text(errors="ignore")
        except Exception as e:
            logger.debug("enrich %s %s failed: %s", lid, url[:40], e)
            continue
        if _looks_like_login_wall(html):
            raise AccountTokenDeadError("login wall on item page")
        _extract_item_doc_id(html)
        _merge_item_from_html(item, html)
        sp = _patch_seller_from_item_page(html, lid)
        if sp:
            _merge_listing(item, sp)
        if not (item.seller_name or "").strip():
            nm = _seller_name_near_listing_in_html(html, lid)
            if nm:
                item.seller_name = nm
        if _profile_id(item):
            return item
        gql_patch = await _enrich_via_graphql_item(
            token,
            lid,
            user_agent=user_agent,
            proxy_url=proxy_url,
            timeout_sec=timeout_sec,
            country=country,
            hint_html=html,
        )
        if gql_patch:
            _merge_listing(item, gql_patch)
            if _profile_id(item):
                return item

    if not _profile_id(item):
        prof_n = feed_hint.count("/marketplace/profile/") if feed_hint else 0
        logger.info(
            "enrich %s: no person_link (feed profiles=%s, token_graphql=%s)",
            lid,
            prof_n,
            bool(token.access_token),
        )
    return item


def _seo_path_from_url(url: str) -> str:
    return url.replace("https://www.facebook.com/marketplace/", "").strip("/")


async def _fetch_graphql_category(
    token: AccountToken,
    *,
    seo_path: str,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float,
    category_label: str,
    doc_id: str,
    limit: int,
    cursor: str | None = None,
    country: str | None = None,
) -> tuple[list[MarketplaceListing], str | None, bool] | None:
    if not token.access_token:
        return None

    variables: dict[str, Any] = {
        "count": min(limit, _FEED_PAGE_SIZE),
        "cursor": cursor,
        "scale": 2,
        "seoURL": seo_path,
    }
    if country and country in COUNTRY_LOCATIONS:
        lid = COUNTRY_LOCATIONS[country].get("filter_location_id")
        if lid:
            variables["filterLocationID"] = str(lid)
            variables["filter_location_id"] = str(lid)
    referer = f"https://www.facebook.com/marketplace/{seo_path}/"
    if country:
        referer = with_country_geo(referer, country)
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Accept-Language": _accept_language(country),
        "Origin": "https://www.facebook.com",
        "Referer": referer,
    }
    body = {
        "doc_id": doc_id,
        "variables": json.dumps(variables),
        "access_token": token.access_token,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with _session_for_proxy(proxy_url) as session:
            async with session.post(
                "https://www.facebook.com/api/graphql/",
                headers=headers,
                data=body,
                timeout=timeout,
            ) as resp:
                raw = await resp.text(errors="ignore")
                if resp.status >= 400:
                    return None
                data = json.loads(raw)
    except Exception as e:
        logger.debug("graphql category failed %s: %s", seo_path, e)
        return None

    by_id: dict[str, MarketplaceListing] = {}
    _walk_marketplace_json(data, by_id)
    for lid, item in by_id.items():
        item.category = category_label
        if not item.link:
            item.link = f"https://www.facebook.com/marketplace/item/{lid}/"
    items = [x for x in by_id.values() if listing_is_valid(x)]
    next_cursor, has_next = _extract_feed_cursor(data)
    if items:
        logger.info(
            "graphql %s: %s items page cursor=%s next=%s",
            seo_path,
            len(items),
            bool(cursor),
            has_next,
        )
    return items, next_cursor, has_next


async def _fetch_page(
    token: AccountToken,
    *,
    url: str,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float,
    category_label: str,
    graphql_doc_id: str | None = None,
    cursor: str | None = None,
    country: str | None = None,
) -> tuple[list[MarketplaceListing], dict[str, Any], str | None, bool]:
    seo = _seo_path_from_url(url)
    feed_doc = _graphql_doc_for_feed(graphql_doc_id)
    if feed_doc and cursor:
        gql = await _fetch_graphql_category(
            token,
            seo_path=seo,
            user_agent=user_agent,
            proxy_url=proxy_url,
            timeout_sec=timeout_sec,
            category_label=category_label,
            doc_id=feed_doc,
            limit=_FEED_PAGE_SIZE,
            cursor=cursor,
            country=country,
        )
        if gql:
            items, next_cursor, has_next = gql
            return items, {
                "html_len": 0,
                "link_count": len(items),
                "parsed": len(items),
                "source": "graphql",
            }, next_cursor, has_next
        return [], {"html_len": 0, "link_count": 0, "parsed": 0, "source": "graphql"}, None, False

    if feed_doc and not cursor:
        gql = await _fetch_graphql_category(
            token,
            seo_path=seo,
            user_agent=user_agent,
            proxy_url=proxy_url,
            timeout_sec=timeout_sec,
            category_label=category_label,
            doc_id=feed_doc,
            limit=_FEED_PAGE_SIZE,
            cursor=None,
            country=country,
        )
        if gql and gql[0]:
            items, next_cursor, has_next = gql
            return items, {
                "html_len": 0,
                "link_count": len(items),
                "parsed": len(items),
                "source": "graphql",
            }, next_cursor, has_next
        logger.info(
            "graphql empty for %s — fallback HTML (норма без FB_MARKETPLACE_DOC_ID)",
            seo[:48],
        )

    referer = "https://www.facebook.com/marketplace/"
    if country and country in COUNTRY_LOCATIONS:
        slugs = COUNTRY_LOCATIONS[country].get("marketplace_slugs") or []
        root = str(slugs[0]) if slugs else ""
        if root:
            referer = with_country_geo(urljoin(_FB_BASE, f"{root}/"), country)
        referer = with_country_geo(urljoin(_FB_BASE, f"{seo}/"), country)
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": _accept_language(country),
        "Referer": referer,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with _session_for_proxy(proxy_url) as session:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            html = await resp.text(errors="ignore")
            if resp.status >= 400:
                raise RuntimeError(f"Facebook HTTP {resp.status}")
            if "login" in str(resp.url).lower():
                raise AccountTokenDeadError("redirect to login")

    meta: dict[str, Any] = {
        "html_len": len(html),
        "link_count": html.count("/marketplace/item/"),
    }
    if len(html) < 3_000_000:
        meta["raw_html"] = html
    if _looks_like_login_wall(html):
        raise AccountTokenDeadError("login wall in HTML")

    items = _parse_html(html, category_label)
    meta["parsed"] = len(items)
    meta["source"] = "html"
    doc_from_html = _extract_feed_doc_id_from_html(html)
    if doc_from_html:
        meta["discovered_doc_id"] = doc_from_html
    next_cursor, has_next = _extract_cursor_from_html(html)
    effective_doc = feed_doc or doc_from_html
    if not next_cursor and items:
        has_next = len(items) >= 8 and bool(effective_doc)
    if items and not has_next and not next_cursor and not doc_from_html:
        logger.warning(
            "feed %s items, no cursor/doc_id — only 1 HTML page; set FB_MARKETPLACE_DOC_ID",
            len(items),
        )
    return items, meta, next_cursor, has_next


def _parse_html(html: str, category_label: str) -> list[MarketplaceListing]:
    by_id: dict[str, MarketplaceListing] = _parse_embedded_scripts(html)
    seller_index = build_feed_seller_index(html)

    ids_in_order = _collect_listing_ids(html)
    titles = _first_group(list(_TITLE_RE.finditer(html)))
    prices = _first_group(list(_PRICE_RE.finditer(html)))
    sellers = _first_group(list(_SELLER_RE.finditer(html)))
    photos = _first_group(list(_PHOTO_RE.finditer(html)))
    locs = [_unescape(m.group(1) or m.group(2) or "") for m in _LOCATION_RE.finditer(html)]

    for idx, lid in enumerate(ids_in_order):
        pos = html.find(f"/marketplace/item/{lid}")
        if pos < 0:
            continue
        chunk = html[max(0, pos - _WINDOW_BEFORE) : pos + _WINDOW_AFTER]
        patch = _parse_chunk(chunk, lid)
        embedded = by_id.get(lid)
        if embedded:
            if not patch.get("seller_id") and embedded.seller_id:
                patch["seller_id"] = embedded.seller_id
            if not patch.get("seller_name") and embedded.seller_name:
                patch["seller_name"] = embedded.seller_name
            if not patch.get("person_link") and embedded.person_link:
                patch["person_link"] = embedded.person_link
        if not patch.get("title"):
            patch["title"] = titles[idx] if idx < len(titles) else ""
        if not patch.get("price"):
            patch["price"] = prices[idx] if idx < len(prices) else ""
        if not patch.get("seller_name"):
            patch["seller_name"] = sellers[idx] if idx < len(sellers) else ""
        if not patch.get("photo"):
            patch["photo"] = photos[idx] if idx < len(photos) else ""
        if not patch.get("location"):
            patch["location"] = locs[idx] if idx < len(locs) else ""
        if not (patch.get("title") or "").strip():
            continue
        if lid not in by_id:
            by_id[lid] = MarketplaceListing(
                listing_id=lid,
                link=f"https://www.facebook.com/marketplace/item/{lid}/",
                category=category_label,
            )
        _merge_listing(by_id[lid], patch)
        seller_patch = _patch_seller_from_html(html, lid)
        if seller_patch:
            _merge_listing(by_id[lid], seller_patch)
        apply_feed_seller_index(by_id[lid], seller_index)
        if not (by_id[lid].seller_name or "").strip():
            nm = _seller_name_near_listing_in_html(html, lid)
            if nm:
                by_id[lid].seller_name = nm
        by_id[lid].category = category_label

    out: list[MarketplaceListing] = []
    for item in by_id.values():
        if not item.link:
            item.link = f"https://www.facebook.com/marketplace/item/{item.listing_id}/"
        if listing_is_valid(item):
            out.append(item)
    return out


def listings_to_json(items: list[MarketplaceListing]) -> str:
    from parser.void_format import listings_to_void_json

    return listings_to_void_json(items)
