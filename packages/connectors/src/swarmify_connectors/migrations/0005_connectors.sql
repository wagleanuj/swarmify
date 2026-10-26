-- Connectors a world added. Built-ins live in code. Credential values never land here, only names.
CREATE TABLE connectors (
  id          TEXT PRIMARY KEY,
  world_id    TEXT NOT NULL,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('tool', 'stage', 'publish')),
  command     TEXT NOT NULL,  -- JSON list of argv, empty for a URL connector
  url         TEXT,
  credentials TEXT NOT NULL,  -- JSON list of credential names
  stage_types TEXT NOT NULL,  -- JSON list
  created_at  TEXT NOT NULL,
  UNIQUE (world_id, name)
);

-- One row per connector stage call, keyed by the stage id (the idempotency key).
CREATE TABLE connector_calls (
  idempotency_key TEXT PRIMARY KEY,
  world_id        TEXT NOT NULL,
  run_id          TEXT NOT NULL,
  connector       TEXT NOT NULL,
  tool            TEXT NOT NULL,
  request         TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('running', 'polling', 'done', 'failed')),
  response        TEXT,
  error           TEXT,
  job_id          TEXT,
  poll_tool       TEXT,
  progress        REAL,
  message         TEXT,
  attempts        INTEGER NOT NULL,
  polls           INTEGER NOT NULL,
  started_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX connector_calls_world ON connector_calls (world_id);
