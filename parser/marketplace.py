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
from parser.account_token import AccountToken, cookies_header

logger = logging.getLogger(__name__)

_FB_BASE = "https://www.facebook.com/marketplace/"
_LISTING_ID_RE = re.compile(r"/marketplace/item/(\d+)")
_LISTING_ID_JSON_RE = re.compile(r'"(?:listing_)?id"\s*:\s*"(\d{8,})"')
_TITLE_RE = re.compile(
    r'"marketplace_listing_title"\s*:\s*"((?:\\.|[^"\\])*)"|"title"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_PRICE_RE = re.compile(
    r'"formatted_amount"\s*:\s*"((?:\\.|[^"\\])*)"|"amount"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_SELLER_RE = re.compile(r'"marketplace_listing_seller_name"\s*:\s*"((?:\\.|[^"\\])*)"')
_PHOTO_RE = re.compile(r'"uri"\s*:\s*"(https://[^"]*scontent[^"]*)"')
_LOCATION_RE = re.compile(r'"city"\s*:\s*"([^"]+)"|"location_text"\s*:\s*\{\s*"text"\s*:\s*"([^"]+)"')


@dataclass
class MarketplaceListing:
    listing_id: str
    title: str
    price: str
    link: str
    seller_name: str
    photo: str
    location: str
    category: str

    def to_export_dict(self) -> dict[str, Any]:
        return {
            "item_title": self.title,
            "item_price": self.price,
            "item_link": self.link,
            "item_person_name": self.seller_name,
            "item_photo": self.photo,
            "location": self.location,
            "listing_id": self.listing_id,
            "category": self.category,
        }


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
    "switzerland",
    "ch-",
    " zürich",
    " zurich",
    " geneva",
    " genève",
    " bern",
    " basel",
    " lausanne",
    " lugano",
    " winterthur",
    " luzern",
    " lucerne",
    " st. gallen",
    " st gallen",
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
        return any(h in loc for h in _CH_OK)
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
    """URL по всей стране: общий slug + крупные города (регионы FB)."""
    cfg = COUNTRY_LOCATIONS.get(country) or {}
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    add(build_category_url(url_path))
    for slug in cfg.get("marketplace_slugs") or []:
        add(build_category_url(url_path, marketplace_root=slug))
    for hub in cfg.get("region_hubs") or []:
        add(build_category_url(url_path, marketplace_root=hub))
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
            )
            logger.info(
                "parsed %s items from %s (html=%s, links=%s)",
                len(batch),
                short if on_url_progress else url,
                meta.get("html_len"),
                meta.get("link_count"),
            )
        except RuntimeError as e:
            if "HTTP 400" in str(e) or "HTTP 404" in str(e):
                logger.info("skip url %s: %s", url, e)
                continue
            raise
        except Exception as e:
            logger.warning("skip url %s: %s", url, e)
            continue

        for item in batch:
            if item.listing_id in seen_ids:
                continue
            if country and not _country_location_ok(item.location, country):
                continue
            seen_ids.add(item.listing_id)
            out.append(item)
            if len(out) >= limit:
                break

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


def _looks_like_login_wall(html: str) -> bool:
    h = html.lower()
    if "id=\"loginform\"" in h or 'id="loginform"' in h:
        return True
    if "checkpoint" in h and len(html) < 800_000:
        return True
    if "marketplace/item/" not in h and ("login" in h[:8000] or "log in" in h[:8000]):
        return True
    return False


def _first_group(matches: list) -> list[str]:
    out: list[str] = []
    for m in matches:
        g = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else "")
        if g:
            out.append(_unescape(g))
    return out


def _collect_listing_ids(html: str) -> list[str]:
    seen: list[str] = []
    for pattern in (_LISTING_ID_RE, _LISTING_ID_JSON_RE):
        for m in pattern.finditer(html):
            lid = m.group(1)
            if lid not in seen:
                seen.append(lid)
    return seen


async def _fetch_page(
    token: AccountToken,
    *,
    url: str,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float,
    category_label: str,
) -> tuple[list[MarketplaceListing], dict[str, int]]:
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
                raise RuntimeError("Токен аккаунта недействителен — вставь новую строку")

    meta = {
        "html_len": len(html),
        "link_count": html.count("/marketplace/item/"),
    }
    if _looks_like_login_wall(html):
        raise RuntimeError(
            "Facebook отдал страницу входа — обнови токен аккаунта (cookies истекли)"
        )

    return _parse_html(html, category_label), meta


def _parse_html(html: str, category_label: str) -> list[MarketplaceListing]:
    by_id: dict[str, MarketplaceListing] = {}
    titles = _first_group(list(_TITLE_RE.finditer(html)))
    prices = _first_group(list(_PRICE_RE.finditer(html)))
    sellers = _first_group(list(_SELLER_RE.finditer(html)))
    photos = _first_group(list(_PHOTO_RE.finditer(html)))
    locs: list[str] = []
    for m in _LOCATION_RE.finditer(html):
        locs.append(_unescape(m.group(1) or m.group(2) or ""))

    ids_in_order = _collect_listing_ids(html)

    for i, lid in enumerate(ids_in_order):
        title = titles[i] if i < len(titles) else ""
        if not title and titles:
            title = titles[0]
        if not title:
            title = f"Listing {lid}"
        by_id[lid] = MarketplaceListing(
            listing_id=lid,
            title=title,
            price=prices[i] if i < len(prices) else "",
            link=f"https://www.facebook.com/marketplace/item/{lid}/",
            seller_name=sellers[i] if i < len(sellers) else "",
            photo=photos[i] if i < len(photos) else "",
            location=locs[i] if i < len(locs) else "",
            category=category_label,
        )
    return list(by_id.values())


def listings_to_json(items: list[MarketplaceListing]) -> str:
    return json.dumps([x.to_export_dict() for x in items], ensure_ascii=False, indent=2)
