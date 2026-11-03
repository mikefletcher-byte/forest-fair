"""SQLite storage layer for Forest Fair authentication.

This module is intentionally independent from aiohttp, Telegram, and VK.
It contains no provider verification and must only receive identities that
have already been verified by the corresponding provider handler.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class AuthStoreError(Exception):
    """Base exception for authentication storage errors."""


class IdentityAlreadyLinked(AuthStoreError):
    """The provider identity is already linked to another Forest Fair user."""


class InactiveUser(AuthStoreError):
    """The account exists but is inactive."""


class AuthStore:
    def __init__(self, db_path: str | Path, schema_path: str | Path | None = None):
        self.db_path = Path(db_path)
        self.schema_path = (
            Path(schema_path)
            if schema_path is not None
            else Path(__file__).with_name("auth_schema.sql")
        )

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create tables and indexes. Safe to call more than once."""
        schema = self.schema_path.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(schema)

    @staticmethod
    def normalize_identity(provider: str, provider_id: str | int) -> tuple[str, str]:
        provider_name = str(provider).strip().lower()
        identity = str(provider_id).strip()
        if not provider_name or not identity:
            raise ValueError("provider and provider_id must not be empty")
        if provider_name == "email":
            identity = identity.casefold()
        if len(provider_name) > 32 or len(identity) > 255:
            raise ValueError("provider identity is too long")
        return provider_name, identity

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def get_user_for_identity(
        self, provider: str, provider_id: str | int
    ) -> dict[str, Any] | None:
        provider, provider_id = self.normalize_identity(provider, provider_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.user_id, u.created_at, u.updated_at, u.is_active
                FROM auth_links AS a
                JOIN users AS u ON u.user_id = a.user_id
                WHERE a.provider = ? AND a.provider_id = ?
                """,
                (provider, provider_id),
            ).fetchone()
        return dict(row) if row else None

    def get_or_create_user_for_identity(
        self, provider: str, provider_id: str | int
    ) -> tuple[int, bool]:
        """Return (user_id, created). Caller must verify provider identity first."""
        provider, provider_id = self.normalize_identity(provider, provider_id)
        now = int(time.time())

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT u.user_id, u.is_active
                FROM auth_links AS a
                JOIN users AS u ON u.user_id = a.user_id
                WHERE a.provider = ? AND a.provider_id = ?
                """,
                (provider, provider_id),
            ).fetchone()

            if existing:
                if not existing["is_active"]:
                    raise InactiveUser("Forest Fair account is inactive")
                conn.commit()
                return int(existing["user_id"]), False

            cursor = conn.execute(
                """
                INSERT INTO users (created_at, updated_at, is_active)
                VALUES (?, ?, 1)
                """,
                (now, now),
            )
            user_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO auth_links
                    (user_id, provider, provider_id, created_at, verified_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, provider, provider_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO auth_events (user_id, event_type, provider, created_at)
                VALUES (?, 'account_created', ?, ?)
                """,
                (user_id, provider, now),
            )
            conn.commit()
            return user_id, True

    def link_identity(
        self, user_id: int, provider: str, provider_id: str | int
    ) -> None:
        """Link a previously verified identity to an existing active user."""
        provider, provider_id = self.normalize_identity(provider, provider_id)
        now = int(time.time())

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute(
                "SELECT is_active FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not user or not user["is_active"]:
                raise InactiveUser("Forest Fair account is unavailable")

            existing = conn.execute(
                "SELECT user_id FROM auth_links WHERE provider = ? AND provider_id = ?",
                (provider, provider_id),
            ).fetchone()
            if existing:
                if int(existing["user_id"]) == int(user_id):
                    conn.commit()
                    return
                raise IdentityAlreadyLinked(
                    "This provider identity is already linked to another account"
                )

            conn.execute(
                """
                INSERT INTO auth_links
                    (user_id, provider, provider_id, created_at, verified_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, provider, provider_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO auth_events (user_id, event_type, provider, created_at)
                VALUES (?, 'identity_linked', ?, ?)
                """,
                (user_id, provider, now),
            )
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE user_id = ?",
                (now, user_id),
            )
            conn.commit()

    def create_session(
        self, user_id: int, lifetime_seconds: int = 30 * 24 * 60 * 60,
        device_label: str | None = None,
    ) -> tuple[int, str, int]:
        """Create a session; return (session_id, raw_token, expires_at).

        The raw token is returned once and must never be stored in the database
        or written to logs. Store only its hash.
        """
        if lifetime_seconds < 60:
            raise ValueError("session lifetime must be at least 60 seconds")
        now = int(time.time())
        expires_at = now + int(lifetime_seconds)
        token = secrets.token_urlsafe(32)
        token_hash = self._token_hash(token)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute(
                "SELECT is_active FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not user or not user["is_active"]:
                raise InactiveUser("Forest Fair account is unavailable")
            cursor = conn.execute(
                """
                INSERT INTO sessions
                    (user_id, token_hash, created_at, expires_at, last_used_at, device_label)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, token_hash, now, expires_at, now, device_label),
            )
            session_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO auth_events (user_id, event_type, created_at)
                VALUES (?, 'session_created', ?)
                """,
                (user_id, now),
            )
            conn.commit()
        return session_id, token, expires_at

    def get_session(self, token: str) -> dict[str, Any] | None:
        """Validate a raw session token and return its user/session metadata."""
        if not token:
            return None
        now = int(time.time())
        token_hash = self._token_hash(token)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT s.id AS session_id, s.user_id, s.created_at, s.expires_at,
                       s.device_label, u.is_active
                FROM sessions AS s
                JOIN users AS u ON u.user_id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL
                      AND s.expires_at > ? AND u.is_active = 1
                """,
                (token_hash, now),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE sessions SET last_used_at = ? WHERE id = ?",
                    (now, row["session_id"]),
                )
            conn.commit()
        return dict(row) if row else None

    def list_sessions(self, user_id: int) -> list[dict[str, Any]]:
        now = int(time.time())
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, expires_at, last_used_at, device_label
                FROM sessions
                WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
                ORDER BY last_used_at DESC
                """,
                (user_id, now),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_session(self, user_id: int, session_id: int) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions SET revoked_at = ?
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (now, session_id, user_id),
            )
            if cursor.rowcount:
                conn.execute(
                    """
                    INSERT INTO auth_events (user_id, event_type, created_at)
                    VALUES (?, 'session_revoked', ?)
                    """,
                    (user_id, now),
                )
        return cursor.rowcount > 0

    def revoke_all_sessions(self, user_id: int) -> int:
        now = int(time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (now, user_id),
            )
            conn.execute(
                """
                INSERT INTO auth_events (user_id, event_type, created_at)
                VALUES (?, 'all_sessions_revoked', ?)
                """,
                (user_id, now),
            )
        return cursor.rowcount

    def record_event(
        self, event_type: str, user_id: int | None = None,
        provider: str | None = None, details: dict[str, Any] | None = None,
    ) -> None:
        details_json = json.dumps(details, ensure_ascii=False) if details else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_events
                    (user_id, event_type, provider, created_at, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, event_type, provider, int(time.time()), details_json),
            )
