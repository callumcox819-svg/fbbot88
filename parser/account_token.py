"""Токен Facebook-аккаунта (строка как в VOID: uid|xs|datr|fr|access_token)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import unquote


@dataclass
class AccountToken:
    cookies: dict[str, str]
    access_token: str | None
    raw: str

    @property
    def c_user(self) -> str:
        return self.cookies.get("c_user", "")


def is_account_token_line(text: str) -> bool:
    line = (text or "").strip()
    if "|" not in line:
        return False
    parts = line.split("|")
    if len(parts) < 2:
        return False
    uid = parts[0].strip()
    xs = parts[1].strip()
    return uid.isdigit() and len(uid) >= 8 and bool(xs)


def parse_account_token(raw: str) -> AccountToken:
    line = (raw or "").strip()
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2:
        raise ValueError("Токен аккаунта: минимум uid|xs|…")

    c_user = parts[0]
    if not c_user.isdigit():
        raise ValueError("Первая часть — числовой id аккаунта Facebook")

    xs = unquote(parts[1])
    cookies: dict[str, str] = {"c_user": c_user, "xs": xs}
    access_token: str | None = None

    if len(parts) >= 5:
        if parts[2]:
            cookies["datr"] = unquote(parts[2])
        if parts[3]:
            cookies["fr"] = unquote(parts[3])
        if parts[4]:
            access_token = parts[4]
    elif len(parts) == 4:
        if len(parts[3]) > 80:
            if parts[2]:
                cookies["datr"] = unquote(parts[2])
            access_token = parts[3]
        else:
            if parts[2]:
                cookies["datr"] = unquote(parts[2])
            cookies["fr"] = unquote(parts[3])
    elif len(parts) == 3 and parts[2]:
        cookies["datr"] = unquote(parts[2])

    return AccountToken(cookies=cookies, access_token=access_token, raw=line)


def account_token_to_storage(token: AccountToken) -> str:
    return json.dumps(
        {"raw": token.raw, "cookies": token.cookies, "access_token": token.access_token},
        ensure_ascii=False,
    )


def account_token_from_storage(stored: str) -> AccountToken:
    data = json.loads(stored)
    if isinstance(data, str):
        return parse_account_token(data)
    return AccountToken(
        cookies={str(k): str(v) for k, v in (data.get("cookies") or {}).items()},
        access_token=data.get("access_token"),
        raw=data.get("raw") or "",
    )


def cookies_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


class AccountTokenDeadError(RuntimeError):
    """Cookies/access_token Facebook больше не принимаются."""


TOKEN_DEAD_USER_MESSAGE = (
    "❌ Все токены не валидны или истекли, попробуйте получить их заново"
)


def is_account_token_dead(exc: BaseException) -> bool:
    if isinstance(exc, AccountTokenDeadError):
        return True
    s = str(exc).lower()
    return any(
        x in s
        for x in (
            "недействителен",
            "cookies истекли",
            "страницу входа",
            "loginform",
            "invalid token",
            "session has expired",
        )
    )
