-- Web Push: one row per browser the operator turned notifications on in (issue 459).
CREATE TABLE push_subscriptions (
  id              TEXT PRIMARY KEY,
  endpoint        TEXT NOT NULL UNIQUE,
  p256dh          TEXT NOT NULL,
  auth            TEXT NOT NULL,
  label           TEXT NOT NULL,
  gates           INTEGER NOT NULL DEFAULT 1,
  failures        INTEGER NOT NULL DEFAULT 1,
  budget          INTEGER NOT NULL DEFAULT 1,
  notices         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  last_success_at TEXT
);
