-- Forks and promotes (Runs context): where a run came from. A fork copies its source up to
-- `fork_stage_index` and runs on from there. A promote starts at a stage with a Library artifact
-- (`promoted_from`, a path in the world folder) standing in for the skipped stages' output.
ALTER TABLE runs ADD COLUMN forked_from_run_id TEXT REFERENCES runs (id) ON DELETE SET NULL;
ALTER TABLE runs ADD COLUMN fork_stage_index INTEGER;
ALTER TABLE runs ADD COLUMN promoted_from TEXT;
CREATE INDEX runs_forked_from ON runs (forked_from_run_id);
