"""Личный ЧС: у каждого user_id свой список в БД (ручное добавление / очистка)."""

from __future__ import annotations

import re

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import BlockedSeller

_PROFILE_RE = re.compile(r"/marketplace/profile/(\d+)")
_FB_ID_RE = re.compile(
    r"(?:[?&]id=|/profile\.php\?id=|/profile/|/user/)(\d{8,})"
)


def _profile_id(item) -> str:
    sid = (getattr(item, "seller_id", None) or "").strip()
    if sid.isdigit():
        return sid
    link = (getattr(item, "person_link", None) or "").strip()
    if link:
        m = _PROFILE_RE.search(link)
        if m:
            return m.group(1)
        m = _FB_ID_RE.search(link)
        if m:
            return m.group(1)
    return ""


def canonical_seller_key(item) -> str:
    pid = _profile_id(item)
    if pid:
        return f"id:{pid}"
    name = (getattr(item, "seller_name", None) or "").strip().lower()
    if len(name) >= 2:
        return f"name:{name}"
    return ""


def seller_keys_for_item(item) -> frozenset[str]:
    keys: set[str] = set()
    ck = canonical_seller_key(item)
    if ck:
        keys.add(ck)
    name = (getattr(item, "seller_name", None) or "").strip().lower()
    if len(name) >= 2:
        keys.add(f"name:{name}")
    return frozenset(keys)


def seller_key_from_item(item) -> str:
    return canonical_seller_key(item)


def listing_has_known_seller(item) -> bool:
    return bool(canonical_seller_key(item))


def primary_seller_key(item) -> str:
    pid = _profile_id(item)
    if pid:
        return f"id:{pid}"
    return canonical_seller_key(item)


def is_seller_blocked(item, blocked: set[str]) -> bool:
    if not blocked:
        return False
    return bool(seller_keys_for_item(item) & blocked)


def normalize_seller_identity(item) -> None:
    sid = (getattr(item, "seller_id", None) or "").strip()
    if sid.isdigit() and len(sid) >= 8:
        link = f"https://www.facebook.com/marketplace/profile/{sid}/"
        item.seller_id = sid
        if hasattr(item, "person_link"):
            item.person_link = link
        return
    pid = _profile_id(item)
    if not pid:
        return
    if hasattr(item, "seller_id"):
        item.seller_id = pid
    link = f"https://www.facebook.com/marketplace/profile/{pid}/"
    if hasattr(item, "person_link"):
        item.person_link = link


async def count_blocked_sellers(session: AsyncSession, user_id: int) -> int:
    res = await session.execute(
        select(func.count(BlockedSeller.id)).where(BlockedSeller.user_id == user_id)
    )
    return int(res.scalar() or 0)


async def clear_blocked_sellers(
    session: AsyncSession, user_id: int, *, country: str | None = None
) -> int:
    q = delete(BlockedSeller).where(BlockedSeller.user_id == user_id)
    if country:
        q = q.where(BlockedSeller.country == country)
    res = await session.execute(q)
    await session.commit()
    return res.rowcount or 0


async def load_blocked_seller_keys(
    session: AsyncSession, user_id: int, country: str | None = None
) -> set[str]:
    """Все ключи ЧС пользователя (личный список, не зависит от текущей страны парса)."""
    res = await session.execute(
        select(BlockedSeller.seller_key, BlockedSeller.seller_name).where(
            BlockedSeller.user_id == user_id,
        )
    )
    keys: set[str] = set()
    for sk, sn in res.fetchall():
        if sk:
            keys.add(sk)
        name = (sn or "").strip().lower()
        if len(name) >= 2:
            keys.add(f"name:{name}")
    return keys


async def add_blocked_seller(
    session: AsyncSession,
    user_id: int,
    country: str,
    item,
) -> bool:
    """Добавить продавца в ЧС (один ключ — одна строка, дубли не плодим)."""
    item_keys = seller_keys_for_item(item)
    if not item_keys or not country:
        return False
    name = (getattr(item, "seller_name", None) or "").strip() or None
    primary = canonical_seller_key(item) or next(iter(item_keys))
    existing = await session.execute(
        select(BlockedSeller.id).where(
            BlockedSeller.user_id == user_id,
            BlockedSeller.seller_key == primary,
        )
    )
    if existing.scalar_one_or_none():
        return False
    session.add(
        BlockedSeller(
            user_id=user_id,
            country=country,
            seller_key=primary,
            seller_name=name,
        )
    )
    await session.commit()
    return True
