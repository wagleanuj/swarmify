-- World settings (T20). `monthly_budget_usd` NULL is no ceiling; `run_cap` is how many runs of
-- the world execute at once; `agent_count` is how many workers its cast has.
ALTER TABLE worlds ADD COLUMN monthly_budget_usd REAL;
ALTER TABLE worlds ADD COLUMN run_cap INTEGER NOT NULL DEFAULT 2;
ALTER TABLE worlds ADD COLUMN agent_count INTEGER NOT NULL DEFAULT 4;
