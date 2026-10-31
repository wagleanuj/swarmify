-- What an API token reaches: `mcp` is the whole MCP server; `remote_approvals` only the server a
-- Remote Approval connector answers gates through, provisioned by the core for its process.
ALTER TABLE api_tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'mcp';
