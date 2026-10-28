-- The visit to the stage a connector call was made in, the run's send-back count (issue 60):
-- a send-back that reruns the stage calls the connector again.
ALTER TABLE connector_calls ADD COLUMN visit INTEGER NOT NULL DEFAULT 0;
