import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _admin_ids() -> frozenset[int]:
    raw = (os.getenv("ADMIN_IDS") or "").strip()
    if not raw:
        return frozenset()
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    fb_user_agent: str
    fb_marketplace_doc_id: str | None
    fb_marketplace_browse_doc_id: str | None
    listing_max_age_hours: float
    parse_item_delay_sec: float
    parse_category_delay_sec: float
    parse_page_delay_sec: float
    marketplace_pages_per_category: int
    feed_dup_stop_ratio: float


def load_config() -> Config:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Задай BOT_TOKEN в .env")

    return Config(
        bot_token=token,
        admin_ids=_admin_ids(),
        fb_user_agent=(
            os.getenv("FB_USER_AGENT")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ).strip(),
        fb_marketplace_doc_id=(os.getenv("FB_MARKETPLACE_DOC_ID") or "").strip() or None,
        fb_marketplace_browse_doc_id=(
            (os.getenv("FB_MARKETPLACE_BROWSE_DOC_ID") or "2022753507811174").strip() or None
        ),
        listing_max_age_hours=float(os.getenv("LISTING_MAX_AGE_HOURS") or "24"),
        parse_item_delay_sec=float(os.getenv("PARSE_ITEM_DELAY_SEC") or "6"),
        parse_category_delay_sec=float(os.getenv("PARSE_CATEGORY_DELAY_SEC") or "10"),
        parse_page_delay_sec=float(os.getenv("PARSE_PAGE_DELAY_SEC") or "3"),
        marketplace_pages_per_category=int(os.getenv("MARKETPLACE_PAGES_PER_CATEGORY") or "3"),
        feed_dup_stop_ratio=float(os.getenv("FEED_DUP_STOP_RATIO") or "0.85"),
    )


config = load_config()
