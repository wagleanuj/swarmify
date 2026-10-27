-- Learning (ADR 0008): every version of a world's skills, with the run that produced it, and
-- every retro proposal with how it was decided. The current SKILL.md lives in the world folder.
CREATE TABLE skill_versions (
  id         TEXT PRIMARY KEY,
  world_id   TEXT NOT NULL,
  name       TEXT NOT NULL,
  number     INTEGER NOT NULL,
  content    TEXT NOT NULL,
  run_id     TEXT,
  note       TEXT NOT NULL DEFAULT '',
  retired    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (world_id, name, number)
);

-- `skill` and `workflow` are the proposal's two halves as JSON, either may be null.
CREATE TABLE skill_proposals (
  id         TEXT PRIMARY KEY,
  world_id   TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  stage_id   TEXT NOT NULL,
  skill      TEXT,
  workflow   TEXT,
  status     TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
  note       TEXT NOT NULL DEFAULT '',
  path       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE INDEX skill_proposals_run ON skill_proposals (run_id);
CREATE INDEX skill_proposals_world ON skill_proposals (world_id, created_at);
