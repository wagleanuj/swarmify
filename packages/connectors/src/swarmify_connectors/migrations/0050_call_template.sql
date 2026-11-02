-- The Template a build stage's call started from, its name and fingerprint as JSON (issue 348).
ALTER TABLE connector_calls ADD COLUMN template TEXT;
