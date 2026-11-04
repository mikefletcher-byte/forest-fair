"""Tests for one-time email verification codes."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from auth_email import EmailCodeRateLimited, EmailCodeService
from auth_store import AuthStore


class FakeClock:
    def __init__(self, now=1_800_000_000):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class EmailCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "auth.sqlite3"
        schema_path = Path(__file__).with_name("auth_schema.sql")
        AuthStore(self.db_path, schema_path).initialize()
        self.clock = FakeClock()
        self.service = EmailCodeService(
            self.db_path,
            pepper="test-only-pepper-value-at-least-32-bytes",
            code_ttl_seconds=300,
            resend_cooldown_seconds=30,
            max_attempts=3,
            clock=self.clock,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_code_is_not_stored_in_plaintext_and_is_one_time(self):
        code = self.service.create_code(" Person@Example.com ")
        self.assertTrue(self.service.verify_code("person@example.com", code))
        self.assertFalse(self.service.verify_code("person@example.com", code))

        with sqlite3.connect(self.db_path) as conn:
            stored = conn.execute(
                "SELECT code_hash FROM email_codes"
            ).fetchone()[0]
        self.assertNotEqual(stored, code)
        self.assertEqual(len(stored), 64)

    def test_wrong_code_counts_attempts_and_locks_after_limit(self):
        code = self.service.create_code("person@example.com")
        wrong_codes = ["000000", "111111", "222222", "333333"]
        wrong_codes = [candidate for candidate in wrong_codes if candidate != code]
        for candidate in wrong_codes[:3]:
            self.assertFalse(self.service.verify_code("person@example.com", candidate))
        self.assertFalse(self.service.verify_code("person@example.com", code))

    def test_expired_code_is_rejected(self):
        code = self.service.create_code("person@example.com")
        self.clock.advance(301)
        self.assertFalse(self.service.verify_code("person@example.com", code))

    def test_resend_cooldown_blocks_new_code(self):
        self.service.create_code("person@example.com")
        with self.assertRaises(EmailCodeRateLimited):
            self.service.create_code("person@example.com")
        self.clock.advance(30)
        new_code = self.service.create_code("person@example.com")
        self.assertTrue(self.service.verify_code("person@example.com", new_code))

    def test_new_code_invalidates_previous_code(self):
        first = self.service.create_code("person@example.com")
        self.clock.advance(30)
        second = self.service.create_code("person@example.com")
        self.assertFalse(self.service.verify_code("person@example.com", first))
        self.assertTrue(self.service.verify_code("person@example.com", second))

    def test_link_code_is_bound_to_account(self):
        store = AuthStore(self.db_path)
        user_a, _ = store.get_or_create_user_for_identity("telegram", "111")
        user_b, _ = store.get_or_create_user_for_identity("telegram", "222")
        code = self.service.create_code("person@example.com", "link", user_a)

        self.assertFalse(
            self.service.verify_code("person@example.com", code, "link", user_b)
        )
        # A mismatched account attempt consumes an attempt, not the code.
        self.assertTrue(
            self.service.verify_code("person@example.com", code, "link", user_a)
        )

    def test_malformed_code_is_rejected_and_counts_attempt(self):
        code = self.service.create_code("person@example.com")
        self.assertFalse(self.service.verify_code("person@example.com", "12ab"))
        self.assertTrue(self.service.verify_code("person@example.com", code))

    def test_invalid_email_and_purpose_are_rejected(self):
        with self.assertRaises(ValueError):
            self.service.create_code("not-an-email")
        with self.assertRaises(ValueError):
            self.service.create_code("person@example.com", "password-reset")

    def test_link_code_requires_user_id(self):
        with self.assertRaises(ValueError):
            self.service.create_code("person@example.com", "link")


if __name__ == "__main__":
    unittest.main()
