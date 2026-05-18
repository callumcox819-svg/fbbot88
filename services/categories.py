from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from data.preset_categories import MAX_CATEGORIES_PER_USER, PRESET_BY_KEY
from models import UserCategory


async def get_active_preset_keys(session: AsyncSession, user_id: int) -> set[str]:
    cats = await list_user_categories(session, user_id)
    return {c.category_key for c in cats if c.is_preset}


async def toggle_preset_category(
    session: AsyncSession, user_id: int, key: str
) -> tuple[set[str], str | None]:
    """Вкл/выкл готовую категорию (сохранение сразу)."""
    if key not in PRESET_BY_KEY:
        return await get_active_preset_keys(session, user_id), "Неизвестная категория"

    all_cats = await list_user_categories(session, user_id)
    custom_count = sum(1 for c in all_cats if not c.is_preset)
    current = await get_active_preset_keys(session, user_id)

    if key in current:
        current.discard(key)
    else:
        if custom_count + len(current) >= MAX_CATEGORIES_PER_USER:
            return current, (
                f"Максимум {MAX_CATEGORIES_PER_USER} категорий всего. "
                "Выключи другую 🟢 или убери свою ссылку."
            )
        current.add(key)

    err = await set_preset_categories(session, user_id, list(current))
    return current, err


async def count_active_categories(session: AsyncSession, user_id: int) -> int:
    cats = await list_user_categories(session, user_id)
    return len(cats)


async def list_user_categories(session: AsyncSession, user_id: int) -> list[UserCategory]:
    res = await session.execute(
        select(UserCategory).where(UserCategory.user_id == user_id).order_by(UserCategory.id)
    )
    return list(res.scalars().all())


async def set_preset_categories(session: AsyncSession, user_id: int, keys: list[str]) -> str | None:
    all_cats = await list_user_categories(session, user_id)
    custom_count = sum(1 for c in all_cats if not c.is_preset)
    if custom_count + len(keys) > MAX_CATEGORIES_PER_USER:
        return f"Максимум {MAX_CATEGORIES_PER_USER} категорий (с учётом своих ссылок)"

    await session.execute(
        delete(UserCategory).where(
            UserCategory.user_id == user_id,
            UserCategory.is_preset.is_(True),
        )
    )
    for key in keys:
        preset = PRESET_BY_KEY.get(key)
        if not preset:
            continue
        session.add(
            UserCategory(
                user_id=user_id,
                category_key=preset.key,
                label=preset.label,
                url_path=preset.url_path,
                is_preset=True,
            )
        )
    await session.commit()
    return None


async def add_custom_category(session: AsyncSession, user_id: int, url: str) -> str | None:
    cats = await list_user_categories(session, user_id)
    if len(cats) >= MAX_CATEGORIES_PER_USER:
        return f"Максимум {MAX_CATEGORIES_PER_USER} категорий"

    from parser.marketplace import normalize_category_path

    try:
        path = normalize_category_path(url)
    except ValueError:
        return "Некорректная ссылка"

    key = f"custom:{path[:60]}"
    session.add(
        UserCategory(
            user_id=user_id,
            category_key=key,
            label=path[:40],
            url_path=path,
            is_preset=False,
        )
    )
    await session.commit()
    return None
