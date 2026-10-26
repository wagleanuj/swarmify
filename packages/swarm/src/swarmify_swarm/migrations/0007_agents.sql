-- The agents of a swarm stage and their live log. `session_id` is the Agent SDK session a
-- replacement agent resumes. Ids from Runs carry no foreign keys across contexts.
CREATE TABLE agents (
  id         TEXT PRIMARY KEY,
  world_id   TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  stage_id   TEXT NOT NULL,
  name       TEXT NOT NULL,
  role       TEXT NOT NULL CHECK (role IN ('worker')),
  status     TEXT NOT NULL CHECK (status IN ('working', 'done', 'failed')),
  session_id TEXT,
  cost_usd   REAL NOT NULL DEFAULT 0,
  skills     TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (stage_id, name)
);
CREATE INDEX agents_run ON agents (run_id);
CREATE INDEX agents_world ON agents (world_id);

CREATE TABLE agent_log (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
  stage_id TEXT NOT NULL,
  agent    TEXT NOT NULL,
  kind     TEXT NOT NULL CHECK (kind IN ('session', 'tool', 'denied', 'result', 'error')),
  tool     TEXT,
  input    TEXT,
  detail   TEXT NOT NULL DEFAULT '',
  ts       TEXT NOT NULL
);
CREATE INDEX agent_log_stage ON agent_log (stage_id, seq);
