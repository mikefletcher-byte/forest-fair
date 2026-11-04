"""Tests for Telegram WebApp initData verification."""

import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telegram_auth import TelegramInitDataError, verify_telegram_init_data


BOT_TOKEN = "123456:TEST_TOKEN_FOR_UNIT_TESTS_ONLY"
TEST_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
TEST_PUBLIC_KEY = TEST_PRIVATE_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
).hex()


def make_init_data(
    user=None,
    auth_date=1_800_000_000,
    token=BOT_TOKEN,
    signature=None,
    ed25519_private_key=None,
    hmac_include_signature=True,
):
    values = {
        "auth_date": str(auth_date),
        "query_id": "AAE-test-query",
        "user": json.dumps(
            user or {"id": 12345, "first_name": "Test"},
            separators=(",", ":"),
        ),
    }
    if ed25519_private_key is not None:
        bot_id = token.split(":", 1)[0]
        signed_fields = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items())
        )
        signed_data = f"{bot_id}:WebAppData\n{signed_fields}".encode()
        signature = base64.urlsafe_b64encode(
            ed25519_private_key.sign(signed_data)
        ).decode("ascii").rstrip("=")
    if signature is not None:
        values["signature"] = signature

    hmac_values = dict(values)
    if not hmac_include_signature:
        hmac_values.pop("signature", None)
    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(hmac_values.items())
    )
    secret = hmac.new(token.encode(), b"WebAppData", hashlib.sha256).digest()
    values["hash"] = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(values)


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_data_returns_verified_user(self):
        raw = make_init_data()
        self.assertEqual(
            verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)["id"],
            12345,
        )

    def test_valid_data_with_signature_field(self):
        raw = make_init_data(signature="test-ed25519-signature")
        self.assertEqual(
            verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)["id"],
            12345,
        )

    def test_signature_field_is_covered_by_hmac(self):
        raw = make_init_data(signature="original-signature")
        tampered = raw.replace("original-signature", "changed-signature")
        with self.assertRaises(TelegramInitDataError) as context:
            verify_telegram_init_data(tampered, BOT_TOKEN, now=1_800_000_100)
        self.assertIn("signature_present=True", str(context.exception))
        self.assertIn("hmac_without_signature_match=False", str(context.exception))
        self.assertNotIn("original-signature", str(context.exception))

    def test_valid_ed25519_signature_is_accepted_when_hmac_differs(self):
        raw = make_init_data(
            ed25519_private_key=TEST_PRIVATE_KEY,
            hmac_include_signature=False,
        )
        with patch("telegram_auth.TELEGRAM_ED25519_PUBLIC_KEY", TEST_PUBLIC_KEY):
            user = verify_telegram_init_data(
                raw, BOT_TOKEN, now=1_800_000_100
            )
        self.assertEqual(user["id"], 12345)

    def test_invalid_ed25519_signature_is_rejected_when_hmac_differs(self):
        other_key = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
        raw = make_init_data(
            ed25519_private_key=TEST_PRIVATE_KEY,
            hmac_include_signature=False,
        )
        wrong_public_key = other_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        with patch("telegram_auth.TELEGRAM_ED25519_PUBLIC_KEY", wrong_public_key):
            with self.assertRaises(TelegramInitDataError):
                verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)

    def test_tampered_user_is_rejected_by_ed25519_fallback(self):
        raw = make_init_data(
            ed25519_private_key=TEST_PRIVATE_KEY,
            hmac_include_signature=False,
        ).replace("first_name", "last_name")
        with patch("telegram_auth.TELEGRAM_ED25519_PUBLIC_KEY", TEST_PUBLIC_KEY):
            with self.assertRaises(TelegramInitDataError):
                verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)

    def test_url_encoded_plus_unicode_and_slashes_round_trip(self):
        user = {
            "id": 12345,
            "first_name": "Тест + Проверка /",
            "username": "user+test",
        }
        raw = make_init_data(user=user)
        self.assertEqual(
            verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100),
            user,
        )

    def test_tampered_data_is_rejected(self):
        raw = make_init_data().replace("first_name", "last_name")
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)

    def test_wrong_bot_token_is_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(
                make_init_data(), "different-token", now=1_800_000_100
            )

    def test_expired_data_is_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(
                make_init_data(), BOT_TOKEN, now=1_800_001_000
            )

    def test_future_timestamp_is_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(
                make_init_data(auth_date=1_800_000_100),
                BOT_TOKEN,
                now=1_800_000_000,
            )

    def test_duplicate_fields_are_rejected(self):
        raw = make_init_data() + "&auth_date=1800000000"
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)

    def test_missing_user_is_rejected(self):
        values = {"auth_date": "1800000000"}
        check = "auth_date=1800000000"
        secret = hmac.new(BOT_TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
        values["hash"] = hmac.new(
            secret, check.encode(), hashlib.sha256
        ).hexdigest()
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(urlencode(values), BOT_TOKEN, now=1_800_000_100)

    def test_oversized_input_is_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data("x" * 16_385, BOT_TOKEN)


if __name__ == "__main__":
    unittest.main()
