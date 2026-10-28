-- The model a world's sessions run on and its per-role overrides from the Cast (issue 79).
-- NULL falls back: role to world, world to the global default.
ALTER TABLE worlds ADD COLUMN model TEXT;
ALTER TABLE worlds ADD COLUMN planner_model TEXT;
ALTER TABLE worlds ADD COLUMN worker_model TEXT;
ALTER TABLE worlds ADD COLUMN judge_model TEXT;
ALTER TABLE worlds ADD COLUMN retro_model TEXT;
