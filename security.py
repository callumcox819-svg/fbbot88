"""Encryption helpers for credentials stored in the application database."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    key = (os.getenv("DATA_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise RuntimeError("Задай DATA_ENCRYPTION_KEY для шифрования секретов в БД")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("DATA_ENCRYPTION_KEY должен быть корректным ключом Fernet") from exc


def validate_encryption_key() -> None:
    _fernet()


def protect_secret(value: str | None) -> str | None:
    if not value or value.startswith(_PREFIX):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def reveal_secret(value: str | None) -> str | None:
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(
            "Не удалось расшифровать данные: проверь DATA_ENCRYPTION_KEY"
        ) from exc
