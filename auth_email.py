"""Secure one-time email verification codes for Forest Fair.

The caller is responsible for actually delivering the returned code by email
and for linking the verified email to an account after successful verification.
Never log or persist the plaintext code or pepper.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path


class EmailCodeError(Exception):
    """Base class for email-code errors."""


class EmailCodeRateLimited(EmailCodeError):
    """A new code was requested before the resend cooldown elapsed."""


class EmailCodeService:
    PURPOSES = {"login", "link", "replace"}

    def __init__(
        self,
        db_path: str | Path,
        pepper: str | bytes,
        *,
        code_ttl_seconds: int = 600,
        resend_cooldown_seconds: int = 60,
        max_attempts: int = 5,
        clock=None,
    ):
        if isinstance(pepper, str):
            pepper = pepper.encode("utf-8")
        if not pepper or len(pepper) < 32:
            raise ValueError("email-code pepper must contain at least 32 bytes")
        if code_ttl_seconds < 60:
            raise ValueError("code TTL must be at least 60 seconds")
        if resend_cooldown_seconds < 1:
            raise ValueError("resend cooldown must be positive")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")

        self.db_path = Path(db_path)
        self._pepper = bytes(pepper)
        self.code_ttl_seconds = int(code_ttl_seconds)
        self.resend_cooldown_seconds = int(resend_cooldown_seconds)
        self.max_attempts = int(max_attempts)
        self._clock = clock or time.time

    @staticmethod
    def normalize_email(email: str) -> str:
        normalized = str(email).strip().casefold()
        if (
            not normalized
            or len(normalized) > 254
            or normalized.count("@") != 1
            or normalized.startswith("@")
            or normalized.endswith("@")
            or any(ch.isspace() for ch in normalized)
        ):
            raise ValueError("invalid email address")
        return normalized

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path), timeout=10, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _code_hash(self, email: str, purpose: str, code: str) -> str:
        message = f"{email}\n{purpose}\n{code}".encode("utf-8")
        return hmac.new(self._pepper, message, hashlib.sha256).hexdigest()

    def create_code(
        self, email: str, purpose: str = "login", user_id: int | None = None
    ) -> str:
        email = self.normalize_email(email)
        purpose = str(purpose).strip().lower()
        if purpose not in self.PURPOSES:
            raise ValueError("unsupported email-code purpose")
        if purpose in {"link", "replace"} and user_id is None:
            raise ValueError("user_id is required for link/replace codes")

        now = int(self._clock())
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = self._code_hash(email, purpose, code)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            recent = conn.execute(
                """
                SELECT created_at FROM email_codes
                WHERE email = ? AND purpose = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (email, purpose),
            ).fetchone()
            if recent and now - int(recent["created_at"]) < self.resend_cooldown_seconds:
                conn.rollback()
                raise EmailCodeRateLimited("please wait before requesting another code")

            # Invalidate all older unconsumed codes for this email and purpose.
            conn.execute(
                """
                UPDATE email_codes SET consumed_at = ?
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL
                """,
                (now, email, purpose),
            )
            conn.execute(
                """
                INSERT INTO email_codes
                    (email, code_hash, purpose, user_id, created_at, expires_at, attempts)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    email, code_hash, purpose, user_id, now,
                    now + self.code_ttl_seconds,
                ),
            )
            conn.commit()
        return code

    def verify_code(
        self,
        email: str,
        code: str,
        purpose: str = "login",
        user_id: int | None = None,
    ) -> bool:
        """Consume a valid code once. Invalid attempts count toward lockout."""
        email = self.normalize_email(email)
        purpose = str(purpose).strip().lower()
        if purpose not in self.PURPOSES:
            raise ValueError("unsupported email-code purpose")
        if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
            # Treat malformed input as a failed attempt against the active code.
            candidate_hash = self._code_hash(email, purpose, "invalid-input")
        else:
            candidate_hash = self._code_hash(email, purpose, code)

        now = int(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, code_hash, user_id, expires_at, attempts
                FROM email_codes
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (email, purpose),
            ).fetchone()

            if not row or int(row["expires_at"]) <= now:
                if row:
                    conn.execute(
                        "UPDATE email_codes SET consumed_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                conn.commit()
                return False

            if int(row["attempts"]) >= self.max_attempts:
                conn.execute(
                    "UPDATE email_codes SET consumed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                conn.commit()
                return False

            expected_hash = str(row["code_hash"])
            valid = hmac.compare_digest(candidate_hash, expected_hash)
            if user_id is not None and row["user_id"] != user_id:
                valid = False
            if purpose in {"link", "replace"} and user_id is None:
                valid = False

            if valid:
                conn.execute(
                    "UPDATE email_codes SET consumed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                conn.commit()
                return True

            new_attempts = int(row["attempts"]) + 1
            conn.execute(
                """
                UPDATE email_codes
                SET attempts = ?, consumed_at = ?
                WHERE id = ?
                """,
                (
                    new_attempts,
                    now if new_attempts >= self.max_attempts else None,
                    row["id"],
                ),
            )
            conn.commit()
            return False
