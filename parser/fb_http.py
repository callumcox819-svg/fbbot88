"""Общая HTTP-сессия Facebook: CookieJar + прокси (как браузер VOID)."""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiohttp
from aiohttp_socks import ProxyConnector
from yarl import URL

_FB_ORIGIN = URL("https://www.facebook.com/")
_bound_session: contextvars.ContextVar[aiohttp.ClientSession | None] = contextvars.ContextVar(
    "fb_bound_session", default=None
)


@asynccontextmanager
async def fb_http_session(
    proxy_url: str | None,
    cookies: dict[str, str],
) -> AsyncIterator[aiohttp.ClientSession]:
    """
    Одна сессия на весь прогон: FB выставляет cookies при region GET,
    без них category/ GraphQL часто падают с 0/message=''.
    """
    jar = aiohttp.CookieJar(unsafe=True)
    connector = ProxyConnector.from_url(proxy_url) if proxy_url else None
    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=jar,
        headers={"Accept-Language": "de-CH,de;q=0.9,en;q=0.8"},
    ) as session:
        if cookies:
            jar.update_cookies(
                {k: str(v) for k, v in cookies.items() if k and v},
                response_url=_FB_ORIGIN,
            )
        tok = _bound_session.set(session)
        try:
            yield session
        finally:
            _bound_session.reset(tok)


class reuse_http_session:
    """Контекст: использовать переданную/глобальную сессию, не закрывать."""

    def __init__(self, session: aiohttp.ClientSession | None, proxy_url: str | None) -> None:
        self._session = session or _bound_session.get()
        self._proxy_url = proxy_url
        self._owned: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        connector = (
            ProxyConnector.from_url(self._proxy_url) if self._proxy_url else None
        )
        jar = aiohttp.CookieJar(unsafe=True)
        self._owned = aiohttp.ClientSession(connector=connector, cookie_jar=jar)
        return self._owned

    async def __aexit__(self, *args: object) -> None:
        if self._owned is not None:
            await self._owned.close()
            self._owned = None
