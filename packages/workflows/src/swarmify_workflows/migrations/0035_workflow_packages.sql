-- The Connector Package version each workflow version calls, as JSON `{package: version}`
-- (ADRs 0019, 0023, issue 223): its runs resolve those packages from here, never from the
-- world's current pin. Written on save; the only update is the stamp filling in a package a
-- version calls but did not record, as on upgrade.
ALTER TABLE workflow_versions ADD COLUMN packages TEXT NOT NULL DEFAULT '{}';
