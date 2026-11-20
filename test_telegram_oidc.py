"""Security tests for Telegram OIDC token validation and redirect construction."""

import base64
import hashlib
import tempfile
import time
import unittest
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from auth_store import AuthStore
from telegram_oidc import (
    AUTHORIZATION_ENDPOINT,
    ISSUER,
    JWKS_URI,
    TelegramOIDCClient,
    TelegramOIDCError,
    handle_telegram_oidc_callback,
    handle_telegram_oidc_exchange,
)


def b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class TelegramOIDCTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_numbers = cls.private_key.public_key().public_numbers()
        cls.jwk = {
            "kty": "RSA",
            "kid": "test-key-1",
            "use": "sig",
            "alg": "RS256",
            "n": b64url_uint(public_numbers.n),
            "e": b64url_uint(public_numbers.e),
        }
        cls.ec_private_key = ec.generate_private_key(ec.SECP256R1())
        ec_numbers = cls.ec_private_key.public_key().public_numbers()
        cls.ec_jwk = {
            "kty": "EC",
            "kid": "test-ec-key-1",
            "use": "sig",
            "alg": "ES256",
            "crv": "P-256",
            "x": b64url_uint(ec_numbers.x),
            "y": b64url_uint(ec_numbers.y),
        }

    def setUp(self):
        self.client = TelegramOIDCClient(
            client_id="123456789",
            client_secret="test-client-secret",
            redirect_uri=(
                "https://api.forest-fair.ru:8443/api/auth/telegram/oidc/callback"
            ),
            frontend_url="https://forest-fair.ru/",
            session=object(),
        )
        now = int(time.time())
        self.client._metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": ISSUER + "/token",
            "jwks_uri": JWKS_URI,
            "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            "code_challenge_methods_supported": ["plain", "S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        }
        self.client._metadata_loaded_at = now
        self.client._jwks = {"keys": [self.jwk, self.ec_jwk]}
        self.client._jwks_loaded_at = now
        self.nonce = "nonce-value-for-oidc-test-123456"
        self.nonce_hash = hashlib.sha256(self.nonce.encode()).hexdigest()

    def make_token(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "123456789",
            "sub": "telegram-oidc-subject-abc",
            "id": 987654321,
            "iat": now - 10,
            "exp": now + 300,
            "nonce": self.nonce,
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key-1"},
        )

    async def test_accepts_valid_signed_id_token(self):
        claims = await self.client.verify_id_token(
            self.make_token(), self.nonce_hash
        )
        self.assertEqual(claims["sub"], "telegram-oidc-subject-abc")
        self.assertEqual(claims["id"], 987654321)

    async def test_accepts_valid_es256_signed_id_token(self):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "123456789",
            "sub": "telegram-oidc-es256-subject",
            "id": 987654321,
            "iat": now - 10,
            "exp": now + 300,
            "nonce": self.nonce,
        }
        token = jwt.encode(
            claims,
            self.ec_private_key,
            algorithm="ES256",
            headers={"kid": "test-ec-key-1"},
        )
        verified = await self.client.verify_id_token(token, self.nonce_hash)
        self.assertEqual(verified["sub"], "telegram-oidc-es256-subject")

    async def test_rejects_wrong_audience(self):
        with self.assertRaises(TelegramOIDCError):
            await self.client.verify_id_token(
                self.make_token(aud="another-client"), self.nonce_hash
            )

    async def test_rejects_wrong_issuer(self):
        with self.assertRaises(TelegramOIDCError):
            await self.client.verify_id_token(
                self.make_token(iss="https://attacker.example"), self.nonce_hash
            )

    async def test_rejects_wrong_nonce(self):
        wrong_hash = hashlib.sha256(b"another-nonce").hexdigest()
        with self.assertRaises(TelegramOIDCError):
            await self.client.verify_id_token(self.make_token(), wrong_hash)

    async def test_rejects_expired_token(self):
        now = int(time.time())
        with self.assertRaises(TelegramOIDCError):
            await self.client.verify_id_token(
                self.make_token(iat=now - 500, exp=now - 300), self.nonce_hash
            )

    async def test_rejects_unsupported_algorithm(self):
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": "123456789",
                "sub": "subject",
                "id": 987654321,
                "iat": int(time.time()) - 1,
                "exp": int(time.time()) + 60,
                "nonce": self.nonce,
            },
            "not-a-real-provider-key-with-32-bytes!!",
            algorithm="HS256",
        )
        with self.assertRaises(TelegramOIDCError):
            await self.client.verify_id_token(token, self.nonce_hash)

    async def test_rejects_non_numeric_telegram_profile_id(self):
        with self.assertRaises(TelegramOIDCError):
            await self.client.verify_id_token(
                self.make_token(id="987654321"), self.nonce_hash
            )

    async def test_rejects_ambiguous_multiple_audiences_without_azp(self):
        with self.assertRaises(TelegramOIDCError):
            await self.client.verify_id_token(
                self.make_token(aud=["123456789", "another-client"]),
                self.nonce_hash,
            )

    async def test_authorization_url_uses_state_nonce_and_pkce_s256(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AuthStore(Path(tmp) / "auth.sqlite3")
            store.initialize()
            url = self.client.build_authorization_url(store)
            parsed = urlsplit(url)
            params = parse_qs(parsed.query)
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.netloc, "oauth.telegram.org")
            self.assertEqual(parsed.path, "/auth")
            self.assertEqual(params["response_type"], ["code"])
            self.assertEqual(params["scope"], ["openid profile"])
            self.assertEqual(params["code_challenge_method"], ["S256"])
            transaction = store.consume_oidc_transaction(params["state"][0])
            self.assertIsNotNone(transaction)
            expected = base64.urlsafe_b64encode(
                hashlib.sha256(
                    transaction["code_verifier"].encode("ascii")
                ).digest()
            ).decode("ascii").rstrip("=")
            self.assertEqual(params["code_challenge"], [expected])

    async def test_frontend_handoff_ticket_is_in_fragment_not_query(self):
        url = self.client.frontend_redirect(ticket="one-time-ticket")
        parsed = urlsplit(url)
        self.assertEqual(parsed.query, "")
        self.assertEqual(
            parse_qs(parsed.fragment),
            {"telegram_oidc_ticket": ["one-time-ticket"]},
        )

    async def test_rejects_unapproved_frontend_host(self):
        self.client.frontend_url = "https://attacker.example/"
        with self.assertRaises(TelegramOIDCError):
            self.client.frontend_redirect(ticket="ticket")



class FakeTelegramOIDCClient:
    configured = True

    def __init__(self):
        self.exchanged_code = None
        self.received_verifier = None

    async def exchange_code(self, code, code_verifier):
        self.exchanged_code = code
        self.received_verifier = code_verifier
        return "verified-id-token"

    async def verify_id_token(self, id_token, expected_nonce_hash):
        if id_token != "verified-id-token" or not expected_nonce_hash:
            raise TelegramOIDCError("invalid mocked token")
        return {"id": 7654321, "sub": "oidc-subject-7654321"}

    def frontend_redirect(self, *, ticket=None, error=None):
        values = (
            {"telegram_oidc_ticket": ticket}
            if ticket
            else {"telegram_oidc_error": error}
        )
        return "https://forest-fair.ru/#" + urlencode(values)


class TelegramOIDCHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AuthStore(Path(self.temp_dir.name) / "auth.sqlite3")
        self.store.initialize()
        self.fake_client = FakeTelegramOIDCClient()
        app = web.Application()
        app["auth_store"] = self.store
        app["telegram_oidc"] = self.fake_client
        app["cors_headers"] = lambda _request: {}
        app.router.add_get(
            "/api/auth/telegram/oidc/callback", handle_telegram_oidc_callback
        )
        app.router.add_post(
            "/api/auth/telegram/oidc/exchange", handle_telegram_oidc_exchange
        )
        self.http = TestClient(TestServer(app))
        await self.http.start_server()

    async def asyncTearDown(self):
        await self.http.close()
        self.temp_dir.cleanup()

    async def test_callback_and_exchange_issue_one_shared_bearer_session(self):
        state = "state-handler-test-123456789"
        nonce = "nonce-handler-test-123456789"
        verifier = "v" * 64
        self.store.create_oidc_transaction(state, nonce, verifier)

        callback = await self.http.get(
            "/api/auth/telegram/oidc/callback?code=one-time-code&state=" + state,
            allow_redirects=False,
        )
        self.assertEqual(callback.status, 302)
        self.assertEqual(self.fake_client.exchanged_code, "one-time-code")
        self.assertEqual(self.fake_client.received_verifier, verifier)

        fragment = urlsplit(callback.headers["Location"]).fragment
        ticket = parse_qs(fragment)["telegram_oidc_ticket"][0]
        exchanged = await self.http.post(
            "/api/auth/telegram/oidc/exchange", json={"ticket": ticket}
        )
        self.assertEqual(exchanged.status, 200)
        payload = await exchanged.json()
        self.assertTrue(payload["ok"])
        session = self.store.get_session(payload["token"])
        self.assertEqual(session["user_id"], payload["user"]["user_id"])
        self.assertEqual(
            self.store.get_user_for_identity("telegram", "7654321")["user_id"],
            session["user_id"],
        )
        self.assertEqual(
            self.store.get_user_for_identity("telegram_oidc", "oidc-subject-7654321")["user_id"],
            session["user_id"],
        )

        replay = await self.http.post(
            "/api/auth/telegram/oidc/exchange", json={"ticket": ticket}
        )
        self.assertEqual(replay.status, 401)

    async def test_callback_state_cannot_be_replayed(self):
        state = "state-replay-test-123456789"
        nonce = "nonce-replay-test-123456789"
        self.store.create_oidc_transaction(state, nonce, "v" * 64)
        path = "/api/auth/telegram/oidc/callback?code=code&state=" + state

        first = await self.http.get(path, allow_redirects=False)
        second = await self.http.get(path, allow_redirects=False)
        self.assertEqual(first.status, 302)
        self.assertEqual(second.status, 302)
        error = parse_qs(urlsplit(second.headers["Location"]).fragment)
        self.assertEqual(error["telegram_oidc_error"], ["login_failed"])


if __name__ == "__main__":
    unittest.main()
