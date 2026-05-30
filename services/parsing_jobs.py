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
    TOKEN_DEAD_USER_MESSAGE,
    is_account_token_dead,
    parse_account_token,
)
from parser.marketplace import (
    enrich_listing,
    export_reject_reason,
    normalize_listing_for_export,
    fetch_category_listings,
    is_connection_error,
    listings_to_json,
    void_complete_from_feed,
    void_export_reject_reason,
)
from parser.marketplace_region import apply_marketplace_region, prime_category_feed_region
from data.preset_categories import parse_categories_for_country
from services.proxies import pick_random_proxy_url
from services.seller_blacklist import (
    is_seller_blocked,
    load_blocked_seller_keys,
    normalize_seller_identity,
)

logger = logging.getLogger(__name__)

_HARD_FEED_REJECT = frozenset({"старше_24ч", "нет_заголовка"})


async def _sleep_human(base_sec: float) -> None:
    if base_sec > 0:
        await asyncio.sleep(base_sec * random.uniform(0.9, 1.12))


_REJECT_LABELS: dict[str, str] = {
    "повторный_продавец": "В вашем ЧС",
    "чужая_страна": "Чужая страна",
    "мало_полей": "Мало полей",
    "нет_заголовка": "Нет заголовка",
    "старше_24ч": "Старше 24 ч",
    "нет_данных": "Нет продавца/описания",
    "нет_продавца": "Нет person_link (как VOID)",
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
        lines.append(
            f"📊 Проверено: <b>{checked_n}</b> · В JSON: <b>{in_json}</b> · "
            f"Страниц: <b>{stats.get('pages', 0)}</b>"
        )
        if reasons:
            lines.append("<b>Отсев:</b>")
            for key, count in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
                label = _REJECT_LABELS.get(key, key)
                lines.append(f"→ {label}: <b>{count}</b>")
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
    export_items = collected[:json_limit]
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
                "\n<i>Как VOID: нужен person_link с карточки /marketplace/item/… "
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
            f"Если мало — в Railway задай FB_MARKETPLACE_DOC_ID для глубокой ленты.</i>"
        )
    else:
        dup = stats.get("reject_reasons", {}).get("повторный_продавец", 0)
        if dup:
            lines.append(
                f"<i>В ЧС (ручной список): {dup}. Очистка — в настройках бота.</i>"
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
        user.last_account_token = token_raw
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
    async with Session() as session:
        manual_blocked = await load_blocked_seller_keys(session, user_id, country)
    active_cats: list = list(categories)
    cat_count = len(categories)
    connect_fails = 0
    stop_reason = ""

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
                    max_age_hours=max_age_hours,
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
            current_step["text"] = f"⏳ {sec} сек — {current_step.get('detail', '…')}"
            await status_progress()

    hb_task = asyncio.create_task(heartbeat())

    country_label = ""
    if country == "ch":
        country_label = " 🇨🇭"
    elif country == "fi":
        country_label = " 🇫🇮"
    current_step["text"] = f"Категорий VOID: {cat_count}{country_label}"
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
            timeout_sec=18.0,
        )
    except AccountTokenDeadError:
        token_dead_flag["value"] = True

    parse_error: str | None = None
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

            async with Session() as session:
                proxy_url = await pick_random_proxy_url(session, user_id)

            try:
                await prime_category_feed_region(
                    token,
                    country,
                    cat.url_path,
                    user_agent=config.fb_user_agent,
                    proxy_url=proxy_url,
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
            processed_in_cat: set[str] = set()
            stop_cat_pages = {"value": False}
            accept_lock = asyncio.Lock()
            seen_lock = asyncio.Lock()

            async def _accept_item(item) -> bool:
                normalize_seller_identity(item)
                if is_seller_blocked(item, manual_blocked):
                    _record_reject(stats, "повторный_продавец")
                    return False
                reason = void_export_reject_reason(
                    item, country, max_age_hours=max_age_hours
                )
                if reason:
                    _record_reject(stats, reason)
                    return False
                normalize_listing_for_export(item, country)
                collected.append(item)
                return True

            async def _process_listing(item) -> tuple[bool, bool]:
                """Возвращает (accepted, dup_skip)."""
                if stop.is_set() or len(collected) >= json_limit:
                    return False, False
                async with seen_lock:
                    if item.listing_id in processed_in_cat:
                        return False, False
                    stats["checked"] = stats.get("checked", 0) + 1
                    processed_in_cat.add(item.listing_id)
                if is_seller_blocked(item, manual_blocked):
                    _record_reject(stats, "повторный_продавец")
                    return False, True
                reason = export_reject_reason(
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
                        await _sleep_human(config.parse_item_delay_sec)
                    return ok, False
                if not stop.is_set():
                    try:
                        await enrich_listing(
                            token,
                            item,
                            user_agent=config.fb_user_agent,
                            proxy_url=proxy_used,
                            timeout_sec=24.0,
                            country=country,
                        )
                    except AccountTokenDeadError:
                        token_dead_flag["value"] = True
                        raise
                async with accept_lock:
                    ok = await _accept_item(item)
                if ok:
                    await _sleep_human(config.parse_item_delay_sec)
                return ok, False

            async def on_page_items(page_items: list) -> None:
                nonlocal cat_added, page_raw
                page_raw = len(page_items)
                stats["feed_cards"] = stats.get("feed_cards", 0) + page_raw
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

                await asyncio.gather(*[_run_one(it) for it in page_items])

                logger.info(
                    "page %s: feed=%s checked=%s accepted=%s skip=%s collected=%s",
                    cat.label,
                    page_raw,
                    stats.get("checked"),
                    page_acc,
                    page_skip,
                    len(collected),
                )

                if page_raw >= 8 and page_acc == 0 and page_skip_dup >= int(
                    page_raw * config.feed_dup_stop_ratio
                ):
                    stop_cat_pages["value"] = True
                    logger.info(
                        "stop pages %s: %.0f%% dup sellers (VOID-style)",
                        cat.label,
                        100 * page_skip_dup / page_raw,
                    )

            def should_stop() -> bool:
                return stop.is_set() or len(collected) >= json_limit

            def should_stop_pagination() -> bool:
                return stop_cat_pages["value"]

            fetch_meta: dict = {}
            last_err: Exception | None = None
            proxy_used: str | None = None
            for proxy_try in (proxy_url, None):
                if proxy_try is None and proxy_url is None:
                    break
                try:
                    proxy_used = proxy_try
                    _, fetch_meta = await fetch_category_listings(
                        token,
                        url_path=cat.url_path,
                        category_label=cat.label,
                        user_agent=config.fb_user_agent,
                        country=country,
                        proxy_url=proxy_try,
                        limit=max(json_limit * 12, 500),
                        timeout_sec=22.0,
                        on_url_progress=on_url,
                        on_page_found=on_page_found,
                        on_page_items=on_page_items,
                        should_stop=should_stop,
                        should_stop_pagination=should_stop_pagination,
                        hub_round=None,
                        graphql_doc_id=gql_doc,
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

            if last_err is not None and not fetch_meta.get("pages_fetched"):
                err = last_err
                if is_account_token_dead(err):
                    raise AccountTokenDeadError(str(err)) from err
                logger.warning("category %s failed: %s", cat.label, err)
                if is_connection_error(err):
                    connect_fails += 1
                    if connect_fails >= 3 and len(collected) == 0:
                        hint = (
                            "❌ <b>Нет связи с Facebook</b>\n\n"
                            "Добавь SOCKS5 прокси страны поиска в ⚙️ Настройки → 🌐 Прокси."
                        )
                        if on_status:
                            await on_status(hint)
                        raise RuntimeError("Нет связи с Facebook") from err
                active_cats = active_cats[1:] + [cat]
                continue

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
                await asyncio.sleep(config.parse_category_delay_sec)
    except AccountTokenDeadError:
        token_dead_flag["value"] = True
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
