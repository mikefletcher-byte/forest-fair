"""Tests for Telegram WebApp initData verification."""

import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from telegram_auth import TelegramInitDataError, verify_telegram_init_data


BOT_TOKEN = "123456:TEST_TOKEN_FOR_UNIT_TESTS_ONLY"


def make_init_data(user=None, auth_date=1_800_000_000, token=BOT_TOKEN):
    values = {
        "auth_date": str(auth_date),
        "query_id": "AAE-test-query",
        "user": json.dumps(user or {"id": 12345, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(token.encode(), b"WebAppData", hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_data_returns_verified_user(self):
        raw = make_init_data()
        self.assertEqual(verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)["id"], 12345)

    def test_tampered_data_is_rejected(self):
        raw = make_init_data().replace("first_name", "last_name")
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)

    def test_wrong_bot_token_is_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(make_init_data(), "different-token", now=1_800_000_100)

    def test_expired_data_is_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(make_init_data(), BOT_TOKEN, now=1_800_001_000)

    def test_future_timestamp_is_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(make_init_data(auth_date=1_800_000_100), BOT_TOKEN, now=1_800_000_000)

    def test_duplicate_fields_are_rejected(self):
        raw = make_init_data() + "&auth_date=1800000000"
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(raw, BOT_TOKEN, now=1_800_000_100)

    def test_missing_user_is_rejected(self):
        values = {"auth_date": "1800000000"}
        check = "auth_date=1800000000"
        secret = hmac.new(BOT_TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data(urlencode(values), BOT_TOKEN, now=1_800_000_100)

    def test_oversized_input_is_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            verify_telegram_init_data("x" * 16_385, BOT_TOKEN)


if __name__ == "__main__":
    unittest.main()
