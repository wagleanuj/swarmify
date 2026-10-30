-- The default Connector Packages each world has been given (ADR 0023, issue 213): a world gets a
-- default once, enabled at the version installed then, and keeps whatever it decides after,
-- disabling it included.
CREATE TABLE world_default_packages (
  world_id   TEXT NOT NULL,
  package    TEXT NOT NULL,
  offered_at TEXT NOT NULL,
  PRIMARY KEY (world_id, package)
);
