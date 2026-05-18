"""Сбор объявлений с Facebook Marketplace."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import aiohttp
from aiohttp_socks import ProxyConnector

from data.preset_categories import COUNTRY_LOCATIONS
from parser.account_token import AccountToken, cookies_header

logger = logging.getLogger(__name__)

_FB_BASE = "https://www.facebook.com/marketplace/"
_LISTING_ID_RE = re.compile(r"/marketplace/item/(\d+)")
_TITLE_RE = re.compile(r'"marketplace_listing_title"\s*:\s*"((?:\\.|[^"\\])*)"')
_PRICE_RE = re.compile(r'"formatted_amount"\s*:\s*"((?:\\.|[^"\\])*)"')
_SELLER_RE = re.compile(r'"marketplace_listing_seller_name"\s*:\s*"((?:\\.|[^"\\])*)"')
_PHOTO_RE = re.compile(r'"uri"\s*:\s*"(https://[^"]*scontent[^"]*)"')
_LOCATION_RE = re.compile(r'"city"\s*:\s*"([^"]+)"')


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


def _country_location_ok(location: str, country: str | None) -> bool:
    if not country:
        return True
    loc = (location or "").lower()
    if country == "ch":
        hints = (
            "switzerland", "schweiz", "suisse", "zürich", "zurich", "geneva", "genève",
            "bern", "basel", "lausanne", "lugano", "winterthur",
        )
    elif country == "fi":
        hints = (
            "finland", "suomi", "helsinki", "tampere", "turku", "oulu", "espoo", "vantaa",
        )
    else:
        return True
    if not loc:
        return True
    return any(h in loc for h in hints)


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


def build_category_url(url_path: str) -> str:
    """Как в браузере: https://www.facebook.com/marketplace/category/sports"""
    path = normalize_category_path(url_path)
    return urljoin(_FB_BASE, f"{path}/")


async def fetch_category_listings(
    token: AccountToken,
    *,
    url_path: str,
    category_label: str,
    user_agent: str,
    country: str | None,
    proxy_url: str | None,
    limit: int,
    timeout_sec: float = 45.0,
) -> list[MarketplaceListing]:
    """Собрать объявления из категории. Страна — фильтр по городу в объявлении."""
    url = build_category_url(url_path)
    logger.info("fetch category url=%s country=%s", url, country or "all")

    batch = await _fetch_page(
        token,
        url=url,
        user_agent=user_agent,
        proxy_url=proxy_url,
        timeout_sec=timeout_sec,
        category_label=category_label,
    )

    out: list[MarketplaceListing] = []
    for item in batch:
        if country and not _country_location_ok(item.location, country):
            continue
        out.append(item)
        if len(out) >= limit:
            break

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


async def _fetch_page(
    token: AccountToken,
    *,
    url: str,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float,
    category_label: str,
) -> list[MarketplaceListing]:
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

    return _parse_html(html, category_label)


def _parse_html(html: str, category_label: str) -> list[MarketplaceListing]:
    by_id: dict[str, MarketplaceListing] = {}
    titles = [_unescape(m.group(1)) for m in _TITLE_RE.finditer(html)]
    prices = [_unescape(m.group(1)) for m in _PRICE_RE.finditer(html)]
    sellers = [_unescape(m.group(1)) for m in _SELLER_RE.finditer(html)]
    photos = [_unescape(m.group(1)) for m in _PHOTO_RE.finditer(html)]
    locations = [_unescape(m.group(1)) for m in _LOCATION_RE.finditer(html)]

    ids_in_order: list[str] = []
    for m in _LISTING_ID_RE.finditer(html):
        lid = m.group(1)
        if lid not in ids_in_order:
            ids_in_order.append(lid)

    for i, lid in enumerate(ids_in_order):
        title = titles[i] if i < len(titles) else ""
        if not title:
            continue
        by_id[lid] = MarketplaceListing(
            listing_id=lid,
            title=title,
            price=prices[i] if i < len(prices) else "",
            link=f"https://www.facebook.com/marketplace/item/{lid}/",
            seller_name=sellers[i] if i < len(sellers) else "",
            photo=photos[i] if i < len(photos) else "",
            location=locations[i] if i < len(locations) else "",
            category=category_label,
        )
    return list(by_id.values())


def listings_to_json(items: list[MarketplaceListing]) -> str:
    return json.dumps([x.to_export_dict() for x in items], ensure_ascii=False, indent=2)
