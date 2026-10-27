-- Budget (ADR 0013): set on the top run and spent by it and every child run below it.
-- `spent_usd` is the estimated agent cost of the run and its children, refreshed by the cost
-- hook; `budget_warned_usd` is the budget whose 80 percent warning already went out, so a raised
-- budget warns again. `over_budget` marks the waiting stage as a budget gate.
ALTER TABLE runs ADD COLUMN budget_usd REAL;
ALTER TABLE runs ADD COLUMN spent_usd REAL NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN budget_warned_usd REAL;
ALTER TABLE runs ADD COLUMN over_budget INTEGER NOT NULL DEFAULT 0;
-- Baseline: one agent and no graph, to measure the swarm against.
ALTER TABLE runs ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0;
CREATE INDEX runs_baseline ON runs (workflow_id, baseline, created_at);
