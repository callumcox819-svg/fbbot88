"""Переключение региона Facebook Marketplace (CH/FI) — как в VOID, не лента аккаунта UA."""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlencode, urljoin

import aiohttp

from aiohttp_socks import ProxyConnector

from data.preset_categories import COUNTRY_LOCATIONS
from parser.fb_http import reuse_http_session
from parser.account_token import AccountToken, AccountTokenDeadError, cookies_header

logger = logging.getLogger(__name__)

_FB = "https://www.facebook.com/marketplace/"
def _session_for_proxy(proxy_url: str | None) -> aiohttp.ClientSession:
    if not proxy_url:
        return aiohttp.ClientSession()
    return aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy_url))


def _looks_like_login_wall(html: str) -> bool:
    h = html.lower()
    if "id=\"loginform\"" in h or 'id="loginform"' in h:
        return True
    if "marketplace/item/" not in h and ("login" in h[:8000] or "log in" in h[:8000]):
        return True
    return False


def _geo_params(country: str) -> dict[str, str]:
    cfg = COUNTRY_LOCATIONS.get(country) or {}
    lat = cfg.get("latitude")
    lon = cfg.get("longitude")
    r = cfg.get("radius_km", 65)
    params: dict[str, str] = {}
    if country == "fi":
        params["locale"] = "fi_FI"
    elif country == "ch":
        params["locale"] = "de_CH"
    if lat is None or lon is None:
        return params
    params.update(
        {
            "radiusKM": str(int(r)),
            "latitude": str(lat),
            "longitude": str(lon),
        }
    )
    return params


def _url_with_geo(base: str, country: str) -> str:
    params = _geo_params(country)
    if not params:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


def _filter_location_id(country: str) -> str | None:
    env_key = f"FB_MARKETPLACE_LOCATION_ID_{country.upper()}"
    from_env = (os.getenv(env_key) or "").strip()
    if from_env:
        return from_env
    cfg = COUNTRY_LOCATIONS.get(country) or {}
    lid = cfg.get("filter_location_id")
    return str(lid) if lid else None


def _accept_language(country: str) -> str:
    if country == "fi":
        return "fi-FI,fi;q=0.9,en;q=0.8"
    if country == "ch":
        return "de-CH,fr-CH,de;q=0.9,fr;q=0.8,en;q=0.7"
    return "en-US,en;q=0.9"


def _fb_headers(
    token: AccountToken, user_agent: str, referer: str, *, country: str = ""
) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Accept": "text/html,application/xhtml+xml,application/json",
        "Accept-Language": _accept_language(country),
        "Referer": referer,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }


async def _graphql_browse_prime(
    token: AccountToken,
    *,
    country: str,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float,
    http_session: aiohttp.ClientSession | None = None,
) -> bool:
    """Задать filter_location_id через marketplace_search (как VOID / wesbos gist)."""
    loc_id = _filter_location_id(country)
    if not loc_id or not token.access_token:
        return False

    doc_id = (os.getenv("FB_MARKETPLACE_BROWSE_DOC_ID") or "2022753507811174").strip()
    variables: dict[str, Any] = {
        "params": {
            "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": ""},
            "browse_request_params": {
                "filter_location_id": loc_id,
                "filter_price_lower_bound": 0,
                "filter_price_upper_bound": 214748364700,
            },
            "custom_request_params": {
                "surface": "BROWSE",
                "search_vertical": "C2C",
            },
        },
    }
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Origin": "https://www.facebook.com",
        "Referer": _url_with_geo(urljoin(_FB, COUNTRY_LOCATIONS[country]["marketplace_slugs"][0] + "/"), country),
    }
    body = {
        "doc_id": doc_id,
        "variables": json.dumps(variables),
        "access_token": token.access_token,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with reuse_http_session(http_session, proxy_url) as session:
            async with session.post(
                "https://www.facebook.com/api/graphql/",
                headers=headers,
                data=body,
                timeout=timeout,
            ) as resp:
                raw = await resp.text(errors="ignore")
                if resp.status >= 400:
                    return False
                if "login" in raw.lower()[:500]:
                    return False
                logger.info("marketplace GraphQL location primed country=%s loc_id=%s", country, loc_id[:8])
                return True
    except Exception as e:
        logger.debug("graphql browse prime failed: %s", e)
    return False


async def fetch_browse_marketplace_listings(
    token: AccountToken,
    country: str,
    *,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float = 20.0,
    http_session: aiohttp.ClientSession | None = None,
    category_label: str = "Browse",
    limit: int = 80,
) -> tuple[list, str]:
    """Лента browse/search GraphQL (filter_location_id) — когда category HTML не открывается."""
    loc_id = _filter_location_id(country)
    if not loc_id or not token.access_token or country not in COUNTRY_LOCATIONS:
        return [], ""

    doc_id = (os.getenv("FB_MARKETPLACE_BROWSE_DOC_ID") or "2022753507811174").strip()
    variables: dict[str, Any] = {
        "params": {
            "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": ""},
            "browse_request_params": {
                "filter_location_id": loc_id,
                "filter_price_lower_bound": 0,
                "filter_price_upper_bound": 214748364700,
            },
            "custom_request_params": {
                "surface": "BROWSE",
                "search_vertical": "C2C",
            },
        },
    }
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Origin": "https://www.facebook.com",
        "Referer": _url_with_geo(
            urljoin(_FB, COUNTRY_LOCATIONS[country]["marketplace_slugs"][0] + "/"),
            country,
        ),
    }
    body = {
        "doc_id": doc_id,
        "variables": json.dumps(variables),
        "access_token": token.access_token,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with reuse_http_session(http_session, proxy_url) as session:
            async with session.post(
                "https://www.facebook.com/api/graphql/",
                headers=headers,
                data=body,
                timeout=timeout,
            ) as resp:
                raw = await resp.text(errors="ignore")
                if resp.status >= 400 or "login" in raw.lower()[:400]:
                    return []
                data = json.loads(raw)
    except Exception as e:
        logger.info("browse feed failed country=%s: %s", country, e)
        return [], ""

    from parser.marketplace import MarketplaceListing, _walk_marketplace_json, listing_is_valid

    from parser.marketplace import patch_listings_sellers_from_raw

    by_id: dict[str, MarketplaceListing] = {}
    _walk_marketplace_json(data, by_id)
    out: list = []
    for lid, item in by_id.items():
        if not listing_is_valid(item):
            continue
        item.category = category_label
        if not item.link:
            item.link = f"https://www.facebook.com/marketplace/item/{lid}/"
        out.append(item)
        if len(out) >= limit:
            break
    n_prof = patch_listings_sellers_from_raw(raw, out)
    if out:
        logger.info(
            "browse feed country=%s: %s items sellers=%s (doc=%s)",
            country,
            len(out),
            n_prof,
            doc_id[:12],
        )
    return out, raw


def _browse_variables(country: str) -> dict[str, Any] | None:
    loc_id = _filter_location_id(country)
    if not loc_id:
        return None
    return {
        "params": {
            "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": ""},
            "browse_request_params": {
                "filter_location_id": loc_id,
                "filter_price_lower_bound": 0,
                "filter_price_upper_bound": 214748364700,
            },
            "custom_request_params": {
                "surface": "BROWSE",
                "search_vertical": "C2C",
            },
        },
    }


async def fetch_browse_marketplace_comet(
    token: AccountToken,
    country: str,
    *,
    hint_html: str,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float = 22.0,
    http_session: aiohttp.ClientSession | None = None,
    category_label: str = "Browse",
    limit: int = 120,
) -> tuple[list, str]:
    """Browse-лента через fb_dtsg (как браузер VOID), без access_token."""
    from parser.marketplace import (
        MarketplaceListing,
        _extract_fb_tokens,
        _html_for_discovery,
        _walk_marketplace_json,
        listing_is_valid,
    )

    variables = _browse_variables(country)
    if not variables or country not in COUNTRY_LOCATIONS:
        return [], ""
    lsd, dtsg = _extract_fb_tokens(_html_for_discovery(hint_html))
    if not dtsg or not token.c_user:
        logger.info("browse comet skip: dtsg=%s c_user=%s", bool(dtsg), bool(token.c_user))
        return [], ""

    doc_id = (os.getenv("FB_MARKETPLACE_BROWSE_DOC_ID") or "2022753507811174").strip()
    referer = urljoin(_FB, COUNTRY_LOCATIONS[country]["marketplace_slugs"][0] + "/")
    body = urlencode(
        {
            "__a": "1",
            "__user": token.c_user,
            "fb_dtsg": dtsg,
            "lsd": lsd or dtsg[:12],
            "doc_id": doc_id,
            "variables": json.dumps(variables),
            "server_timestamps": "1",
        }
    )
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Origin": "https://www.facebook.com",
        "Referer": referer,
        "X-FB-LSD": lsd or "",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with reuse_http_session(http_session, proxy_url) as session:
            async with session.post(
                "https://www.facebook.com/api/graphql/",
                headers=headers,
                data=body,
                timeout=timeout,
            ) as resp:
                raw = await resp.text(errors="ignore")
                if resp.status >= 400:
                    logger.info("browse comet HTTP %s", resp.status)
                    return [], ""
                if "login" in raw.lower()[:500]:
                    return [], ""
                data = json.loads(raw)
    except Exception as e:
        logger.info("browse comet failed: %s", e)
        return [], ""

    from parser.marketplace import patch_listings_sellers_from_raw

    by_id: dict[str, MarketplaceListing] = {}
    _walk_marketplace_json(data, by_id)
    out: list = []
    for lid, item in by_id.items():
        if not listing_is_valid(item):
            continue
        item.category = category_label
        out.append(item)
        if len(out) >= limit:
            break
    n_prof = patch_listings_sellers_from_raw(raw, out)
    if out:
        logger.info(
            "browse comet country=%s: %s items, sellers=%s",
            country,
            len(out),
            n_prof,
        )
    else:
        logger.info("browse comet country=%s: 0 items (keys in json=%s)", country, len(by_id))
    return out, raw


async def fetch_any_browse_feed(
    token: AccountToken,
    country: str,
    *,
    hint_html: str,
    user_agent: str,
    proxy_url: str | None,
    http_session: aiohttp.ClientSession | None = None,
    limit: int = 120,
) -> tuple[list, str]:
    if hint_html:
        items, raw = await fetch_browse_marketplace_comet(
            token,
            country,
            hint_html=hint_html,
            user_agent=user_agent,
            proxy_url=proxy_url,
            http_session=http_session,
            limit=limit,
        )
        if items:
            return items, raw
    return await fetch_browse_marketplace_listings(
        token,
        country,
        user_agent=user_agent,
        proxy_url=proxy_url,
        http_session=http_session,
        limit=limit,
    )


async def _read_response_body(resp: aiohttp.ClientResponse, max_bytes: int = 8_000_000) -> str:
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks).decode("utf-8", errors="ignore")


async def apply_marketplace_region(
    token: AccountToken,
    country: str,
    *,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float = 25.0,
    http_session: aiohttp.ClientSession | None = None,
) -> tuple[list[str], str]:
    """
    Перед парсингом: открыть CH/FI marketplace + координаты, чтобы FB не отдавал ленту UA аккаунта.
    Возвращает (doc_id кандидаты из hub HTML, самый полный hub HTML для GraphQL как в браузере).
    """
    if country not in COUNTRY_LOCATIONS:
        return [], ""

    cfg = COUNTRY_LOCATIONS[country]
    label = cfg.get("label", country)
    headers_base = _fb_headers(
        token, user_agent, "https://www.facebook.com/marketplace/", country=country
    )
    timeout = aiohttp.ClientTimeout(
        total=timeout_sec,
        connect=min(20.0, timeout_sec),
        sock_read=max(15.0, timeout_sec - 5.0),
    )
    doc_ids: list[str] = []
    hub_html = ""

    urls: list[str] = []
    slugs = cfg.get("marketplace_slugs") or []
    hubs = cfg.get("region_hubs") or []
    loc_id = str(cfg.get("filter_location_id") or "")
    seen: set[str] = set()

    def add(path: str) -> None:
        u = _url_with_geo(urljoin(_FB, path if path.endswith("/") else f"{path}/"), country)
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for slug in slugs:
        add(f"{slug}/")
    if loc_id and loc_id not in slugs:
        add(f"{loc_id}/")
    for hub in hubs[:2]:
        add(f"{hub}/")

    logger.info("region switch start country=%s urls=%s proxy=%s", country, len(urls), bool(proxy_url))
    async with reuse_http_session(http_session, proxy_url) as session:
        for url in urls:
            try:
                logger.info("region GET %s", url.replace(_FB, "")[:56])
                async with session.get(url, headers=headers_base, timeout=timeout) as resp:
                    html = await _read_response_body(resp)
                    if "login" in str(resp.url).lower() or _looks_like_login_wall(html):
                        raise AccountTokenDeadError("login during region switch")
                    logger.info("marketplace region GET %s", url.replace(_FB, "")[:56])
                    if len(html) > len(hub_html):
                        hub_html = html
                    from parser.marketplace import (
                        _discover_feed_doc_ids_from_html,
                        _hub_has_listings,
                    )

                    for did in _discover_feed_doc_ids_from_html(html):
                        if did not in doc_ids:
                            doc_ids.append(did)
            except AccountTokenDeadError:
                raise
            except Exception as e:
                logger.debug("region GET %s: %s", url, e)

    await _graphql_browse_prime(
        token,
        country=country,
        user_agent=user_agent,
        proxy_url=proxy_url,
        timeout_sec=min(timeout_sec, 12.0),
        http_session=http_session,
    )
    logger.info(
        "marketplace region applied: %s (hub_html=%s, feed_doc_ids=%s)",
        label,
        len(hub_html),
        len(doc_ids),
    )
    return doc_ids, hub_html


def _category_prime_paths(country: str, url_path: str) -> list[str]:
    """VOID: лента через filter_location_id, не только finland/ в path."""
    cfg = COUNTRY_LOCATIONS.get(country) or {}
    norm = url_path.strip("/").replace("\\", "/")
    parts = norm.split("/")
    cat_slug = ""
    if "category" in parts:
        idx = parts.index("category")
        if idx + 1 < len(parts):
            cat_slug = parts[idx + 1]
    loc_id = str(cfg.get("filter_location_id") or "")
    paths: list[str] = []
    if cat_slug and loc_id:
        paths.append(f"{loc_id}/category/{cat_slug}/")
    paths.append(f"{norm}/" if not norm.endswith("/") else norm)
    return paths


async def prime_category_feed_region(
    token: AccountToken,
    country: str,
    url_path: str,
    *,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float = 18.0,
    http_session: aiohttp.ClientSession | None = None,
) -> None:
    """Открыть ленту категории с geo до парсинга (loc_id + finland)."""
    if country not in COUNTRY_LOCATIONS:
        return
    headers = _fb_headers(
        token, user_agent, "https://www.facebook.com/marketplace/", country=country
    )
    timeout = aiohttp.ClientTimeout(
        total=timeout_sec,
        connect=min(18.0, timeout_sec),
        sock_read=max(12.0, timeout_sec - 4.0),
    )
    async with reuse_http_session(http_session, proxy_url) as session:
        for path in _category_prime_paths(country, url_path)[:2]:
            url = urljoin(_FB, path if path.endswith("/") else f"{path}/")
            try:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    await _read_response_body(resp, max_bytes=4_000_000)
                    logger.info("category prime GET %s", path[:56])
            except Exception as e:
                logger.warning("category prime %s: %s", path[:40], e)


def append_geo_to_marketplace_url(url: str, country: str) -> str:
    return _url_with_geo(url, country)
