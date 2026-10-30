-- Who answered a gate from outside the web UI (a Remote Approval), as the connector names them,
-- such as "anuj in Slack". NULL for an answer given in the web UI or over the MCP server.
ALTER TABLE gate_decisions ADD COLUMN answered_by TEXT;
