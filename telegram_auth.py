"""Telegram WebApp initData verification (HMAC + Telegram Ed25519)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class TelegramInitDataError(ValueError):
    """Telegram WebApp initData is malformed, expired, or has an invalid signature."""


# Telegram's production public key for third-party Mini App initData validation.
# Source: https://core.telegram.org/bots/webapps#validating-data-for-third-party-use
TELEGRAM_ED25519_PUBLIC_KEY = (
    "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d"
)


def _verify_ed25519_signature(
    values: dict[str, str],
    bot_id: str,
    public_key_hex: str = TELEGRAM_ED25519_PUBLIC_KEY,
) -> bool:
    """Verify Telegram's bot_id-bound Ed25519 signature without exposing payload data."""
    signature_text = values.get("signature", "")
    if not signature_text or not bot_id.isdigit():
        return False

    try:
        # Telegram sends unpadded base64url. Restore padding and reject other characters.
        encoded = signature_text.encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        signature = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(signature) != 64:
            return False

        public_key_bytes = bytes.fromhex(public_key_hex)
        if len(public_key_bytes) != 32:
            return False

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(values.items())
            if key not in {"hash", "signature"}
        )
        signed_data = (
            f"{bot_id}:WebAppData\n{data_check_string}"
        ).encode("utf-8")

        # Imported lazily so parsing and HMAC-only deployments remain importable.
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
                signature, signed_data
            )
            return True
        except InvalidSignature:
            return False
    except (ValueError, TypeError, UnicodeEncodeError, ImportError):
        return False


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 600,
    future_skew_seconds: int = 30,
    now: int | None = None,
) -> dict:
    """Verify Telegram WebApp initData and return its decoded user object.

    The bot-owner HMAC is the primary check. Telegram's documented Ed25519
    signature is accepted as a cryptographically independent fallback, bound
    to this bot's numeric ID and Telegram's production public key.
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
        f"{key}={value}" for key, value in sorted(values.items())
        if key != "hash"
    )
    secret_key = hmac.new(
        bot_token.encode("utf-8"), b"WebAppData", hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    hmac_match = hmac.compare_digest(expected_hash, received_hash.lower())

    ed25519_match = False
    if not hmac_match:
        bot_id = bot_token.split(":", 1)[0]
        ed25519_match = _verify_ed25519_signature(values, bot_id)

        if not ed25519_match:
            # Diagnostic booleans only. Never log initData, user data, hashes,
            # signatures, bot tokens, or any part of their values.
            without_signature_string = "\n".join(
                f"{key}={value}" for key, value in sorted(values.items())
                if key not in {"hash", "signature"}
            )
            without_signature_hash = hmac.new(
                secret_key,
                without_signature_string.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            without_signature_match = hmac.compare_digest(
                without_signature_hash, received_hash.lower()
            )
            diagnostic = (
                "Telegram signature verification failed "
                f"(init_data_length={len(init_data)}, "
                f"field_count={len(values)}, "
                f"signature_present={'signature' in values}, "
                f"user_present={'user' in values}, "
                f"auth_date_present={'auth_date' in values}, "
                f"query_id_present={'query_id' in values}, "
                f"chat_instance_present={'chat_instance' in values}, "
                f"chat_type_present={'chat_type' in values}, "
                "hmac_match=False, "
                f"hmac_without_signature_match={without_signature_match}, "
                "ed25519_match=False)"
            )
            raise TelegramInitDataError(diagnostic)

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
