-- Gates: the send-back counter, the parent link abort cascades along, and every decision.
ALTER TABLE runs ADD COLUMN send_backs INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN parent_run_id TEXT REFERENCES runs (id) ON DELETE CASCADE;
CREATE INDEX runs_parent ON runs (parent_run_id);

-- One row per gate decision; `path` is the decision markdown artifact in the stage folder.
CREATE TABLE gate_decisions (
  id           TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  stage_index  INTEGER NOT NULL,
  stage_name   TEXT NOT NULL,
  kind         TEXT NOT NULL,
  choice       TEXT,
  note         TEXT NOT NULL DEFAULT '',
  send_back_to TEXT,
  path         TEXT NOT NULL,
  decided_at   TEXT NOT NULL
);
CREATE INDEX gate_decisions_run ON gate_decisions (run_id, decided_at);
