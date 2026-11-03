"""HTTP integration tests for the authenticated order endpoints."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import bot
from auth_store import AuthStore


class OrderRouteIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = AuthStore(root / "auth.sqlite3")
        self.store.initialize()

        self.user_a, _ = self.store.get_or_create_user_for_identity("telegram", "1001")
        self.user_b, _ = self.store.get_or_create_user_for_identity("telegram", "1002")
        self.session_a_id, self.token_a, _ = self.store.create_session(self.user_a)
        self.session_b_id, self.token_b, _ = self.store.create_session(self.user_b)

        self.orders_path = root / "orders.json"
        self.patches = [
            patch.object(bot, "ORDERS_FILE", str(self.orders_path)),
            patch.object(bot, "FILE_LOCK", asyncio.Lock()),
            patch.object(bot, "send_manager_alerts", new_callable=AsyncMock),
            patch.object(bot, "get_manager_order_kb", return_value=None),
        ]
        for item in self.patches:
            item.start()
        self.manager_alert = bot.send_manager_alerts

        app = web.Application(client_max_size=16_384)
        app["auth_store"] = self.store
        app["bot"] = object()
        app.router.add_post("/api/order", bot.handle_external_order)
        app.router.add_get("/api/orders", bot.handle_get_orders)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def order_payload(user_id=None):
        payload = {
            "customer_name": "Тестовый покупатель",
            "phone": "+79990000000",
            "address": "Томск, тестовый адрес",
            "delivery_method": "Самовывоз",
            "payment_method": "Наличными",
            "items": [{"title": "Тестовый товар", "grams": 50, "subtotal": 1000}],
            "total": 1000,
            "source": "HTTP integration test",
        }
        if user_id is not None:
            payload["user_id"] = user_id
        return payload

    async def test_post_without_session_returns_401_and_does_not_create_order(self):
        response = await self.client.post("/api/order", json=self.order_payload())
        self.assertEqual(response.status, 401)
        self.assertFalse(self.orders_path.exists())
        self.manager_alert.assert_not_awaited()

    async def test_post_uses_authenticated_owner_and_preserves_manager_alert(self):
        response = await self.client.post(
            "/api/order",
            json=self.order_payload(user_id=self.user_b),
            headers={"Authorization": "Bearer " + self.token_a},
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["order"]["user_id"], self.user_a)
        self.assertNotEqual(payload["order"]["user_id"], self.user_b)
        self.manager_alert.assert_awaited_once()
        notification = self.manager_alert.await_args.args[1]
        self.assertIn(payload["order"]["order_id"], notification)
        self.assertIn("НОВЫЙ ЗАКАЗ", notification)

        saved = json.loads(self.orders_path.read_text(encoding="utf-8"))
        self.assertEqual(saved[payload["order"]["order_id"]]["user_id"], self.user_a)

    async def test_get_requires_session_and_ignores_phone_or_ids_query(self):
        self.orders_path.write_text(
            json.dumps(
                {
                    "101": {"order_id": "101", "user_id": self.user_a, "phone": "+7001"},
                    "102": {"order_id": "102", "user_id": self.user_b, "phone": "+7002"},
                    "103": {"order_id": "103", "phone": "+7001"},
                }
            ),
            encoding="utf-8",
        )

        unauthorized = await self.client.get("/api/orders?phone=%2B7002&ids=102")
        self.assertEqual(unauthorized.status, 401)

        response = await self.client.get(
            "/api/orders?phone=%2B7002&ids=102",
            headers={"Authorization": "Bearer " + self.token_a},
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual([order["order_id"] for order in payload["orders"]], ["101"])

    async def test_revoked_session_cannot_create_or_read_orders(self):
        self.store.revoke_session(self.user_a, self.session_a_id)

        post_response = await self.client.post(
            "/api/order",
            json=self.order_payload(),
            headers={"Authorization": "Bearer " + self.token_a},
        )
        get_response = await self.client.get(
            "/api/orders",
            headers={"Authorization": "Bearer " + self.token_a},
        )

        self.assertEqual(post_response.status, 401)
        self.assertEqual(get_response.status, 401)
        self.assertFalse(self.orders_path.exists())
        self.manager_alert.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
