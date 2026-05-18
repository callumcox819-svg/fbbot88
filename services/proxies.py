from __future__ import annotations

import random
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Proxy


def parse_proxy_line(raw: str) -> Optional[dict]:
    line = (raw or "").strip()
    if not line:
        return None
    if "@" in line:
        auth, hostport = line.rsplit("@", 1)
        user, _, pwd = auth.partition(":")
        host, _, port = hostport.rpartition(":")
        try:
            return {"host": host, "port": int(port), "username": user or None, "password": pwd or None}
        except ValueError:
            return None
    parts = line.split(":")
    if len(parts) >= 2:
        try:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] if len(parts) > 2 else None,
                "password": ":".join(parts[3:]) if len(parts) > 3 else None,
            }
        except ValueError:
            return None
    return None


def proxy_to_url(proxy: Proxy) -> str:
    auth = ""
    if proxy.username:
        auth = f"{proxy.username}:{proxy.password or ''}@"
    scheme = "socks5" if proxy.proxy_type.startswith("socks") else "http"
    return f"{scheme}://{auth}{proxy.host}:{proxy.port}"


async def list_proxies(session: AsyncSession, user_id: int) -> list[Proxy]:
    res = await session.execute(
        select(Proxy).where(Proxy.user_id == user_id, Proxy.is_active.is_(True)).order_by(Proxy.id)
    )
    return list(res.scalars().all())


async def add_proxies(session: AsyncSession, user_id: int, lines: list[str]) -> tuple[int, int]:
    added, failed = 0, 0
    for line in lines:
        parsed = parse_proxy_line(line)
        if not parsed:
            failed += 1
            continue
        session.add(
            Proxy(
                user_id=user_id,
                host=parsed["host"],
                port=parsed["port"],
                username=parsed.get("username"),
                password=parsed.get("password"),
            )
        )
        added += 1
    await session.commit()
    return added, failed


async def delete_proxy(session: AsyncSession, user_id: int, proxy_id: int) -> bool:
    res = await session.execute(
        select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == user_id)
    )
    row = res.scalar_one_or_none()
    if not row:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def pick_random_proxy_url(session: AsyncSession, user_id: int) -> str | None:
    rows = await list_proxies(session, user_id)
    if not rows:
        return None
    return proxy_to_url(random.choice(rows))
