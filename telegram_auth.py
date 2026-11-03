"""Telegram WebApp initData verification (server-side, stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class TelegramInitDataError(ValueError):
    """Telegram initData is malformed, expired, or has an invalid signature."""


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 600,
    future_skew_seconds: int = 30,
    now: int | None = None,
) -> dict:
    """Verify Telegram WebApp initData and return its decoded user object.

    The bot token must come from server-side secret storage. Never accept a
    token supplied by the browser, and never log init_data or the bot token.
    """
    if not isinstance(init_data, str) or not init_data or len(init_data) > 16_384:
        raise TelegramInitDataError("invalid initData")
    if not isinstance(bot_token, str) or not bot_token:
        raise TelegramInitDataError("Telegram authentication is not configured")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise TelegramInitDataError("malformed initData") from exc

    values = {}
    for key, value in pairs:
        if key in values:
            raise TelegramInitDataError("duplicate initData field")
        values[key] = value

    received_hash = values.get("hash", "")
    if len(received_hash) != 64:
        raise TelegramInitDataError("missing or invalid hash")

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
        if key not in {"hash", "signature"}
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise TelegramInitDataError("Telegram signature verification failed")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramInitDataError("required Telegram fields are invalid") from exc

    timestamp = int(time.time()) if now is None else int(now)
    if auth_date > timestamp + future_skew_seconds:
        raise TelegramInitDataError("initData timestamp is in the future")
    if timestamp - auth_date > max_age_seconds:
        raise TelegramInitDataError("initData has expired")
    if user_id <= 0:
        raise TelegramInitDataError("invalid Telegram user id")

    return user
