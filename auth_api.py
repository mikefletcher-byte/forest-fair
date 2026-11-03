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



def attach_order_owner(data: dict, user_id: int) -> dict:
    """Copy client order data and overwrite ownership with the authenticated user."""
    if not isinstance(data, dict):
        raise ValueError("order payload must be an object")
    owned = dict(data)
    owned["user_id"] = int(user_id)
    return owned


def list_orders_for_user(orders: dict, user_id: int, limit: int = 15) -> list[dict]:
    """Return only explicitly owned orders, newest first; legacy orders stay hidden."""
    if not isinstance(orders, dict):
        return []
    owner_id = str(int(user_id))
    visible = []
    for record in reversed(list(orders.values())):
        if not isinstance(record, dict):
            continue
        if str(record.get("user_id", "")) == owner_id:
            visible.append(record)
            if len(visible) >= max(0, int(limit)):
                break
    return visible
