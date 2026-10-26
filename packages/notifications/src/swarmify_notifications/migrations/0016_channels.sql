CREATE TABLE channels (
  world_id      TEXT PRIMARY KEY,
  slack_webhook TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
