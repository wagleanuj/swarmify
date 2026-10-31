-- Connector Packages a world enables, each at the version it pins (ADR 0023, issue 208). The
-- packages themselves are installed once for the whole install, on disk; world-scoped Setup
-- values live beside the shared ones, not here.
CREATE TABLE world_packages (
  world_id   TEXT NOT NULL,
  package    TEXT NOT NULL,
  version    TEXT NOT NULL,
  enabled_at TEXT NOT NULL,
  PRIMARY KEY (world_id, package)
);
