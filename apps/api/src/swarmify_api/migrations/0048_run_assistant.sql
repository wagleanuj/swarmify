-- The Run Assistant (spec Run Assistant): one conversation per run. Each message is the
-- operator's or the assistant's; an assistant turn keeps the session it resumes, its cost (the
-- world's monthly spend, never the run's Budget) and its source links. Proposals are the Run
-- Changes it proposed, as RunControl.propose returned them, with their state.
CREATE TABLE assistant_messages (
  id         TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  world_id   TEXT NOT NULL,
  role       TEXT NOT NULL,
  text       TEXT NOT NULL,
  sources    TEXT NOT NULL DEFAULT '[]',
  session_id TEXT,
  cost_usd   REAL NOT NULL DEFAULT 0,
  at         TEXT NOT NULL
);
CREATE INDEX assistant_messages_run ON assistant_messages (run_id, at);
CREATE INDEX assistant_messages_world ON assistant_messages (world_id, at);
CREATE TABLE assistant_proposals (
  id         TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  message_id TEXT REFERENCES assistant_messages (id) ON DELETE CASCADE,
  proposal   TEXT NOT NULL,
  state      TEXT NOT NULL DEFAULT 'open',
  change_id  TEXT,
  position   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX assistant_proposals_message ON assistant_proposals (message_id, position);
