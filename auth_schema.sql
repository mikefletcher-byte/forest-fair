-- Forest Fair unified authentication schema
-- Schema version 2 (base unified auth plus Telegram OIDC tables).
-- Applied additively; this file does not modify or delete production records.
-- Timestamps are Unix seconds in UTC.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS auth_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 2 AND 32),
    provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 255),
    created_at INTEGER NOT NULL,
    verified_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    UNIQUE (provider, provider_id)
);

CREATE INDEX IF NOT EXISTS idx_auth_links_user_id
    ON auth_links(user_id);

-- One normalized, verified email address can belong to only one Forest Fair account.
-- The email address is stored as provider_id for provider='email'.

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    revoked_at INTEGER,
    device_label TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_active
    ON sessions(user_id, revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS email_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('login', 'link', 'replace')),
    user_id INTEGER,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0 AND attempts <= 10),
    consumed_at INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_email_codes_lookup
    ON email_codes(email, purpose, consumed_at, expires_at);

CREATE TABLE IF NOT EXISTS auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 64),
    provider TEXT,
    created_at INTEGER NOT NULL,
    details_json TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_events_user_time
    ON auth_events(user_id, created_at DESC);

-- Short-lived OIDC state, nonce and PKCE transactions. Raw state/nonce are never stored.
CREATE TABLE IF NOT EXISTS oidc_transactions (
    state_hash TEXT PRIMARY KEY,
    nonce_hash TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_oidc_transactions_expiry
    ON oidc_transactions(expires_at, consumed_at);

-- One-time handoff from the OIDC callback to the storefront browser.
-- Only the ticket hash is stored; the session is created at ticket exchange.
CREATE TABLE IF NOT EXISTS auth_login_tickets (
    ticket_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_auth_login_tickets_expiry
    ON auth_login_tickets(expires_at, consumed_at);

PRAGMA user_version = 2;
