"""Small, framework-independent helpers for authenticated API requests."""

from __future__ import annotations

from auth_store import AuthStore


def get_bearer_session(auth_store: AuthStore, authorization: str | None):
    """Return an active session for a strict Bearer header, otherwise None."""
    if not isinstance(authorization, str):
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return None
    token = token.strip()
    if not token or any(ch.isspace() for ch in token):
        return None
    return auth_store.get_session(token)
