"""Tests for the isolated Forest Fair SQLite auth store."""

import hashlib
import sqlite3
import tempfile
import time
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

    def test_version_one_database_migrates_without_losing_users(self):
        user_id = self.new_user("telegram", "legacy-telegram-id")
        with self.store._connect() as conn:
            conn.execute("DROP TABLE auth_login_tickets")
            conn.execute("DROP TABLE oidc_transactions")
            conn.execute("PRAGMA user_version = 1")

        self.store.initialize()
        with self.store._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            user = conn.execute(
                "SELECT user_id, is_active FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(version, 2)
        self.assertEqual(user["user_id"], user_id)
        self.assertEqual(user["is_active"], 1)
        self.assertTrue({"oidc_transactions", "auth_login_tickets"} <= tables)

    def test_initialize_is_idempotent(self):
        self.store.initialize()
        conn = sqlite3.connect(self.db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        self.assertEqual(version, 2)
        self.assertTrue({"users", "auth_links", "sessions", "email_codes", "oidc_transactions", "auth_login_tickets"} <= tables)

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

        conn = sqlite3.connect(self.db_path)
        try:
            stored_hash = conn.execute(
                "SELECT token_hash FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]
        finally:
            conn.close()
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
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,)
            )
            conn.commit()
        finally:
            conn.close()

        self.assertIsNone(self.store.get_session(token))
        with self.assertRaises(InactiveUser):
            self.store.create_session(user_id)
        with self.assertRaises(InactiveUser):
            self.store.get_or_create_user_for_identity("telegram", "10001")

    def test_telegram_oidc_links_verified_sub_to_existing_mini_app_account(self):
        mini_app_user = self.new_user("telegram", "123456")
        resolved_user, created = self.store.get_or_create_user_for_telegram_oidc(
            123456, "opaque-oidc-subject"
        )
        self.assertFalse(created)
        self.assertEqual(resolved_user, mini_app_user)
        self.assertEqual(
            self.store.get_user_for_identity("telegram_oidc", "opaque-oidc-subject")["user_id"],
            mini_app_user,
        )

    def test_telegram_oidc_creates_both_verified_identity_links(self):
        user_id, created = self.store.get_or_create_user_for_telegram_oidc(
            987654, "subject-987654"
        )
        self.assertTrue(created)
        self.assertEqual(
            self.store.get_user_for_identity("telegram", "987654")["user_id"],
            user_id,
        )
        self.assertEqual(
            self.store.get_user_for_identity("telegram_oidc", "subject-987654")["user_id"],
            user_id,
        )

    def test_telegram_oidc_rejects_identity_conflict_without_merging_accounts(self):
        first_user = self.new_user("telegram", "111")
        second_user = self.new_user("telegram_oidc", "subject-222")
        with self.assertRaises(IdentityAlreadyLinked):
            self.store.get_or_create_user_for_telegram_oidc(111, "subject-222")
        self.assertEqual(
            self.store.get_user_for_identity("telegram", "111")["user_id"],
            first_user,
        )
        self.assertEqual(
            self.store.get_user_for_identity("telegram_oidc", "subject-222")["user_id"],
            second_user,
        )

    def test_oidc_state_is_hashed_and_can_only_be_consumed_once(self):
        state = "state-value-for-test-123456789"
        nonce = "nonce-value-for-test-123456789"
        verifier = "v" * 64
        self.store.create_oidc_transaction(state, nonce, verifier)
        transaction = self.store.consume_oidc_transaction(state)
        self.assertEqual(
            transaction["nonce_hash"],
            hashlib.sha256(nonce.encode()).hexdigest(),
        )
        self.assertEqual(transaction["code_verifier"], verifier)
        self.assertIsNone(self.store.consume_oidc_transaction(state))
        conn = sqlite3.connect(self.db_path)
        try:
            stored = conn.execute(
                "SELECT state_hash FROM oidc_transactions"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertNotEqual(stored, state)

    def test_expired_oidc_state_cannot_be_consumed(self):
        self.store.create_oidc_transaction(
            "state-expired-test-123456", "nonce-expired-test-123456", "v" * 64
        )
        self.assertIsNone(
            self.store.consume_oidc_transaction(
                "state-expired-test-123456", now=int(time.time()) + 601
            )
        )

    def test_login_ticket_is_one_time_and_creates_a_session(self):
        user_id = self.new_user()
        ticket = self.store.create_login_ticket(user_id)
        session = self.store.consume_login_ticket_and_create_session(ticket)
        self.assertIsNotNone(session)
        session_id, token, expires_at = session
        self.assertEqual(self.store.get_session(token)["user_id"], user_id)
        self.assertGreater(expires_at, int(time.time()))
        self.assertIsNone(
            self.store.consume_login_ticket_and_create_session(ticket)
        )

    def test_expired_login_ticket_cannot_create_a_session(self):
        user_id = self.new_user()
        ticket = self.store.create_login_ticket(user_id)
        self.assertIsNone(
            self.store.consume_login_ticket_and_create_session(
                ticket, now=int(time.time()) + 181
            )
        )

    def test_invalid_session_lifetime_is_rejected(self):
        user_id = self.new_user()
        with self.assertRaises(ValueError):
            self.store.create_session(user_id, lifetime_seconds=1)


if __name__ == "__main__":
    unittest.main()
