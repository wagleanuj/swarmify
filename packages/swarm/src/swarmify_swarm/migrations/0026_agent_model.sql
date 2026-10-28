-- The model an agent's latest session ran on (issue 79). NULL for agents from before.
ALTER TABLE agents ADD COLUMN model TEXT;
