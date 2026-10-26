CREATE TABLE worlds (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  purpose    TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
