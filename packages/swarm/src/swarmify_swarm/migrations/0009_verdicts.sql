-- Planner and judge sessions are agents of the stage too. SQLite cannot widen a CHECK in place,
-- so agents is rebuilt; agent_log is copied aside first, since dropping agents would cascade.
CREATE TABLE agents_next (
  id         TEXT PRIMARY KEY,
  world_id   TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  stage_id   TEXT NOT NULL,
  name       TEXT NOT NULL,
  role       TEXT NOT NULL CHECK (role IN ('planner', 'worker', 'judge')),
  status     TEXT NOT NULL CHECK (status IN ('working', 'done', 'failed')),
  session_id TEXT,
  cost_usd   REAL NOT NULL DEFAULT 0,
  skills     TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (stage_id, name)
);
INSERT INTO agents_next SELECT * FROM agents;
CREATE TABLE agent_log_copy AS SELECT * FROM agent_log;
DROP TABLE agent_log;
DROP TABLE agents;
ALTER TABLE agents_next RENAME TO agents;
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
INSERT INTO agent_log SELECT * FROM agent_log_copy;
DROP TABLE agent_log_copy;
CREATE INDEX agent_log_stage ON agent_log (stage_id, seq);

-- The judge's verdict on each loop of a swarm stage. `items` keeps a pass's projection input.
CREATE TABLE verdicts (
  id         TEXT PRIMARY KEY,
  world_id   TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  stage_id   TEXT NOT NULL,
  loop       INTEGER NOT NULL,
  passed     INTEGER NOT NULL CHECK (passed IN (0, 1)),
  reasons    TEXT NOT NULL,
  gaps       TEXT NOT NULL,
  items      TEXT NOT NULL,
  path       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (stage_id, loop)
);
CREATE INDEX verdicts_world ON verdicts (world_id);
