"""Security tests for authenticated order ownership and isolation."""

import tempfile
import unittest
from pathlib import Path

from auth_api import (
    attach_order_owner,
    get_bearer_session,
    list_orders_for_user,
)
from auth_store import AuthStore


class AuthenticatedOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AuthStore(Path(self.temp_dir.name) / "auth.sqlite3")
        self.store.initialize()
        self.user_a, _ = self.store.get_or_create_user_for_identity("telegram", "1001")
        self.user_b, _ = self.store.get_or_create_user_for_identity("telegram", "1002")
        self.session_a_id, self.token_a, _ = self.store.create_session(self.user_a)
        self.session_b_id, self.token_b, _ = self.store.create_session(self.user_b)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_post_without_session_is_rejected(self):
        for header in (None, "", "Bearer invalid"):
            with self.subTest(header=header):
                self.assertIsNone(get_bearer_session(self.store, header))

    def test_post_uses_session_owner_not_client_user_id(self):
        session = get_bearer_session(self.store, "Bearer " + self.token_a)
        self.assertIsNotNone(session)
        order = attach_order_owner(
            {"customer_name": "Buyer", "user_id": self.user_b},
            session["user_id"],
        )
        self.assertEqual(order["user_id"], self.user_a)

    def test_valid_session_can_create_owned_order_payload(self):
        session = get_bearer_session(self.store, "Bearer " + self.token_a)
        order = attach_order_owner({"total": 1200}, session["user_id"])
        self.assertEqual(order["user_id"], self.user_a)
        self.assertEqual(order["total"], 1200)

    def test_get_returns_only_current_users_orders(self):
        orders = {
            "101": {"order_id": "101", "user_id": self.user_a},
            "102": {"order_id": "102", "user_id": self.user_b},
            "103": {"order_id": "103", "user_id": self.user_a},
        }
        visible = list_orders_for_user(orders, self.user_a)
        self.assertEqual([o["order_id"] for o in visible], ["103", "101"])

    def test_user_a_cannot_see_user_b_orders(self):
        orders = {"102": {"order_id": "102", "user_id": self.user_b}}
        self.assertEqual(list_orders_for_user(orders, self.user_a), [])

    def test_legacy_order_without_owner_is_hidden(self):
        orders = {
            "101": {"order_id": "101", "phone": "+79990000000"},
            "102": {"order_id": "102", "user_id": self.user_a},
        }
        visible = list_orders_for_user(orders, self.user_a)
        self.assertEqual([o["order_id"] for o in visible], ["102"])

    def test_revoked_session_cannot_authorize_order_access(self):
        self.store.revoke_session(self.user_a, self.session_a_id)
        self.assertIsNone(
            get_bearer_session(self.store, "Bearer " + self.token_a)
        )


if __name__ == "__main__":
    unittest.main()
