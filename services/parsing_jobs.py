"""Фоновый парсинг: старт / стоп на пользователя."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select

from config import config
from database import Session
from models import ParseRun, User
from parser.marketplace import fetch_category_listings, listings_to_json
from parser.account_token import parse_account_token
from services.categories import list_user_categories
from services.proxies import pick_random_proxy_url

logger = logging.getLogger(__name__)


@dataclass
class JobState:
    task: asyncio.Task
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


_jobs: dict[int, JobState] = {}


def is_parsing(telegram_id: int) -> bool:
    job = _jobs.get(telegram_id)
    return bool(job and not job.task.done())


def request_stop(telegram_id: int) -> bool:
    job = _jobs.get(telegram_id)
    if not job:
        return False
    job.stop_event.set()
    job.task.cancel()
    return True


async def start_parsing(
    bot: Bot,
    *,
    telegram_id: int,
    user_id: int,
    token_raw: str,
    on_status: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    if is_parsing(telegram_id):
        raise RuntimeError("Парсинг уже запущен")

    async def _run() -> None:
        stop = _jobs[telegram_id].stop_event
        try:
            await _parse_impl(bot, telegram_id=telegram_id, user_id=user_id, token_raw=token_raw, stop=stop, on_status=on_status)
        except asyncio.CancelledError:
            if on_status:
                await on_status("⏹ Парсинг остановлен.")
        except Exception as e:
            logger.exception("parse failed tg=%s", telegram_id)
            if on_status:
                await on_status(f"❌ Ошибка: {e}")
        finally:
            _jobs.pop(telegram_id, None)

    task = asyncio.create_task(_run())
    _jobs[telegram_id] = JobState(task=task)


async def _parse_impl(
    bot: Bot,
    *,
    telegram_id: int,
    user_id: int,
    token_raw: str,
    stop: asyncio.Event,
    on_status: Callable[[str], Awaitable[None]] | None,
) -> None:
    token = parse_account_token(token_raw)

    async with Session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        categories = await list_user_categories(session, user_id)
        json_limit = max(1, min(int(user.json_limit or 50), 500))
        country = user.country

        if not categories:
            raise RuntimeError("Выбери категории в ⚙️ Настройки")

        run = ParseRun(user_id=user_id, status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    collected: list = []
    seen_ids: set[str] = set()

    async def status(msg: str) -> None:
        if on_status:
            await on_status(msg)

    await status(f"▶️ Старт. Цель: <b>{json_limit}</b> объявлений, категорий: <b>{len(categories)}</b>")

    for cat in categories:
        if stop.is_set():
            break
        if len(collected) >= json_limit:
            break

        need = json_limit - len(collected)
        await status(f"📂 {cat.label}… (собрано {len(collected)}/{json_limit})")

        async with Session() as session:
            proxy_url = await pick_random_proxy_url(session, user_id)

        try:
            batch = await fetch_category_listings(
                token,
                url_path=cat.url_path,
                category_label=cat.label,
                user_agent=config.fb_user_agent,
                country=country,
                proxy_url=proxy_url,
                limit=need + 10,
            )
        except Exception as e:
            await status(f"⚠️ {cat.label}: {e}")
            continue

        for item in batch:
            if stop.is_set() or len(collected) >= json_limit:
                break
            if item.listing_id in seen_ids:
                continue
            seen_ids.add(item.listing_id)
            collected.append(item)

    status_key = "stopped" if stop.is_set() else ("done" if collected else "error")

    async with Session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        user.last_account_token = token_raw
        user.total_parses = (user.total_parses or 0) + 1
        user.total_listings = (user.total_listings or 0) + len(collected)

        run = (await session.execute(select(ParseRun).where(ParseRun.id == run_id))).scalar_one()
        run.status = status_key
        run.listings_count = len(collected)
        run.categories_used = len(categories)
        run.finished_at = datetime.utcnow()
        if not collected:
            run.error_message = "Ничего не собрано"
        await session.commit()

    if not collected:
        await bot.send_message(telegram_id, "❌ Объявления не найдены. Проверь токен аккаунта, прокси и категории.")
        return

    payload = listings_to_json(collected)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        f.write(payload)
        path = f.name

    try:
        await bot.send_document(
            telegram_id,
            FSInputFile(path, filename=f"marketplace_{len(collected)}.json"),
            caption=f"✅ Собрано: {len(collected)} объявлений",
        )
    finally:
        Path(path).unlink(missing_ok=True)

    await status(f"✅ Готово: <b>{len(collected)}</b> объявлений в JSON")
