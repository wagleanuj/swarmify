-- At a gate before a publish stage the decision keeps what was confirmed (title, description and
-- the schedule resolved then, as JSON), which is what the publish stage posts.
ALTER TABLE gate_decisions ADD COLUMN publish TEXT;
