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
    )


config = load_config()
