-- The one world an API token reaches, NULL for every world (Run Assistant spec, MCP).
ALTER TABLE api_tokens ADD COLUMN world_id TEXT;
