CREATE TABLE events (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  id       TEXT NOT NULL UNIQUE,
  world_id TEXT NOT NULL,
  run_id   TEXT,
  ts       TEXT NOT NULL,
  type     TEXT NOT NULL,
  payload  TEXT NOT NULL
);
CREATE INDEX events_world_seq ON events (world_id, seq);
