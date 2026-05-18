"""Личный ЧС продавцов: не более одного объявления на продавца."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import BlockedSeller

_PROFILE_RE = re.compile(r"/marketplace/profile/(\d+)")


def seller_key_from_item(item) -> str:
    sid = (getattr(item, "seller_id", None) or "").strip()
    if sid:
        return f"id:{sid}"
    link = item.person_link or ""
    m = _PROFILE_RE.search(link)
    if m:
        return f"id:{m.group(1)}"
    name = (item.seller_name or "").strip().lower()
    if len(name) >= 2:
        return f"name:{name}"
    return ""


async def clear_blocked_sellers(session: AsyncSession, user_id: int) -> int:
    from sqlalchemy import delete

    res = await session.execute(
        delete(BlockedSeller).where(BlockedSeller.user_id == user_id)
    )
    await session.commit()
    return res.rowcount or 0


async def load_blocked_seller_keys(session: AsyncSession, user_id: int) -> set[str]:
    res = await session.execute(
        select(BlockedSeller.seller_key).where(BlockedSeller.user_id == user_id)
    )
    return {row[0] for row in res.fetchall() if row[0]}


async def remember_seller(session: AsyncSession, user_id: int, item) -> None:
    key = seller_key_from_item(item)
    if not key:
        return
    exists = await session.execute(
        select(BlockedSeller.id).where(
            BlockedSeller.user_id == user_id,
            BlockedSeller.seller_key == key,
        )
    )
    if exists.scalar_one_or_none():
        return
    session.add(
        BlockedSeller(
            user_id=user_id,
            seller_key=key,
            seller_name=(item.seller_name or "").strip() or None,
        )
    )
    await session.commit()
