"""Сбор объявлений с Facebook Marketplace."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

import aiohttp
from aiohttp_socks import ProxyConnector

from data.preset_categories import COUNTRY_LOCATIONS
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
_SELLER_RE = re.compile(r'"marketplace_listing_seller_name"\s*:\s*"((?:\\.|[^"\\])*)"')
_PHOTO_RE = re.compile(r'"uri"\s*:\s*"(https://[^"]*scontent[^"]*)"')
_LOCATION_RE = re.compile(r'"city"\s*:\s*"([^"]+)"|"location_text"\s*:\s*\{\s*"text"\s*:\s*"([^"]+)"')
_DESC_RE = re.compile(
    r'"marketplace_listing_description"\s*:\s*"((?:\\.|[^"\\])*)"|'
    r'"redacted_description"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_SELLER_ID_RE = re.compile(r'"marketplace_listing_seller_id"\s*:\s*"(\d+)"')
_PROFILE_LINK_RE = re.compile(r"/marketplace/profile/(\d+)")
_REL_TIME_RE = re.compile(
    r'"(?:creation_time|listing_created_time|time_created)"\s*:\s*\{[^}]{0,400}?"text"\s*:\s*"([^"]+)"'
)
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
    photo: str = ""
    location: str = ""
    category: str = ""
    item_desc: str = ""
    person_link: str = ""
    created_date: str = ""
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
)
_UA_SPAM_HINTS = (
    "україн",
    "украин",
    "київ",
    "киев",
    "kyiv",
    "kiev",
    "львів",
    "lviv",
    "одес",
    "харків",
    "kharkiv",
    "dnipro",
    "запоріж",
    "poltava",
    "ukraine",
)
_MIN_LISTING_ID_LEN = 12


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


def listing_is_export_ready(item: MarketplaceListing, country: str | None = None) -> bool:
    """Карточка как в ленте VOID: название + цена и/или фото (не пустышка)."""
    return export_reject_reason(item, country) is None


def _price_hints_country(price: str, country: str) -> bool:
    p = price.lower()
    if country == "ch":
        return "chf" in p or "fr." in p or "sfr" in p
    if country == "fi":
        return "eur" in p or "€" in p
    return False


def export_reject_reason(item: MarketplaceListing, country: str | None = None) -> str | None:
    if not listing_is_valid(item):
        return "нет_заголовка"
    loc = (item.location or "").strip()
    price = (item.price or "").strip()
    if country and loc and not _country_location_ok(loc, country):
        return "чужая_страна"
    if country and loc and any(r in f" {loc.lower()} " for r in (_CH_REJECT if country == "ch" else _FI_REJECT)):
        return "чужая_страна"

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


def _country_location_ok(location: str, country: str | None) -> bool:
    """Страна целиком: не режем по одному городу; отсекаем явно чужие страны."""
    if not country:
        return True
    loc = f" {(location or '').lower()} "
    if country == "ch":
        if any(r in loc for r in _CH_REJECT):
            return False
        if not loc.strip():
            return True
        if any(h in loc for h in _CH_OK):
            return True
        return False
    if country == "fi":
        if any(r in loc for r in _FI_REJECT):
            return False
        if not loc.strip():
            return True
        return any(h in loc for h in _FI_OK)
    return True


def normalize_category_path(url_or_path: str) -> str:
    """
    https://www.facebook.com/marketplace/category/sports → category/sports
    sports → category/sports
    """
    path = (url_or_path or "").strip()
    if "facebook.com/marketplace/" in path:
        path = path.split("facebook.com/marketplace/", 1)[1]
    path = path.strip("/").split("?")[0]
    if not path:
        raise ValueError("Пустой путь категории")
    if not path.startswith("category/"):
        if path.startswith("category"):
            path = path.replace("category", "category/", 1)
        else:
            path = f"category/{path}"
    return path


def _category_slug(url_path: str) -> str:
    path = normalize_category_path(url_path)
    return path.split("/", 1)[1] if "/" in path else path


def build_category_url(url_path: str, *, marketplace_root: str | None = None) -> str:
    """
    Без страны: marketplace/category/sports/
    По стране: marketplace/switzerland/category/sports/ или marketplace/zurich/category/sports/
    """
    cat_path = normalize_category_path(url_path)
    slug = _category_slug(url_path)
    if marketplace_root:
        root = marketplace_root.strip("/")
        return urljoin(_FB_BASE, f"{root}/category/{slug}/")
    return urljoin(_FB_BASE, f"{cat_path}/")


def urls_for_country_category(country: str, url_path: str) -> list[str]:
    """Только регионы выбранной страны (без глобального marketplace/category/…)."""
    cfg = COUNTRY_LOCATIONS.get(country) or {}
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for slug in cfg.get("marketplace_slugs") or []:
        add(build_category_url(url_path, marketplace_root=slug))
    for hub in cfg.get("region_hubs") or []:
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
    graphql_doc_id: str | None = None,
) -> list[MarketplaceListing]:
    """Категория; при CH/FI — обход регионов страны, фильтр по стране в объявлении."""
    if country and country in COUNTRY_LOCATIONS:
        urls = urls_for_country_category(country, url_path)
    else:
        urls = [build_category_url(url_path)]

    seen_ids: set[str] = set()
    out: list[MarketplaceListing] = []
    total_urls = len(urls)

    for i, url in enumerate(urls, start=1):
        if should_stop and should_stop():
            break
        if len(out) >= limit:
            break
        if on_url_progress:
            short = url.replace("https://www.facebook.com/marketplace/", "")[:48]
            await on_url_progress(i, total_urls, short)
        logger.info("GET %s", url)
        try:
            batch, meta = await _fetch_page(
                token,
                url=url,
                user_agent=user_agent,
                proxy_url=proxy_url,
                timeout_sec=timeout_sec,
                category_label=category_label,
                graphql_doc_id=graphql_doc_id,
            )
            logger.info(
                "parsed %s items from %s (html=%s, links=%s)",
                len(batch),
                short if on_url_progress else url,
                meta.get("html_len"),
                meta.get("link_count"),
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

            if batch and not page_new:
                logger.debug(
                    "url %s: parsed %s, 0 new (duplicates or invalid)",
                    short if on_url_progress else url,
                    len(batch),
                )
            if on_page_found and page_new:
                await on_page_found(len(page_new))
            if on_page_items and page_new:
                await on_page_items(page_new)
            if len(out) >= limit:
                break
        except RuntimeError as e:
            if "HTTP 400" in str(e) or "HTTP 404" in str(e):
                logger.info("skip url %s: %s", url, e)
                continue
            raise
        except Exception as e:
            logger.warning("skip url %s: %s", url, e)
            continue

    logger.info(
        "category %s country=%s urls=%s collected=%s",
        category_label,
        country or "all",
        len(urls),
        len(out),
    )
    return out[:limit]


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


def _parse_chunk(chunk: str, lid: str) -> dict[str, Any]:
    seller_id = _first_match(_SELLER_ID_RE, chunk)
    profile_m = _PROFILE_LINK_RE.search(chunk)
    person_link = ""
    if profile_m:
        person_link = f"https://www.facebook.com/marketplace/profile/{profile_m.group(1)}/"
    elif seller_id:
        person_link = f"https://www.facebook.com/marketplace/profile/{seller_id}/"

    ads_raw = _first_match(_ADS_COUNT_RE, chunk)
    rating_raw = _first_match(_RATING_RE, chunk)

    return {
        "title": _first_match(_TITLE_RE, chunk),
        "price": _first_match(_PRICE_RE, chunk),
        "seller_name": _first_match(_SELLER_RE, chunk),
        "photo": _first_match(_PHOTO_RE, chunk),
        "location": _first_match(_LOCATION_RE, chunk),
        "item_desc": _first_match(_DESC_RE, chunk),
        "person_link": person_link,
        "created_date": _first_match(_REL_TIME_RE, chunk),
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
    seller = fs.get("marketplace_listing_seller") or fs.get("seller")
    seller_name = ""
    seller_id = ""
    if isinstance(seller, dict):
        seller_name = _dig_str(seller, "name", "marketplace_listing_seller_name")
        seller_id = str(seller.get("id") or "").strip()

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
    ct = fs.get("creation_time") or fs.get("listing_created_time")
    if isinstance(ct, dict):
        created = _dig_str(ct, "text")

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
        "seller_name": seller_name,
        "photo": photo,
        "location": location,
        "item_desc": _dig_str(fs, "marketplace_listing_description", "description"),
        "person_link": person_link,
        "created_date": created,
        "person_reg_date": reg,
        "ads_number": ads_n,
        "gender": gender,
        "rating": rating_v,
    }


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


async def enrich_listing(
    token: AccountToken,
    item: MarketplaceListing,
    *,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float = 18.0,
) -> MarketplaceListing:
    """Догружает цену, описание, даты с карточки объявления."""
    url = item.link or f"https://www.facebook.com/marketplace/item/{item.listing_id}/"
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.facebook.com/marketplace/",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with _session_for_proxy(proxy_url) as session:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status >= 400:
                    return item
                html = await resp.text(errors="ignore")
    except Exception as e:
        logger.debug("enrich %s failed: %s", item.listing_id, e)
        return item

    if _looks_like_login_wall(html):
        raise AccountTokenDeadError("login wall on item page")

    parsed = _parse_html(html, item.category)
    best: MarketplaceListing | None = None
    for p in parsed:
        if p.listing_id == item.listing_id:
            best = p
            break
    if not best and parsed:
        best = parsed[0]
    if best:
        _merge_listing(item, {
            "title": best.title,
            "price": best.price,
            "seller_name": best.seller_name,
            "photo": best.photo,
            "location": best.location,
            "item_desc": best.item_desc,
            "person_link": best.person_link,
            "created_date": best.created_date,
            "person_reg_date": best.person_reg_date,
            "gender": best.gender,
            "ads_number": best.ads_number,
            "rating": best.rating,
        })
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
) -> list[MarketplaceListing] | None:
    if not token.access_token:
        return None

    variables: dict[str, Any] = {
        "count": min(limit, 48),
        "cursor": None,
        "scale": 2,
        "seoURL": seo_path,
    }
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Origin": "https://www.facebook.com",
        "Referer": f"https://www.facebook.com/marketplace/{seo_path}/",
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
    if items:
        logger.info("graphql %s: %s items (doc_id=%s)", seo_path, len(items), doc_id[:8])
    return items or None


async def _fetch_page(
    token: AccountToken,
    *,
    url: str,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float,
    category_label: str,
    graphql_doc_id: str | None = None,
) -> tuple[list[MarketplaceListing], dict[str, int]]:
    seo = _seo_path_from_url(url)
    if graphql_doc_id:
        gql_items = await _fetch_graphql_category(
            token,
            seo_path=seo,
            user_agent=user_agent,
            proxy_url=proxy_url,
            timeout_sec=timeout_sec,
            category_label=category_label,
            doc_id=graphql_doc_id,
            limit=80,
        )
        if gql_items:
            return gql_items, {
                "html_len": 0,
                "link_count": len(gql_items),
                "parsed": len(gql_items),
                "source": "graphql",
            }

    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.facebook.com/marketplace/",
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

    meta = {
        "html_len": len(html),
        "link_count": html.count("/marketplace/item/"),
    }
    if _looks_like_login_wall(html):
        raise AccountTokenDeadError("login wall in HTML")

    items = _parse_html(html, category_label)
    meta["parsed"] = len(items)
    return items, meta


def _parse_html(html: str, category_label: str) -> list[MarketplaceListing]:
    by_id: dict[str, MarketplaceListing] = _parse_embedded_scripts(html)

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
