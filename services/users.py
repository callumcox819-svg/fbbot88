from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import config
from models import ParseRun, User


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None = None) -> User:
    res = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = res.scalar_one_or_none()
    if user:
        if username and user.username != username:
            user.username = username
        user.last_active_at = datetime.utcnow()
        await session.commit()
        return user

    is_admin = telegram_id in config.admin_ids
    user = User(
        telegram_id=telegram_id,
        username=username,
        is_admin=is_admin,
        access_granted=is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def user_has_access(user: User) -> bool:
    if user.is_banned:
        return False
    return bool(user.is_admin or user.access_granted)


async def grant_access(session: AsyncSession, telegram_id: int) -> bool:
    user = await get_or_create_user(session, telegram_id)
    user.access_granted = True
    user.is_banned = False
    await session.commit()
    return True


async def revoke_access(session: AsyncSession, telegram_id: int) -> bool:
    res = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = res.scalar_one_or_none()
    if not user or user.is_admin:
        return False
    user.access_granted = False
    await session.commit()
    return True


async def admin_stats(session: AsyncSession) -> dict:
    total = await session.scalar(select(func.count()).select_from(User)) or 0
    with_access = await session.scalar(
        select(func.count()).select_from(User).where(User.access_granted.is_(True))
    ) or 0
    banned = await session.scalar(
        select(func.count()).select_from(User).where(User.is_banned.is_(True))
    ) or 0
    parses = await session.scalar(select(func.coalesce(func.sum(User.total_parses), 0))) or 0
    listings = await session.scalar(select(func.coalesce(func.sum(User.total_listings), 0))) or 0
    runs_today = await session.scalar(
        select(func.count()).select_from(ParseRun).where(ParseRun.started_at >= datetime.utcnow().replace(hour=0, minute=0, second=0))
    ) or 0
    return {
        "total_users": int(total),
        "with_access": int(with_access),
        "banned": int(banned),
        "total_parses": int(parses),
        "total_listings": int(listings),
        "runs_today": int(runs_today),
    }
