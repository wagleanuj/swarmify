-- Edits to a world's Cast, append-only: version 1 of every agent is the built-in default and
-- is not stored, so the first edit is number 2. `tools` NULL means any tool the stage allows.
CREATE TABLE cast_edits (
  world_id   TEXT NOT NULL,
  name       TEXT NOT NULL,
  number     INTEGER NOT NULL,
  prompt     TEXT NOT NULL,
  tools      TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (world_id, name, number)
);
