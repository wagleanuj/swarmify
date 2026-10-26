-- Child runs (ADR 0013): the run a workflow stage started. Cleared when the stage is sent back
-- to pending, so the next visit starts a fresh child.
ALTER TABLE run_stages ADD COLUMN child_run_id TEXT REFERENCES runs (id) ON DELETE SET NULL;
