"""Tests for the isolated Forest Fair SQLite auth store."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from auth_store import AuthStore, IdentityAlreadyLinked, InactiveUser


class AuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "auth.sqlite3"
        schema_path = Path(__file__).with_name("auth_schema.sql")
        self.store = AuthStore(self.db_path, schema_path)
        self.store.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def new_user(self, provider="telegram", provider_id="10001"):
        return self.store.get_or_create_user_for_identity(provider, provider_id)[0]

    def test_initialize_is_idempotent(self):
        self.store.initialize()
        with sqlite3.connect(self.db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(version, 1)
        self.assertTrue({"users", "auth_links", "sessions", "email_codes"} <= tables)

    def test_first_login_creates_account_and_repeat_reuses_it(self):
        first_id, created_first = self.store.get_or_create_user_for_identity(
            "telegram", "777"
        )
        second_id, created_second = self.store.get_or_create_user_for_identity(
            "telegram", "777"
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first_id, second_id)

    def test_email_identity_is_case_insensitive(self):
        first_id, created = self.store.get_or_create_user_for_identity(
            "email", "User@Example.COM "
        )
        second_id, created_again = self.store.get_or_create_user_for_identity(
            "EMAIL", "user@example.com"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first_id, second_id)

    def test_identity_cannot_be_linked_to_two_accounts(self):
        first_user = self.new_user("telegram", "111")
        second_user = self.new_user("telegram", "222")
        self.store.link_identity(first_user, "vk", "vk-identity-9")
        with self.assertRaises(IdentityAlreadyLinked):
            self.store.link_identity(second_user, "vk", "vk-identity-9")

        owner = self.store.get_user_for_identity("vk", "vk-identity-9")
        self.assertEqual(owner["user_id"], first_user)

    def test_linking_same_identity_to_same_account_is_idempotent(self):
        user_id = self.new_user()
        self.store.link_identity(user_id, "vk", "same-vk")
        self.store.link_identity(user_id, "vk", "same-vk")
        self.assertEqual(
            self.store.get_user_for_identity("vk", "same-vk")["user_id"],
            user_id,
        )

    def test_sessions_store_hash_not_raw_token(self):
        user_id = self.new_user()
        session_id, token, expires_at = self.store.create_session(user_id)
        self.assertTrue(token)
        self.assertGreater(expires_at, 0)

        with sqlite3.connect(self.db_path) as conn:
            stored_hash = conn.execute(
                "SELECT token_hash FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]
        self.assertNotEqual(stored_hash, token)
        self.assertEqual(len(stored_hash), 64)
        self.assertEqual(self.store.get_session(token)["user_id"], user_id)

    def test_sessions_can_be_revoked_individually(self):
        user_id = self.new_user()
        first_id, first_token, _ = self.store.create_session(user_id)
        second_id, second_token, _ = self.store.create_session(user_id)

        self.assertTrue(self.store.revoke_session(user_id, first_id))
        self.assertIsNone(self.store.get_session(first_token))
        self.assertIsNotNone(self.store.get_session(second_token))
        self.assertFalse(self.store.revoke_session(user_id, first_id))

    def test_sessions_can_all_be_revoked(self):
        user_id = self.new_user()
        _, first_token, _ = self.store.create_session(user_id)
        _, second_token, _ = self.store.create_session(user_id)

        self.assertEqual(self.store.revoke_all_sessions(user_id), 2)
        self.assertIsNone(self.store.get_session(first_token))
        self.assertIsNone(self.store.get_session(second_token))

    def test_session_listing_only_returns_live_sessions(self):
        user_id = self.new_user()
        session_id, _, _ = self.store.create_session(user_id, device_label="Desktop")
        self.store.create_session(user_id, device_label="Phone")
        self.store.revoke_session(user_id, session_id)

        sessions = self.store.list_sessions(user_id)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["device_label"], "Phone")

    def test_inactive_account_cannot_create_or_use_sessions(self):
        user_id = self.new_user()
        _, token, _ = self.store.create_session(user_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,)
            )

        self.assertIsNone(self.store.get_session(token))
        with self.assertRaises(InactiveUser):
            self.store.create_session(user_id)
        with self.assertRaises(InactiveUser):
            self.store.get_or_create_user_for_identity("telegram", "10001")

    def test_invalid_session_lifetime_is_rejected(self):
        user_id = self.new_user()
        with self.assertRaises(ValueError):
            self.store.create_session(user_id, lifetime_seconds=1)


if __name__ == "__main__":
    unittest.main()
