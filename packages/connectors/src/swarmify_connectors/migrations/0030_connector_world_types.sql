-- World-dependent types (issue 140): the tool a connector answers its types for one world with.
ALTER TABLE connectors ADD COLUMN world_types TEXT;
