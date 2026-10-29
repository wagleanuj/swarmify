-- Typed contracts (issue 133): the input types a connector declares, as JSON Schemas by name,
-- and which of a stage's inputs carries which type.
ALTER TABLE connectors ADD COLUMN types TEXT NOT NULL DEFAULT '{}';
ALTER TABLE connectors ADD COLUMN input_types TEXT NOT NULL DEFAULT '[]';
