"""Фоновый парсинг: JSON в конце или частичный JSON по ⏹ Стоп."""

from __future__ import annotations

import asyncio
import logging
import random
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
    MarketplaceFeedBlockedError,
    TOKEN_DEAD_USER_MESSAGE,
    is_account_token_dead,
    parse_account_token,
)
from parser.fb_http import fb_http_session
from parser.marketplace import (
    enrich_listing,
    country_export_reject_reason,
    feed_stage_reject_reason,
    normalize_listing_for_export,
    prepare_listing_for_void_export,
    fetch_category_listings,
    is_connection_error,
    listings_from_hub_html,
    listings_to_json,
    merge_marketplace_feeds,
    patch_listings_sellers_from_raw,
    _category_feed_html,
    _extract_item_doc_id,
    _seller_from_listing_chunk,
    void_complete_from_feed,
    void_export_reject_reason,
)
from parser.marketplace import prime_item_doc_id_cache
from parser.marketplace_region import (
    apply_marketplace_region,
    fetch_any_browse_feed,
    fetch_category_hub_snapshot,
    prime_category_feed_region,
)
from data.preset_categories import parse_categories_for_country
from services.proxies import pick_random_proxy_url
from services.seller_blacklist import (
    canonical_seller_key,
    dedupe_listings_by_seller,
    is_seller_blocked,
    load_blocked_seller_keys,
    normalize_seller_identity,
    remember_seller,
    seller_keys_for_item,
)
from security import protect_secret

logger = logging.getLogger(__name__)

_HARD_FEED_REJECT = frozenset({"старше_24ч", "нет_заголовка", "чужая_страна"})


_REJECT_LABELS: dict[str, str] = {
    "повторный_продавец": "Повторные продавцы",
    "чужая_страна": "Отсечено: не FI/CH",
    "мало_полей": "Нет цены/фото (не блокирует VOID JSON)",
    "нет_заголовка": "Нет заголовка",
    "старше_24ч": "Старше 24 ч",
    "нет_данных": "Нет продавца/описания",
    "нет_продавца": "Нет person_link (как VOID)",
    "нет_имени": "Нет имени продавца",
    "нет_описания": "Нет описания",
    "нет_даты_локации": "Нет даты/локации",
}


def _record_reject(stats: dict, reason: str) -> None:
    stats["rejected"] = stats.get("rejected", 0) + 1
    stats["last_reject"] = reason
    reasons = stats.setdefault("reject_reasons", {})
    reasons[reason] = reasons.get(reason, 0) + 1


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
            "feed_cards": 0,
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
    max_age_hours: float = 0,
    stop_reason: str = "",
) -> str:
    age_hint = ""
    if max_age_hours > 0:
        age_hint = f" · до {int(max_age_hours)} ч"
    lines = [
        f"🔎 <b>Показано: {done}/{total}</b>",
        f"<i>Режим VOID: ~{int(config.parse_item_delay_sec)} с/карточка, "
        f"{config.marketplace_pages_per_category} стр./категория{age_hint}. "
        f"Частичный JSON всегда. ⏹ Стоп — сразу файл.</i>",
    ]
    if stats:
        checked_n = stats.get("checked", 0)
        in_json = stats.get("accepted", done)
        reasons = stats.get("reject_reasons") or {}
        feed_new = stats.get("feed_new", 0)
        lines.append(
            f"📊 Проверено: <b>{checked_n}</b> · Новых с ленты: <b>{feed_new}</b> · "
            f"В JSON: <b>{in_json}</b> · Страниц: <b>{stats.get('pages', 0)}</b>"
        )
        if stats.get("feed_doc_auto"):
            doc_hint = stats["feed_doc_auto"][:12]
            if stats.get("feed_doc_is_browse"):
                lines.append(
                    f"<i>⚠️ doc_id из HTML — browse (<code>{doc_hint}…</code>), "
                    f"не лента категории. Задай <code>FB_MARKETPLACE_DOC_ID</code> со скролла category/…</i>"
                )
            else:
                lines.append(
                    f"<i>doc_id ленты категории из HTML: <code>{doc_hint}…</code></i>"
                )
        elif stop_reason == "нет_связи_fb":
            lines.append(
                "<i>Парсинг остановлен: Facebook не отдаёт ленту (прокси/блок). "
                "Смени прокси и перезапусти.</i>"
            )
        elif stats.get("pages", 0) == 0 and checked_n == 0:
            lines.append(
                "<i>⚠️ Лента не открылась (прокси/сеть). Проверь SOCKS5 CH/FI в ⚙️ → 🌐 Прокси. "
                "Токен тот же — VOID у тебя на ПК, бот ходит с Railway.</i>"
            )
        elif not (config.fb_marketplace_doc_id or "").strip() and in_json < total // 4:
            lines.append(
                "<i>⚠️ doc_id ленты не найден — только 1-я порция HTML (~20 карточек). "
                "Можно задать FB_MARKETPLACE_DOC_ID со скролла category/…</i>"
            )
        if reasons:
            lines.append("<b>Отсев (в JSON не попадают):</b>")
            for key, count in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
                label = _REJECT_LABELS.get(key, key)
                lines.append(f"→ {label}: <b>{count}</b>")
            if reasons.get("чужая_страна"):
                lines.append(
                    "<i>UA в ленте FB иногда есть при любом токене — бот их режет, "
                    "не сохраняет.</i>"
                )
    if step:
        lines.append(f"📍 {step}")
    return "\n".join(lines)


async def _send_json_file(
    bot: Bot,
    telegram_id: int,
    collected: list,
    country: str | None,
    json_limit: int,
    *,
    caption: str,
) -> int:
    if not collected:
        return 0
    export_items = dedupe_listings_by_seller(collected)[:json_limit]
    if len(collected) > len(export_items):
        logger.info(
            "export: %s listings -> %s unique sellers (VOID: 1 продавец = 1 строка)",
            len(collected),
            len(export_items),
        )
    for x in export_items:
        normalize_seller_identity(x)
        normalize_listing_for_export(x, country)
    if not export_items:
        logger.warning(
            "export empty: collected=%s after country/dedupe (country=%s)",
            len(collected),
            country,
        )
        return 0
    payload = listings_to_json(export_items)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        f.write(payload)
        path = f.name
    try:
        await bot.send_document(
            telegram_id,
            FSInputFile(path, filename=f"marketplace_{len(export_items)}.json"),
            caption=caption,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    return len(export_items)


async def _finalize_parse_run(
    bot: Bot,
    *,
    telegram_id: int,
    user_id: int,
    run_id: int,
    collected: list,
    country: str,
    json_limit: int,
    cat_count: int,
    stop: asyncio.Event,
    stop_reason: str,
    token_dead: bool,
    stats: dict,
    on_status: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Всегда сохраняет run и отправляет JSON, если есть хоть одно объявление."""
    got = len(collected)
    full = got >= json_limit
    stopped = stop.is_set()

    if full:
        stop_reason = stop_reason or "лимит"
    elif stopped:
        stop_reason = stop_reason or "стоп"
    elif stop_reason:
        pass
    else:
        stop_reason = "стоп"

    if token_dead:
        status_key = "token_expired"
    elif stopped:
        status_key = "stopped"
    elif full:
        status_key = "done"
    else:
        status_key = "partial"

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

    if got <= 0:
        if token_dead:
            await _notify_token_dead(bot, telegram_id, on_status)
        elif stop_reason == "лента_недоступна" and on_status:
            await on_status(
                "⚠️ <b>Аккаунт жив, но Marketplace-лента не открылась</b>\n\n"
                "Facebook запросил повторный вход только для ленты через текущий "
                "прокси/IP. Это не означает, что токен умер.\n"
                "<i>Смени прокси выбранной страны либо повтори позже.</i>"
            )
        elif on_status:
            reasons = stats.get("reject_reasons") or {}
            lines = [
                "⚠️ <b>В JSON: 0 объявлений</b>",
                "",
                f"Проверено: <b>{stats.get('checked', 0)}</b>",
            ]
            if reasons:
                for key, count in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
                    label = _REJECT_LABELS.get(key, key)
                    lines.append(f"→ {label}: <b>{count}</b>")
            lines.append(
                "\n<i>VOID JSON: полная карточка (цена, фото, описание, имя, "
                "person_link, дата, локация) — только после enrich с токеном. "
                "(цена+фото+profile). ЧС, старше 24 ч.</i>"
            )
            await on_status("\n".join(lines))
        return

    if token_dead:
        caption = f"⚠️ Токен умер — частичный JSON ({got}/{json_limit})"
    elif stopped:
        caption = f"⏹ Остановлено — частичный JSON ({got}/{json_limit})"
    elif full:
        caption = f"✅ Готово — {got} объявлений"
    else:
        caption = f"📦 Частичный JSON — {got}/{json_limit}"

    sent = await _send_json_file(
        bot,
        telegram_id,
        collected,
        country,
        json_limit,
        caption=caption,
    )
    lines = [f"📦 Отправлен JSON: <b>{sent}</b> объявлений."]
    if got > 0 and sent == 0:
        lines.append(
            "<i>В памяти были объявления, но в файл не попали "
            "(дубль продавца при экспорте).</i>"
        )
    if not full:
        lines.append(
            f"<i>Лимит в настройках {json_limit} — собрано {got}. "
            f"Файл отправлен в любом случае.</i>"
        )
    if token_dead:
        lines.append("")
        lines.append(TOKEN_DEAD_USER_MESSAGE)
    elif stopped:
        lines.append("<i>Парсинг остановлен вручную — JSON уже в чате.</i>")
    elif full:
        lines.append("<i>Лимит набран.</i>")
    elif stop_reason == "категории_исчерпаны":
        lines.append(
            f"<i>Все категории пройдены ({got}/{json_limit}). "
            f"В ленте могло быть больше карточек, в JSON — уникальные продавцы. "
            f"Если мало: <code>FB_MARKETPLACE_DOC_ID</code> со скролла "
            f"<code>switzerland/category/…</code> или <code>finland/category/…</code> "
            f"(не Search), прокси страны, 5–7 категорий.</i>"
        )
    else:
        dup = stats.get("reject_reasons", {}).get("повторный_продавец", 0)
        if dup:
            lines.append(
                f"<i>Отсев «повторные продавцы»: {dup}. "
                f"Очистка ЧС — ⚙️ Настройки → 🚫 Чёрный список.</i>"
            )
    if on_status:
        await on_status("\n".join(lines))
    if token_dead:
        try:
            await bot.send_message(telegram_id, TOKEN_DEAD_USER_MESSAGE)
        except Exception:
            pass


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
    max_age_hours = config.listing_max_age_hours
    gql_doc = config.fb_marketplace_doc_id

    async with Session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        json_limit = max(1, min(int(user.json_limit or 50), 500))
        country = user.country
        user.last_account_token = protect_secret(token_raw)
        await session.commit()

        if not country or country not in ("ch", "fi"):
            raise RuntimeError(
                "Выбери страну в ⚙️ Настройки → 🇨🇭 Швейцария или 🇫🇮 Финляндия"
            )
        categories = list(parse_categories_for_country(country))

        run = ParseRun(user_id=user_id, status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    collected: list = []
    seen_listing_ids: set[str] = set()
    accepted_listing_ids: set[str] = set()
    session_sellers: set[str] = set()
    accepted_seller_ids: set[str] = set()
    seen_lock = asyncio.Lock()
    async with Session() as session:
        blocked_sellers = await load_blocked_seller_keys(session, user_id, country)
    active_cats: list = list(categories)
    cat_count = len(categories)
    connect_fails = 0
    stop_reason = ""
    hub_round_counter = 0
    category_fetch_fails = 0

    t_start = time.monotonic()
    current_step = {"text": "Старт…"}
    job = _jobs.get(telegram_id)
    stats = job.stats if job else {}
    token_dead_flag = {"value": False}
    status_lock = asyncio.Lock()
    last_status_at = 0.0
    auth_wall_lock = asyncio.Lock()
    auth_wall_listings: set[str] = set()

    async def _interruptible(awaitable, default):
        """Выполнить сетевую операцию либо отменить её сразу после команды ⏹."""
        operation = asyncio.create_task(awaitable)
        stop_wait = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                {operation, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop.is_set():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                return default
            return await operation
        finally:
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)

    async def _gather_cancel_on_error(coroutines) -> None:
        """Не оставлять запросы старой задачи работать после первой ошибки."""
        tasks = [asyncio.create_task(coro) for coro in coroutines]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def status_progress(force: bool = False) -> None:
        nonlocal last_status_at
        stats["accepted"] = len(collected)
        if not on_status:
            return
        async with status_lock:
            now = time.monotonic()
            if not force and now - last_status_at < 3.0:
                return
            await on_status(
                _progress_text(
                    len(collected),
                    json_limit,
                    step=current_step["text"],
                    stats=stats,
                    max_age_hours=max_age_hours,
                    stop_reason=stop_reason,
                )
            )
            last_status_at = time.monotonic()

    if job:
        job.push_status = lambda: status_progress(True)

    async def heartbeat() -> None:
        while not stop.is_set():
            await asyncio.sleep(12)
            if len(collected) >= json_limit:
                return
            sec = int(time.monotonic() - t_start)
            current_step["text"] = f"⏳ {sec} сек — {current_step.get('detail', '…')}"
            await status_progress()

    hb_task = asyncio.create_task(heartbeat())

    country_label = ""
    if country == "ch":
        country_label = " 🇨🇭"
    elif country == "fi":
        country_label = " 🇫🇮"
    current_step["text"] = f"Категорий VOID: {cat_count}{country_label}"
    await status_progress(True)
    logger.info(
        "parse start tg=%s limit=%s cats=%s country=%s token_uid=%s access_token=%s",
        telegram_id,
        json_limit,
        cat_count,
        country,
        bool(token.c_user),
        bool(token.access_token),
    )

    async with Session() as session:
        proxy_url_boot = await pick_random_proxy_url(session, user_id)
    region_doc_ids: list[str] = []
    region_hub_html = ""
    parse_error: str | None = None

    async with fb_http_session(proxy_url_boot, token.cookies):
        try:
            current_step["text"] = f"Переключаю Marketplace на {country_label or country}…"
            await status_progress()
            region_doc_ids, region_hub_html = await _interruptible(
                apply_marketplace_region(
                    token,
                    country,
                    user_agent=config.fb_user_agent,
                    proxy_url=proxy_url_boot,
                    timeout_sec=22.0,
                ),
                ([], ""),
            )
            if region_doc_ids:
                from parser.marketplace import _browse_doc_id

                browse_d = _browse_doc_id()
                cat_ids = [d for d in region_doc_ids if d != browse_d]
                stats["feed_doc_auto"] = (cat_ids or region_doc_ids)[0]
                stats["feed_doc_is_browse"] = stats["feed_doc_auto"] == browse_d
                logger.info(
                    "region feed doc_ids: %s (category=%s browse=%s)",
                    len(region_doc_ids),
                    len(cat_ids),
                    browse_d[:12] if browse_d else "",
                )
            elif region_hub_html:
                from parser.marketplace import _hub_has_listings

                logger.info(
                    "region hub ok (%s bytes, has_listings=%s), doc_ids=%s",
                    len(region_hub_html),
                    _hub_has_listings(region_hub_html),
                    len(region_doc_ids),
                )
            if active_cats and not stop.is_set() and not token_dead_flag["value"]:
                snap_html, snap_docs = await _interruptible(
                    fetch_category_hub_snapshot(
                        token,
                        country,
                        active_cats[0].url_path,
                        user_agent=config.fb_user_agent,
                        proxy_url=proxy_url_boot,
                        timeout_sec=22.0,
                    ),
                    ("", []),
                )
                if snap_html and len(snap_html) > len(region_hub_html or ""):
                    region_hub_html = snap_html
                for d in snap_docs:
                    if d not in region_doc_ids:
                        region_doc_ids.insert(0, d)
                if snap_docs:
                    from parser.marketplace import _browse_doc_id

                    browse_d = _browse_doc_id()
                    cat_ids = [x for x in region_doc_ids if x != browse_d]
                    stats["feed_doc_auto"] = (cat_ids or region_doc_ids)[0]
                    stats["feed_doc_is_browse"] = stats["feed_doc_auto"] == browse_d
                logger.info(
                    "category snapshot merged: html=%s doc_ids=%s profiles=%s",
                    len(region_hub_html or ""),
                    len(region_doc_ids),
                    (region_hub_html or "").count("/marketplace/profile/"),
                )
        except AccountTokenDeadError:
            token_dead_flag["value"] = True

        proxy_used = proxy_url_boot
        accept_lock = asyncio.Lock()

        async def _accept_item(item) -> bool:
            normalize_seller_identity(item)
            lid = (item.listing_id or "").strip()
            if lid in accepted_listing_ids:
                return False
            ck = canonical_seller_key(item)
            if ck in accepted_seller_ids or is_seller_blocked(
                item, blocked_sellers | session_sellers
            ):
                _record_reject(stats, "повторный_продавец")
                return False
            reason = void_export_reject_reason(
                item, country, max_age_hours=max_age_hours
            )
            if reason:
                _record_reject(stats, reason)
                return False
            for sk in seller_keys_for_item(item):
                session_sellers.add(sk)
                blocked_sellers.add(sk)
            if ck:
                accepted_seller_ids.add(ck)
            async with Session() as session:
                await remember_seller(session, user_id, country, item)
            prepare_listing_for_void_export(item, country)
            if lid:
                accepted_listing_ids.add(lid)
            collected.append(item)
            return True

        async def _enrich_until_stopped(item) -> bool:
            """Отменить активные HTTP-запросы карточки сразу после команды ⏹."""
            enrich_task = asyncio.create_task(
                enrich_listing(
                    token,
                    item,
                    user_agent=config.fb_user_agent,
                    proxy_url=proxy_used,
                    timeout_sec=32.0,
                    country=country,
                    hub_html=region_hub_html,
                )
            )
            stop_task = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait(
                    {enrich_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop.is_set():
                    enrich_task.cancel()
                    await asyncio.gather(enrich_task, return_exceptions=True)
                    return False
                await enrich_task
                return True
            finally:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)

        async def _item_delay_until_stopped() -> None:
            delay = config.parse_item_delay_sec * random.uniform(0.9, 1.12)
            if delay <= 0 or stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

        async def _process_listing(item) -> tuple[bool, bool]:
            if stop.is_set() or len(collected) >= json_limit:
                return False, False
            async with seen_lock:
                lid = (item.listing_id or "").strip()
                if not lid or lid in seen_listing_ids:
                    return False, False
                seen_listing_ids.add(lid)
                stats["checked"] = stats.get("checked", 0) + 1
            blocked = blocked_sellers | session_sellers
            if is_seller_blocked(item, blocked):
                _record_reject(stats, "повторный_продавец")
                return False, True
            wrong_country = country_export_reject_reason(item, country)
            if wrong_country:
                _record_reject(stats, wrong_country)
                return False, False
            reason = feed_stage_reject_reason(
                item, country, max_age_hours=max_age_hours
            )
            if reason in _HARD_FEED_REJECT:
                _record_reject(stats, reason)
                return False, False
            if void_complete_from_feed(
                item, country, max_age_hours=max_age_hours
            ):
                async with accept_lock:
                    ok = await _accept_item(item)
                if ok:
                    await _item_delay_until_stopped()
                return ok, False
            if not stop.is_set() and region_hub_html:
                from services.seller_blacklist import _profile_id

                if not _profile_id(item):
                    patch = _seller_from_listing_chunk(
                        region_hub_html, item.listing_id
                    )
                    if patch:
                        from parser.marketplace import _merge_listing

                        _merge_listing(item, patch)
                if void_complete_from_feed(
                    item, country, max_age_hours=max_age_hours
                ):
                    async with accept_lock:
                        ok = await _accept_item(item)
                    if ok:
                        await _item_delay_until_stopped()
                    return ok, False
            if not stop.is_set():
                try:
                    completed = await _enrich_until_stopped(item)
                    if not completed:
                        return False, False
                except AccountTokenDeadError:
                    token_dead_flag["value"] = True
                    raise
                if item.auth_login_wall:
                    async with auth_wall_lock:
                        if item.listing_id:
                            auth_wall_listings.add(item.listing_id)
                        wall_count = len(auth_wall_listings)
                    logger.warning(
                        "account login wall on %s distinct listings", wall_count
                    )
                    if wall_count >= 4:
                        raise AccountTokenDeadError(
                            "Facebook requires login on multiple distinct listings"
                        )
                else:
                    async with auth_wall_lock:
                        auth_wall_listings.clear()
            async with accept_lock:
                ok = await _accept_item(item)
            if ok:
                await _item_delay_until_stopped()
            return ok, False

        if not token_dead_flag["value"] and region_hub_html and not stop.is_set():
            _category_feed_html.set(region_hub_html)
            _extract_item_doc_id(region_hub_html)
            browse_raw = ""
            comet_items: list = []
            try:
                comet_items, browse_raw = await _interruptible(
                    fetch_any_browse_feed(
                        token,
                        country,
                        hint_html=region_hub_html,
                        user_agent=config.fb_user_agent,
                        proxy_url=proxy_url_boot,
                        limit=max(json_limit * 3, 150),
                    ),
                    ([], ""),
                )
            except (ValueError, TypeError) as e:
                logger.warning("browse feed unpack error: %s", e)
                comet_items, browse_raw = [], ""
            hub_items = listings_from_hub_html(region_hub_html)
            boot_items = merge_marketplace_feeds(comet_items, hub_items)
            from services.seller_blacklist import _profile_id

            if browse_raw:
                patch_listings_sellers_from_raw(browse_raw, boot_items)
            patch_listings_sellers_from_raw(region_hub_html, boot_items)
            with_profile = sum(1 for it in boot_items if _profile_id(it))
            logger.info(
                "post-region feed: %s items, person_link=%s",
                len(boot_items),
                with_profile,
            )
            first_lid = next(
                ((it.listing_id or "").strip() for it in boot_items if (it.listing_id or "").strip()),
                "",
            )
            if first_lid:
                await _interruptible(
                    prime_item_doc_id_cache(
                        token,
                        first_lid,
                        user_agent=config.fb_user_agent,
                        proxy_url=proxy_url_boot,
                        timeout_sec=24.0,
                        country=country,
                    ),
                    None,
                )
            boot_sem = asyncio.Semaphore(4)

            async def _boot_one(it) -> None:
                if stop.is_set() or len(collected) >= json_limit:
                    return
                async with boot_sem:
                    if not _profile_id(it):
                        patch = _seller_from_listing_chunk(
                            region_hub_html, it.listing_id
                        )
                        if patch:
                            from parser.marketplace import _merge_listing as _ml

                            _ml(it, patch)
                    ok, _ = await _process_listing(it)
                    if ok:
                        logger.info(
                            "accepted %s (json %s/%s)",
                            (it.listing_id or "")[:14],
                            len(collected),
                            json_limit,
                        )

            await _gather_cancel_on_error(_boot_one(it) for it in boot_items)

        try:
            while (
                not token_dead_flag["value"]
                and len(collected) < json_limit
                and active_cats
                and not stop.is_set()
            ):
                cat = active_cats[0]
                current_step["detail"] = cat.label
                current_step["text"] = (
                    f"Категория: {cat.label} · активных {len(active_cats)}/{cat_count}"
                )
                await status_progress()
    
                proxy_url = proxy_url_boot
                proxy_used = proxy_url

                try:
                    cat_html, cat_docs = await _interruptible(
                        prime_category_feed_region(
                            token,
                            country,
                            cat.url_path,
                            user_agent=config.fb_user_agent,
                            proxy_url=proxy_url,
                        ),
                        ("", []),
                    )
                    if stop.is_set():
                        break
                    if cat_html:
                        if len(cat_html) > len(region_hub_html or ""):
                            region_hub_html = cat_html
                        _category_feed_html.set(cat_html)
                        for d in cat_docs:
                            if d not in region_doc_ids:
                                region_doc_ids.insert(0, d)
                        if cat_docs:
                            from parser.marketplace import _browse_doc_id

                            browse_d = _browse_doc_id()
                            cids = [x for x in region_doc_ids if x != browse_d]
                            stats["feed_doc_auto"] = (cids or region_doc_ids)[0]
                            stats["feed_doc_is_browse"] = (
                                stats["feed_doc_auto"] == browse_d
                            )
                except AccountTokenDeadError:
                    raise
    
                async def on_url(i: int, n: int, short: str) -> None:
                    current_step["detail"] = f"{cat.label} ({i}/{n}) {short}"
                    current_step["text"] = f"⏳ {current_step['detail']}"
                    await status_progress()
    
                async def on_page_found(n: int) -> None:
                    stats["pages"] = stats.get("pages", 0) + 1
                    stats["found"] = stats.get("feed_cards", 0)
                    await status_progress()
    
                cat_added = 0
                page_raw = 0
                stop_cat_pages = {"value": False}

                async def on_page_items(page_items: list) -> None:
                    nonlocal cat_added, page_raw
                    page_raw = len(page_items)
                    stats["feed_cards"] = stats.get("feed_cards", 0) + page_raw
                    stats["feed_new"] = stats.get("feed_new", 0) + page_raw
                    page_acc = 0
                    page_skip = 0
                    page_skip_dup = 0
                    enrich_sem = asyncio.Semaphore(5)
    
                    async def _run_one(item) -> None:
                        nonlocal cat_added, page_acc, page_skip, page_skip_dup
                        if stop.is_set() or len(collected) >= json_limit:
                            return
                        async with enrich_sem:
                            ok, dup = await _process_listing(item)
                        if dup:
                            page_skip += 1
                            page_skip_dup += 1
                        elif ok:
                            cat_added += 1
                            page_acc += 1
                            current_step["detail"] = (
                                f"+{cat_added} {cat.label} · "
                                f"в JSON {len(collected)}/{json_limit}"
                            )
                            await status_progress()
                        else:
                            page_skip += 1
                            last = stats.get("last_reject")
                            if last == "повторный_продавец":
                                page_skip_dup += 1
                            if last and last != "повторный_продавец":
                                current_step["detail"] = (
                                    f"отсев: {_REJECT_LABELS.get(last, last)} · "
                                    f"JSON {len(collected)}/{json_limit}"
                                )
                                await status_progress()
    
                    await _gather_cancel_on_error(_run_one(it) for it in page_items)
    
                    logger.info(
                        "page %s: feed=%s checked=%s accepted=%s skip=%s collected=%s",
                        cat.label,
                        page_raw,
                        stats.get("checked"),
                        page_acc,
                        page_skip,
                        len(collected),
                    )
    
                    min_before_dup_stop = min(json_limit, max(40, json_limit // 2))
                    if (
                        len(collected) >= min_before_dup_stop
                        and page_raw >= 8
                        and page_acc == 0
                        and page_skip_dup >= int(page_raw * config.feed_dup_stop_ratio)
                    ):
                        stop_cat_pages["value"] = True
                        logger.info(
                            "stop pages %s: %.0f%% repeat sellers (collected=%s)",
                            cat.label,
                            100 * page_skip_dup / page_raw,
                            len(collected),
                        )
    
                def should_stop() -> bool:
                    return stop.is_set() or len(collected) >= json_limit
    
                def should_stop_pagination() -> bool:
                    return stop_cat_pages["value"]
    
                fetch_meta: dict = {}
                last_err: Exception | None = None
                proxy_used: str | None = None
                proxy_attempts: tuple[str | None, ...]
                if proxy_url:
                    proxy_attempts = (proxy_url,)
                else:
                    proxy_attempts = (None,)
                for proxy_try in proxy_attempts:
                    try:
                        proxy_used = proxy_try
                        _, fetch_meta = await _interruptible(
                            fetch_category_listings(
                                token,
                                url_path=cat.url_path,
                                category_label=cat.label,
                                user_agent=config.fb_user_agent,
                                country=country,
                                proxy_url=proxy_try,
                                limit=max(json_limit * 12, 500),
                                timeout_sec=35.0,
                                on_url_progress=on_url,
                                on_page_found=on_page_found,
                                on_page_items=on_page_items,
                                should_stop=should_stop,
                                should_stop_pagination=should_stop_pagination,
                                hub_round=hub_round_counter,
                                graphql_doc_id=gql_doc,
                                region_hint_html=region_hub_html,
                                seed_doc_ids=region_doc_ids,
                            ),
                            ([], {"stopped": True}),
                        )
                        if stop.is_set():
                            break
                        hub_round_counter += 1
                        auto_doc = fetch_meta.get("feed_doc_auto")
                        if auto_doc:
                            stats["feed_doc_auto"] = str(auto_doc)
                        connect_fails = 0
                        break
                    except AccountTokenDeadError:
                        raise
                    except Exception as e:
                        last_err = e
                        if is_account_token_dead(e):
                            raise AccountTokenDeadError(str(e)) from e
                        if proxy_try and is_connection_error(e):
                            logger.warning("category fetch failed %s: %s", cat.label, e)
                        break
    
                if last_err is not None and not fetch_meta.get("pages_fetched"):
                    err = last_err
                    if is_account_token_dead(err):
                        raise AccountTokenDeadError(str(err)) from err
                    logger.warning("category %s failed: %s", cat.label, err)
                    category_fetch_fails += 1
                    if is_connection_error(err):
                        connect_fails += 1
                        current_step["text"] = (
                            f"❌ Нет ответа Facebook ({category_fetch_fails}) · "
                            f"проверь прокси CH/FI"
                        )
                        await status_progress()
                        if connect_fails >= 3 and len(collected) == 0:
                            hint = (
                                "❌ <b>Facebook не отвечает</b> (прокси / редиректы).\n\n"
                                "Проверь SOCKS5 CH/FI в ⚙️ → 🌐 Прокси. "
                                "Парсинг остановлен — попробуй другой прокси."
                            )
                            if on_status:
                                await on_status(hint)
                            stop_reason = "нет_связи_fb"
                            stop.set()
                            break
                    if category_fetch_fails >= 18 and len(collected) == 0:
                        stop_reason = "нет_связи_fb"
                        stop.set()
                        break
                    active_cats = active_cats[1:] + [cat]
                    continue
                category_fetch_fails = 0
    
                if fetch_meta.get("stopped_dup_page"):
                    logger.info("category dup-page stop: %s", cat.label)
                    current_step["text"] = (
                        f"↪ {cat.label}: в основном старые/повторы · "
                        f"Показано {len(collected)}/{json_limit}"
                    )
                    active_cats = active_cats[1:] + [cat]
                elif fetch_meta.get("exhausted"):
                    logger.info("category exhausted: %s", cat.label)
                    current_step["text"] = (
                        f"❌ {cat.label}: лента закончилась · "
                        f"Показано {len(collected)}/{json_limit}"
                    )
                    active_cats = [c for c in active_cats if c.key != cat.key]
                else:
                    active_cats = active_cats[1:] + [cat]
    
                await status_progress()
                if config.parse_category_delay_sec > 0 and not stop.is_set():
                    await _interruptible(
                        asyncio.sleep(config.parse_category_delay_sec),
                        None,
                    )
        except AccountTokenDeadError:
            token_dead_flag["value"] = True
        except MarketplaceFeedBlockedError as e:
            stop_reason = "лента_недоступна"
            logger.warning("marketplace feed blocked tg=%s: %s", telegram_id, e)
        except Exception as e:
            parse_error = str(e)
            logger.exception("parse loop error tg=%s", telegram_id)
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
            if not active_cats and len(collected) < json_limit:
                stop_reason = stop_reason or "категории_исчерпаны"

    await _finalize_parse_run(
        bot,
        telegram_id=telegram_id,
        user_id=user_id,
        run_id=run_id,
        collected=collected,
        country=country,
        json_limit=json_limit,
        cat_count=cat_count,
        stop=stop,
        stop_reason=stop_reason,
        token_dead=token_dead_flag["value"],
        stats=stats,
        on_status=on_status,
    )
    if parse_error and on_status and len(collected) > 0:
        await on_status(
            f"⚠️ Парсинг прерван ошибкой, но JSON уже отправлен ({len(collected)} шт.).\n"
            f"<i>{parse_error[:200]}</i>"
        )
