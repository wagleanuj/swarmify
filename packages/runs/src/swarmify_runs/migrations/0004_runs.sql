CREATE TABLE runs (
  id               TEXT PRIMARY KEY,
  world_id         TEXT NOT NULL,
  workflow_id      TEXT NOT NULL,
  workflow_version INTEGER NOT NULL,
  name             TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN
                     ('queued', 'running', 'waiting_gate', 'paused', 'done', 'failed', 'aborted')),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX runs_world ON runs (world_id, created_at);

-- One row per stage of the pinned workflow version. `config` is the stage definition as JSON.
CREATE TABLE run_stages (
  id         TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  idx        INTEGER NOT NULL,
  name       TEXT NOT NULL,
  type       TEXT NOT NULL,
  config     TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN
               ('pending', 'running', 'waiting', 'done', 'failed', 'skipped')),
  started_at TEXT,
  ended_at   TEXT,
  error      TEXT,
  UNIQUE (run_id, idx)
);
