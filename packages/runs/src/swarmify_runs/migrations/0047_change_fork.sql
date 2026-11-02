-- A Restructure, and any Run Change to a run that has ended, continues as a Fork (ADR 0027): the
-- change records the run it made, which links both ways.
ALTER TABLE run_changes ADD COLUMN fork_run_id TEXT REFERENCES runs (id) ON DELETE SET NULL;
