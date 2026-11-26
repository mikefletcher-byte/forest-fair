"""Telegram OpenID Connect Authorization Code + PKCE integration.

The Mini App initData flow remains separate. OIDC profile.id is the signed
Telegram numeric user ID; OIDC sub is stored as a second, distinct identity.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
import jwt
from aiohttp import web

from auth_store import AuthStore, IdentityAlreadyLinked, InactiveUser


ISSUER = "https://oauth.telegram.org"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = ISSUER + "/auth"
TOKEN_ENDPOINT = ISSUER + "/token"
JWKS_URI = ISSUER + "/.well-known/jwks.json"
ALLOWED_FRONTEND_HOSTS = {
    "forest-fair.ru",
    "www.forest-fair.ru",
    "mikefletcher-byte.github.io",
}


class TelegramOIDCError(ValueError):
    """Telegram OIDC response or configuration could not be trusted."""


class TelegramOIDCClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        frontend_url: str,
        session: aiohttp.ClientSession,
    ):
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.redirect_uri = str(redirect_uri or "").strip()
        self.frontend_url = str(frontend_url or "").strip()
        self.session = session
        self._metadata: dict | None = None
        self._metadata_loaded_at = 0
        self._jwks: dict | None = None
        self._jwks_loaded_at = 0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def _validate_configuration(self) -> None:
        if not self.configured:
            raise TelegramOIDCError("Telegram OIDC is not configured")
        parsed = urlsplit(self.redirect_uri)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.forest-fair.ru"
            or parsed.port != 8443
            or parsed.path != "/api/auth/telegram/oidc/callback"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise TelegramOIDCError("Telegram OIDC redirect URI is invalid")
        front = urlsplit(self.frontend_url)
        if (
            front.scheme != "https"
            or front.hostname not in ALLOWED_FRONTEND_HOSTS
            or front.username
            or front.password
            or front.port not in (None, 443)
            or front.query
            or front.fragment
        ):
            raise TelegramOIDCError("Telegram OIDC frontend URL is invalid")

    def build_authorization_url(self, auth_store: AuthStore) -> str:
        self._validate_configuration()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        auth_store.create_oidc_transaction(state, nonce, verifier)
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return AUTHORIZATION_ENDPOINT + "?" + urlencode(params)

    def frontend_redirect(self, *, ticket: str | None = None, error: str | None = None) -> str:
        self._validate_configuration()
        if bool(ticket) == bool(error):
            raise ValueError("exactly one frontend result is required")
        fragment = urlencode(
            {"telegram_oidc_ticket": ticket}
            if ticket
            else {"telegram_oidc_error": error}
        )
        parsed = urlsplit(self.frontend_url)
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, fragment)
        )

    async def _fetch_json(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict | None = None,
        auth: aiohttp.BasicAuth | None = None,
    ) -> dict:
        timeout = aiohttp.ClientTimeout(total=12, connect=5)
        try:
            async with self.session.request(
                method,
                url,
                data=data,
                auth=auth,
                allow_redirects=False,
                timeout=timeout,
                headers={"Accept": "application/json"},
            ) as response:
                if response.status != 200:
                    raise TelegramOIDCError("Telegram OIDC endpoint rejected the request")
                raw = await response.content.read(65_537)
                if len(raw) > 65_536:
                    raise TelegramOIDCError("Telegram OIDC response is too large")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TelegramOIDCError("Telegram OIDC endpoint is unavailable") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramOIDCError("Telegram OIDC returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise TelegramOIDCError("Telegram OIDC returned an invalid response")
        return value

    async def _get_metadata(self) -> dict:
        now = int(time.time())
        if self._metadata and now - self._metadata_loaded_at < 3600:
            return self._metadata
        metadata = await self._fetch_json(DISCOVERY_URL)
        # Pin the issuer and the provider endpoints documented by Telegram.
        if (
            metadata.get("issuer") != ISSUER
            or metadata.get("authorization_endpoint") != AUTHORIZATION_ENDPOINT
            or metadata.get("token_endpoint") != TOKEN_ENDPOINT
            or metadata.get("jwks_uri") != JWKS_URI
            or not ({"RS256", "ES256"} & set(metadata.get("id_token_signing_alg_values_supported", [])))
            or "S256" not in metadata.get("code_challenge_methods_supported", [])
            or "client_secret_basic" not in metadata.get("token_endpoint_auth_methods_supported", [])
        ):
            raise TelegramOIDCError("Telegram OIDC discovery metadata is not trusted")
        self._metadata = metadata
        self._metadata_loaded_at = now
        return metadata

    async def _get_jwks(self, *, force_refresh: bool = False) -> dict:
        now = int(time.time())
        if (
            not force_refresh
            and self._jwks
            and now - self._jwks_loaded_at < 3600
        ):
            return self._jwks
        metadata = await self._get_metadata()
        jwks = await self._fetch_json(metadata["jwks_uri"])
        if not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
            raise TelegramOIDCError("Telegram JWKS contains no signing keys")
        self._jwks = jwks
        self._jwks_loaded_at = now
        return jwks

    async def exchange_code(self, code: str, code_verifier: str) -> str:
        self._validate_configuration()
        if not isinstance(code, str) or not 1 <= len(code) <= 4096:
            raise TelegramOIDCError("invalid authorization code")
        await self._get_metadata()
        response = await self._fetch_json(
            TOKEN_ENDPOINT,
            method="POST",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": code_verifier,
            },
            auth=aiohttp.BasicAuth(self.client_id, self.client_secret),
        )
        id_token = response.get("id_token")
        if not isinstance(id_token, str) or not id_token or len(id_token) > 16_384:
            raise TelegramOIDCError("Telegram did not return a valid ID token")
        if str(response.get("token_type", "")).casefold() != "bearer":
            raise TelegramOIDCError("Telegram returned an unexpected token type")
        return id_token

    async def verify_id_token(self, id_token: str, expected_nonce_hash: str) -> dict:
        self._validate_configuration()
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise TelegramOIDCError("invalid Telegram ID token header") from exc
        algorithm = header.get("alg")
        if algorithm not in {"RS256", "ES256"}:
            raise TelegramOIDCError("unsupported Telegram ID token algorithm")
        expected_kty = "RSA" if algorithm == "RS256" else "EC"
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise TelegramOIDCError("Telegram ID token has no valid key id")
        metadata = await self._get_metadata()
        if algorithm not in metadata.get("id_token_signing_alg_values_supported", []):
            raise TelegramOIDCError("Telegram ID token algorithm is not advertised")

        jwks = await self._get_jwks()
        matches = [
            item for item in jwks["keys"]
            if isinstance(item, dict)
            and item.get("kid") == kid
            and item.get("kty") == expected_kty
            and item.get("use", "sig") == "sig"
            and item.get("alg", algorithm) == algorithm
            and (algorithm != "ES256" or item.get("crv") == "P-256")
        ]
        if not matches:
            jwks = await self._get_jwks(force_refresh=True)
            matches = [
                item for item in jwks["keys"]
                if isinstance(item, dict)
                and item.get("kid") == kid
                and item.get("kty") == expected_kty
                and item.get("use", "sig") == "sig"
                and item.get("alg", algorithm) == algorithm
                and (algorithm != "ES256" or item.get("crv") == "P-256")
            ]
        if len(matches) != 1:
            raise TelegramOIDCError("Telegram signing key was not found")

        try:
            signing_key = jwt.PyJWK.from_dict(matches[0], algorithm=algorithm).key
            claims = jwt.decode(
                id_token,
                signing_key,
                algorithms=[algorithm],
                audience=self.client_id,
                issuer=ISSUER,
                leeway=60,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub", "nonce"],
                },
            )
        except jwt.PyJWTError as exc:
            raise TelegramOIDCError("Telegram ID token validation failed") from exc

        now = int(time.time())
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or issued_at > now + 60
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= issued_at
        ):
            raise TelegramOIDCError("Telegram ID token timestamps are invalid")

        audience = claims.get("aud")
        authorized_party = claims.get("azp")
        if isinstance(audience, list) and len(audience) > 1:
            if authorized_party != self.client_id:
                raise TelegramOIDCError("Telegram ID token authorized party is invalid")
        if authorized_party is not None and authorized_party != self.client_id:
            raise TelegramOIDCError("Telegram ID token authorized party is invalid")

        nonce = claims.get("nonce")
        if not isinstance(nonce, str):
            raise TelegramOIDCError("Telegram ID token nonce is invalid")
        received_nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(received_nonce_hash, expected_nonce_hash):
            raise TelegramOIDCError("Telegram ID token nonce does not match")

        subject = claims.get("sub")
        telegram_id = claims.get("id")
        # Telegram profile.id is returned as a decimal string by its OIDC provider.
        # Normalize only bounded ASCII decimal strings; reject all other shapes below.
        if (
            isinstance(telegram_id, str)
            and telegram_id.isascii()
            and telegram_id.isdecimal()
            and len(telegram_id) <= 20
        ):
            telegram_id = int(telegram_id)
            claims["id"] = telegram_id
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > 255
            or isinstance(telegram_id, bool)
            or not isinstance(telegram_id, int)
            or telegram_id <= 0
        ):
            raise TelegramOIDCError("Telegram ID token identity claims are invalid")
        return claims


def _response_headers(request: web.Request) -> dict[str, str]:
    helper = request.app.get("cors_headers")
    headers = helper(request) if callable(helper) else {}
    return {**headers, "Cache-Control": "no-store"}


def _frontend_error(client: TelegramOIDCClient, code: str) -> None:
    try:
        location = client.frontend_redirect(error=code)
    except TelegramOIDCError as exc:
        raise web.HTTPServiceUnavailable(
            text="Telegram authentication is unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from exc
    raise web.HTTPFound(location)


async def handle_telegram_oidc_start(request: web.Request) -> web.Response:
    client: TelegramOIDCClient = request.app["telegram_oidc"]
    if not client.configured:
        return web.json_response(
            {"ok": False, "error": "telegram_auth_unavailable"},
            status=503,
            headers=_response_headers(request),
        )
    try:
        url = client.build_authorization_url(request.app["auth_store"])
        raise web.HTTPFound(url)
    except web.HTTPException:
        raise
    except (TelegramOIDCError, ValueError):
        logging.warning("Telegram OIDC start failed.")
        return web.json_response(
            {"ok": False, "error": "telegram_auth_unavailable"},
            status=503,
            headers=_response_headers(request),
        )


async def handle_telegram_oidc_callback(request: web.Request) -> web.Response:
    client: TelegramOIDCClient = request.app["telegram_oidc"]
    auth_store: AuthStore = request.app["auth_store"]
    if not client.configured:
        return web.json_response(
            {"ok": False, "error": "telegram_auth_unavailable"},
            status=503,
            headers=_response_headers(request),
        )

    try:
        state_values = request.query.getall("state", [])
        code_values = request.query.getall("code", [])
        error_values = request.query.getall("error", [])
        if len(state_values) != 1 or len(code_values) > 1 or len(error_values) > 1:
            raise TelegramOIDCError("invalid callback parameters")
        state = state_values[0]
        transaction = auth_store.consume_oidc_transaction(state)
        if not transaction:
            raise TelegramOIDCError("OIDC state is missing, expired or already used")
        if error_values:
            return _frontend_error(client, "access_denied")
        if len(code_values) != 1:
            raise TelegramOIDCError("authorization code is missing")

        id_token = await client.exchange_code(
            code_values[0], transaction["code_verifier"]
        )
        claims = await client.verify_id_token(id_token, transaction["nonce_hash"])
        user_id, _created = auth_store.get_or_create_user_for_telegram_oidc(
            claims["id"], claims["sub"]
        )
        ticket = auth_store.create_login_ticket(user_id)
        raise web.HTTPFound(client.frontend_redirect(ticket=ticket))
    except web.HTTPException:
        raise
    except IdentityAlreadyLinked:
        logging.warning("Telegram OIDC identity conflict; automatic account merge denied.")
        return _frontend_error(client, "identity_conflict")
    except InactiveUser:
        return _frontend_error(client, "account_unavailable")
    except (TelegramOIDCError, ValueError, KeyError, TypeError):
        logging.warning("Telegram OIDC callback rejected an invalid or expired response.")
        return _frontend_error(client, "login_failed")
    except Exception as exc:
        logging.error("Telegram OIDC callback failed (%s).", type(exc).__name__)
        return _frontend_error(client, "temporarily_unavailable")


async def handle_telegram_oidc_exchange(request: web.Request) -> web.Response:
    auth_store: AuthStore = request.app["auth_store"]
    try:
        if request.content_length is not None and request.content_length > 4096:
            raise ValueError("request is too large")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid request body")
        ticket = payload.get("ticket")
        session = auth_store.consume_login_ticket_and_create_session(
            ticket,
            device_label=request.headers.get("User-Agent", "")[:200] or None,
        )
        if not session:
            return web.json_response(
                {"ok": False, "error": "invalid_or_expired_login_ticket"},
                status=401,
                headers=_response_headers(request),
            )
        session_id, token, expires_at = session
        return web.json_response(
            {
                "ok": True,
                "token": token,
                "expires_at": expires_at,
                "user": {"user_id": auth_store.get_session(token)["user_id"]},
                "session_id": session_id,
            },
            headers=_response_headers(request),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "invalid_request"},
            status=400,
            headers=_response_headers(request),
        )
    except Exception as exc:
        logging.error("Telegram OIDC ticket exchange failed (%s).", type(exc).__name__)
        return web.json_response(
            {"ok": False, "error": "authentication_unavailable"},
            status=500,
            headers=_response_headers(request),
        )
