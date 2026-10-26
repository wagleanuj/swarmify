-- The whiteboard of a swarm stage, after the beads task model (ADR 0017). `stage_id` and
-- `run_id` are ids from the Runs context, so there are no foreign keys across contexts.
CREATE TABLE tasks (
  id           TEXT PRIMARY KEY,
  world_id     TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  stage_id     TEXT NOT NULL,
  title        TEXT NOT NULL,
  body         TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL CHECK (status IN ('open', 'claimed', 'done')),
  owner        TEXT,
  added_by     TEXT NOT NULL,
  heartbeat_at TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  CHECK ((status = 'open') = (owner IS NULL)),
  CHECK ((status = 'claimed') = (heartbeat_at IS NOT NULL))
);
CREATE INDEX tasks_stage ON tasks (stage_id, created_at);
CREATE INDEX tasks_claimed ON tasks (status, heartbeat_at);
CREATE INDEX tasks_world ON tasks (world_id);

-- Beads' `blocks` dependency: a task is ready only when every task it depends on is done.
CREATE TABLE task_deps (
  task_id    TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
  depends_on TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, depends_on)
);

-- Notes an agent attached while holding a task. They stay when the task returns to open.
CREATE TABLE task_notes (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
  author  TEXT NOT NULL,
  body    TEXT NOT NULL,
  ts      TEXT NOT NULL
);
CREATE INDEX task_notes_task ON task_notes (task_id, seq);
