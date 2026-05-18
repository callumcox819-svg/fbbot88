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
from parser.account_token import (
    AccountTokenDeadError,
    TOKEN_DEAD_USER_MESSAGE,
    is_account_token_dead,
    parse_account_token,
)
from parser.marketplace import (
    enrich_listing,
    export_reject_reason,
    fetch_category_listings,
    is_connection_error,
    listing_is_export_ready,
    listing_is_valid,
    listings_to_json,
)
from parser.marketplace_region import apply_marketplace_region
from services.categories import list_user_categories
from services.proxies import pick_random_proxy_url

logger = logging.getLogger(__name__)


@dataclass
class JobState:
    task: asyncio.Task
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    stats: dict = field(default_factory=dict)
    push_status: Callable[[], Awaitable[None]] | None = None


_jobs: dict[int, JobState] = {}


def get_parse_stats(telegram_id: int) -> dict | None:
    job = _jobs.get(telegram_id)
    if not job or not job.stats:
        return None
    return dict(job.stats)


async def _notify_token_dead(
    bot: Bot,
    telegram_id: int,
    on_status: Callable[[str], Awaitable[None]] | None,
) -> None:
    logger.warning("account token dead tg=%s", telegram_id)
    if on_status:
        await on_status(TOKEN_DEAD_USER_MESSAGE)
    try:
        await bot.send_message(telegram_id, TOKEN_DEAD_USER_MESSAGE)
    except Exception:
        logger.exception("failed to send token-dead message tg=%s", telegram_id)


async def refresh_parse_status(telegram_id: int) -> bool:
    job = _jobs.get(telegram_id)
    if job and job.push_status:
        await job.push_status()
        return True
    return False


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
        except AccountTokenDeadError:
            await _notify_token_dead(bot, telegram_id, on_status)
        except Exception as e:
            if is_account_token_dead(e):
                await _notify_token_dead(bot, telegram_id, on_status)
            else:
                logger.exception("parse failed tg=%s", telegram_id)
                if on_status:
                    await on_status(f"❌ Ошибка: {e}")
        finally:
            _jobs.pop(telegram_id, None)

    task = asyncio.create_task(_run())
    _jobs[telegram_id] = JobState(
        task=task,
        stats={
            "pages": 0,
            "found": 0,
            "checked": 0,
            "rejected": 0,
            "accepted": 0,
        },
    )


def _progress_text(
    done: int,
    total: int,
    *,
    step: str = "",
    stats: dict | None = None,
) -> str:
    lines = [
        f"🔎 <b>В JSON: {done}/{total}</b>",
        "<i>«В JSON» — карточки для выбранной страны (CH/FI). JSON в конце.</i>",
    ]
    if stats:
        lines.append(
            f"📊 Страниц: <b>{stats.get('pages', 0)}</b> · "
            f"новых: <b>{stats.get('found', 0)}</b> · "
            f"проверено: <b>{stats.get('checked', 0)}</b> · "
            f"отклонено: <b>{stats.get('rejected', 0)}</b>"
        )
        last_reject = stats.get("last_reject")
        if last_reject and stats.get("rejected", 0) > 0:
            lines.append(f"<i>Последний отсев: {last_reject}</i>")
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
        if not country:
            raise RuntimeError(
                "Выбери страну в ⚙️ Настройки → 🇨🇭 Швейцария или 🇫🇮 Финляндия"
            )

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
    job = _jobs.get(telegram_id)
    stats = job.stats if job else {}
    token_dead_flag = {"value": False}

    async def status_progress() -> None:
        stats["accepted"] = len(collected)
        if on_status:
            await on_status(
                _progress_text(
                    len(collected),
                    json_limit,
                    step=current_step["text"],
                    stats=stats,
                )
            )

    if job:
        job.push_status = status_progress

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

    async with Session() as session:
        proxy_url_boot = await pick_random_proxy_url(session, user_id)
    try:
        current_step["text"] = f"Переключаю Marketplace на {country_label or country}…"
        await status_progress()
        await apply_marketplace_region(
            token,
            country,
            user_agent=config.fb_user_agent,
            proxy_url=proxy_url_boot,
        )
    except AccountTokenDeadError:
        token_dead_flag["value"] = True
        await _notify_token_dead(bot, telegram_id, on_status)
        return

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
                current_step["text"] = f"⏳ {current_step['detail']}"
                await status_progress()

            async def on_page_found(n: int) -> None:
                stats["pages"] = stats.get("pages", 0) + 1
                stats["found"] = stats.get("found", 0) + n
                current_step["text"] = (
                    f"⏳ {current_step.get('detail', cat.label)} · +{n} новых"
                )
                await status_progress()

            cat_added = 0

            async def on_page_items(page_items: list) -> None:
                nonlocal cat_added, empty_rounds
                for item in page_items:
                    if stop.is_set() or len(collected) >= json_limit:
                        return
                    if item.listing_id in seen_ids:
                        continue
                    stats["checked"] = stats.get("checked", 0) + 1
                    reason = export_reject_reason(item, country)
                    if reason:
                        try:
                            await enrich_listing(
                                token,
                                item,
                                user_agent=config.fb_user_agent,
                                proxy_url=proxy_used,
                                timeout_sec=14.0,
                            )
                        except AccountTokenDeadError:
                            token_dead_flag["value"] = True
                            raise
                        reason = export_reject_reason(item, country)
                    if reason:
                        stats["rejected"] = stats.get("rejected", 0) + 1
                        stats["last_reject"] = reason
                        await status_progress()
                        continue
                    seen_ids.add(item.listing_id)
                    collected.append(item)
                    cat_added += 1
                    empty_rounds = 0
                    current_step["detail"] = (
                        f"+{cat_added} {cat.label} · в JSON {len(collected)}/{json_limit}"
                    )
                    await status_progress()

            def should_stop() -> bool:
                return stop.is_set() or len(collected) >= json_limit

            batch = None
            last_err: Exception | None = None
            proxy_used: str | None = None
            for proxy_try in (proxy_url, None):
                if proxy_try is None and proxy_url is None:
                    break
                try:
                    proxy_used = proxy_try
                    batch = await fetch_category_listings(
                        token,
                        url_path=cat.url_path,
                        category_label=cat.label,
                        user_agent=config.fb_user_agent,
                        country=country,
                        proxy_url=proxy_try,
                        limit=json_limit * 3,
                        timeout_sec=22.0,
                        on_url_progress=on_url,
                        on_page_found=on_page_found,
                        on_page_items=on_page_items,
                        should_stop=should_stop,
                        graphql_doc_id=config.fb_marketplace_doc_id,
                    )
                    connect_fails = 0
                    break
                except AccountTokenDeadError:
                    raise
                except Exception as e:
                    last_err = e
                    if is_account_token_dead(e):
                        raise AccountTokenDeadError(str(e)) from e
                    if proxy_try and is_connection_error(e):
                        logger.warning("proxy failed for %s, retry direct: %s", cat.label, e)
                        continue
                    break

            if batch is None and last_err:
                err = last_err
                if is_account_token_dead(err):
                    raise AccountTokenDeadError(str(err)) from err
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

            if cat_added == 0:
                empty_rounds += 1
                current_step["detail"] = f"{cat.label}: 0 в JSON (см. отклонено)"
                if empty_rounds >= max_empty_rounds:
                    break
            await status_progress()
    except AccountTokenDeadError:
        token_dead_flag["value"] = True
        await _notify_token_dead(bot, telegram_id, on_status)
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

    got = len(collected)
    full = got >= json_limit
    stopped = stop.is_set()
    token_dead = token_dead_flag["value"]

    if token_dead:
        status_key = "token_expired"
    elif stopped:
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
        if token_dead:
            run.error_message = "token_expired"
        elif not full:
            run.error_message = f"Собрано {got}/{json_limit}"
        await session.commit()

    if token_dead:
        return

    if not full:
        reason = "остановлен" if stopped else "объявления не найдены"
        if on_status:
            await on_status(
                f"⚠️ Парсинг {reason}.\n"
                f"Собрано <b>{got}/{json_limit}</b> — JSON <b>не отправлен</b>.\n\n"
                "Чаще всего:\n"
                "• токен аккаунта протух — вставь новый\n"
                "• Facebook отдаёт пустую страницу — проверь прокси CH/FI\n"
                "• в логах Railway: <code>parsed 0 items</code> и <code>links=0</code>\n"
                "• мало полных карточек — обнови токен / прокси CH"
            )
        return

    export_items = [x for x in collected if listing_is_export_ready(x, country)][:json_limit]
    if len(export_items) < json_limit:
        if on_status:
            await on_status(
                f"⚠️ Полных объявлений только <b>{len(export_items)}/{json_limit}</b>.\n"
                "JSON не отправлен — нужны цена/продавец/фото как в VOID.\n"
                "Проверь токен, прокси 🇨🇭 и категории."
            )
        return

    payload = listings_to_json(export_items)
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
