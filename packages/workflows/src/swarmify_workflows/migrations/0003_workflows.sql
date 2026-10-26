CREATE TABLE workflows (
  id              TEXT PRIMARY KEY,
  world_id        TEXT NOT NULL,
  name            TEXT NOT NULL,
  current_version INTEGER NOT NULL,
  created_at      TEXT NOT NULL
);
CREATE INDEX workflows_world ON workflows (world_id, created_at);

-- Versions are append-only: nothing updates or deletes a row while its workflow exists.
CREATE TABLE workflow_versions (
  id          TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
  number      INTEGER NOT NULL,
  yaml        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  UNIQUE (workflow_id, number)
);
