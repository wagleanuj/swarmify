-- The one operator account (spec V2, Sign-in): the password's scrypt hash and the secret that
-- signs session cookies. Sessions and API tokens keep only a SHA-256 of their secret.
CREATE TABLE operator (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    secret TEXT NOT NULL
);

CREATE TABLE sessions (
    hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);

CREATE TABLE api_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
