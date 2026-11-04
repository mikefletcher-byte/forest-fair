"""Tests for bearer-session authentication helper."""

import tempfile
import unittest
from pathlib import Path

from auth_api import get_bearer_session
from auth_store import AuthStore


class BearerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AuthStore(Path(self.temp_dir.name) / "auth.sqlite3")
        self.store.initialize()
        self.user_id, _ = self.store.get_or_create_user_for_identity("telegram", "123")
        self.session_id, self.token, self.expires_at = self.store.create_session(self.user_id)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_bearer_returns_session(self):
        session = get_bearer_session(self.store, "Bearer " + self.token)
        self.assertEqual(session["user_id"], self.user_id)
        self.assertEqual(session["session_id"], self.session_id)

    def test_scheme_is_case_insensitive(self):
        self.assertIsNotNone(get_bearer_session(self.store, "bearer " + self.token))

    def test_missing_or_invalid_header_is_rejected(self):
        for header in (None, "", self.token, "Basic " + self.token, "Bearer ", "Bearer a b"):
            with self.subTest(header=header):
                self.assertIsNone(get_bearer_session(self.store, header))

    def test_revoked_session_is_rejected(self):
        self.store.revoke_session(self.user_id, self.session_id)
        self.assertIsNone(get_bearer_session(self.store, "Bearer " + self.token))


if __name__ == "__main__":
    unittest.main()
