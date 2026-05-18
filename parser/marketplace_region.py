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
    if lat is None or lon is None:
        return {}
    return {
        "radiusKM": str(int(r)),
        "latitude": str(lat),
        "longitude": str(lon),
    }


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


def _fb_headers(token: AccountToken, user_agent: str, referer: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Cookie": cookies_header(token.cookies),
        "Accept": "text/html,application/xhtml+xml,application/json",
        "Accept-Language": "en-US,en;q=0.9",
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
        async with _session_for_proxy(proxy_url) as session:
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


async def apply_marketplace_region(
    token: AccountToken,
    country: str,
    *,
    user_agent: str,
    proxy_url: str | None,
    timeout_sec: float = 25.0,
) -> None:
    """
    Перед парсингом: открыть CH/FI marketplace + координаты, чтобы FB не отдавал ленту UA аккаунта.
    """
    if country not in COUNTRY_LOCATIONS:
        return

    cfg = COUNTRY_LOCATIONS[country]
    label = cfg.get("label", country)
    headers_base = _fb_headers(token, user_agent, "https://www.facebook.com/marketplace/")
    timeout = aiohttp.ClientTimeout(total=timeout_sec)

    urls: list[str] = []
    slugs = cfg.get("marketplace_slugs") or []
    hubs = cfg.get("region_hubs") or []
    if slugs:
        urls.append(_url_with_geo(urljoin(_FB, f"{slugs[0]}/"), country))
    if hubs:
        urls.append(_url_with_geo(urljoin(_FB, f"{hubs[0]}/"), country))

    logger.info("region switch start country=%s urls=%s proxy=%s", country, len(urls), bool(proxy_url))
    async with _session_for_proxy(proxy_url) as session:
        for url in urls:
            try:
                logger.info("region GET %s", url.replace(_FB, "")[:56])
                async with session.get(url, headers=headers_base, timeout=timeout) as resp:
                    html = await resp.text(errors="ignore")
                    if "login" in str(resp.url).lower() or _looks_like_login_wall(html):
                        raise AccountTokenDeadError("login during region switch")
                    logger.info("marketplace region GET %s", url.replace(_FB, "")[:56])
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
    )
    logger.info("marketplace region applied: %s", label)


def append_geo_to_marketplace_url(url: str, country: str) -> str:
    return _url_with_geo(url, country)
