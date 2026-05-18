"""Фоновый парсинг: только JSON в конце, когда набран полный лимит."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select

from config import config
from database import Session
from models import ParseRun, User
from parser.account_token import parse_account_token
from parser.marketplace import fetch_category_listings, is_connection_error, listings_to_json
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
            await _parse_impl(
                bot,
                telegram_id=telegram_id,
                user_id=user_id,
                token_raw=token_raw,
                stop=stop,
                on_status=on_status,
            )
        except asyncio.CancelledError:
            if on_status:
                await on_status("⏹ Остановлено.")
        except Exception as e:
            logger.exception("parse failed tg=%s", telegram_id)
            if on_status:
                await on_status(f"❌ Ошибка: {e}")
        finally:
            _jobs.pop(telegram_id, None)

    task = asyncio.create_task(_run())
    _jobs[telegram_id] = JobState(task=task)


def _progress_text(done: int, total: int, *, step: str = "") -> str:
    lines = [
        f"🔎 <b>Сбор: {done}/{total}</b>",
        "<i>Бот работает — цифра растёт. JSON пришлёт файлом в конце.</i>",
    ]
    if step:
        lines.append(f"📍 {step}")
    return "\n".join(lines)


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
        user.last_account_token = token_raw
        await session.commit()

        if not categories:
            raise RuntimeError("Выбери категории в ⚙️ Настройки")

        run = ParseRun(user_id=user_id, status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    collected: list = []
    seen_ids: set[str] = set()
    cat_count = len(categories)
    cat_idx = 0
    empty_rounds = 0
    max_empty_rounds = cat_count * 5
    connect_fails = 0

    t_start = time.monotonic()
    current_step = {"text": "Старт…"}

    async def status_progress() -> None:
        if on_status:
            await on_status(_progress_text(len(collected), json_limit, step=current_step["text"]))

    async def heartbeat() -> None:
        while not stop.is_set():
            await asyncio.sleep(12)
            if len(collected) >= json_limit:
                return
            sec = int(time.monotonic() - t_start)
            current_step["text"] = f"⏳ {sec} сек — {current_step.get('detail', 'ожидание…')}"
            await status_progress()

    hb_task = asyncio.create_task(heartbeat())

    country_label = ""
    if country == "ch":
        country_label = " 🇨🇭"
    elif country == "fi":
        country_label = " 🇫🇮"
    current_step["text"] = f"Категорий: {cat_count}{country_label}"
    await status_progress()
    logger.info("parse start tg=%s limit=%s cats=%s country=%s", telegram_id, json_limit, cat_count, country)

    try:
        while len(collected) < json_limit and not stop.is_set():
            cat = categories[cat_idx % cat_count]
            cat_idx += 1
            need = json_limit - len(collected)
            current_step["detail"] = cat.label
            current_step["text"] = f"Категория: {cat.label}"
            await status_progress()

            async with Session() as session:
                proxy_url = await pick_random_proxy_url(session, user_id)

            async def on_url(i: int, n: int, short: str) -> None:
                current_step["detail"] = f"{cat.label} ({i}/{n}) {short}"
                await status_progress()

            batch = None
            last_err: Exception | None = None
            for proxy_try in (proxy_url, None):
                if proxy_try is None and proxy_url is None:
                    break
                try:
                    batch = await fetch_category_listings(
                        token,
                        url_path=cat.url_path,
                        category_label=cat.label,
                        user_agent=config.fb_user_agent,
                        country=country,
                        proxy_url=proxy_try,
                        limit=min(max(need * 2, 30), 120),
                        timeout_sec=22.0,
                        on_url_progress=on_url,
                    )
                    connect_fails = 0
                    break
                except Exception as e:
                    last_err = e
                    if proxy_try and is_connection_error(e):
                        logger.warning("proxy failed for %s, retry direct: %s", cat.label, e)
                        continue
                    break

            if batch is None:
                err = last_err or RuntimeError("unknown")
                logger.warning("category %s failed: %s", cat.label, err)
                if is_connection_error(err):
                    connect_fails += 1
                    if connect_fails >= 3 and len(collected) == 0:
                        hint = (
                            "❌ <b>Нет связи с Facebook</b>\n\n"
                            "С сервера Railway без рабочего прокси FB часто недоступен.\n"
                            "⚙️ Настройки → 🌐 Прокси — добавь SOCKS5 (CH/FI).\n"
                            "Проверь host:port:user:pass."
                        )
                        if on_status:
                            await on_status(hint)
                        raise RuntimeError("Нет связи с Facebook") from err
                empty_rounds += 1
                if empty_rounds >= max_empty_rounds:
                    break
                continue

            added = 0
            for item in batch:
                if stop.is_set() or len(collected) >= json_limit:
                    break
                if item.listing_id in seen_ids:
                    continue
                seen_ids.add(item.listing_id)
                collected.append(item)
                added += 1

            if added == 0:
                empty_rounds += 1
                current_step["detail"] = f"{cat.label}: 0 объявлений с этой категории"
                if empty_rounds >= max_empty_rounds:
                    break
            else:
                empty_rounds = 0
                current_step["detail"] = f"+{added} из {cat.label}"

            await status_progress()
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

    got = len(collected)
    full = got >= json_limit
    stopped = stop.is_set()

    if stopped:
        status_key = "stopped"
    elif full:
        status_key = "done"
    else:
        status_key = "error"

    async with Session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        user.total_parses = (user.total_parses or 0) + 1
        user.total_listings = (user.total_listings or 0) + got

        run = (await session.execute(select(ParseRun).where(ParseRun.id == run_id))).scalar_one()
        run.status = status_key
        run.listings_count = got
        run.categories_used = cat_count
        run.finished_at = datetime.utcnow()
        if not full:
            run.error_message = f"Собрано {got}/{json_limit}"
        await session.commit()

    if not full:
        reason = "остановлен" if stopped else "объявления не найдены"
        if on_status:
            await on_status(
                f"⚠️ Парсинг {reason}.\n"
                f"Собрано <b>{got}/{json_limit}</b> — JSON <b>не отправлен</b>.\n\n"
                "Чаще всего:\n"
                "• токен аккаунта протух — вставь новый\n"
                "• Facebook отдаёт пустую страницу — проверь прокси CH/FI\n"
                "• в логах Railway: <code>parsed 0 items</code> и <code>links=0</code>"
            )
        return

    payload = listings_to_json(collected[:json_limit])
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        f.write(payload)
        path = f.name

    try:
        await bot.send_document(
            telegram_id,
            FSInputFile(path, filename=f"marketplace_{json_limit}.json"),
            caption=f"✅ JSON: {json_limit} объявлений",
        )
    finally:
        Path(path).unlink(missing_ok=True)

    if on_status:
        await on_status(f"✅ Готово. Отправлен JSON: <b>{json_limit}</b> объявлений.")
