-- Run Changes (ADR 0027): every edit applied to a live run, what it changed, who applied it and
-- when. A run's `revision` counts every stage status change, gate answer and applied change, so
-- a proposal made against an older one is refused.
ALTER TABLE runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE run_changes (
  id          TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  stage_id    TEXT NOT NULL,
  stage_index INTEGER NOT NULL,
  stage_name  TEXT NOT NULL,
  payload     TEXT NOT NULL,
  before_json TEXT NOT NULL,
  applied_by  TEXT NOT NULL,
  applied_at  TEXT NOT NULL,
  revision    INTEGER NOT NULL
);
CREATE INDEX run_changes_run ON run_changes (run_id, applied_at);
