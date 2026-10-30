-- The message each Remote Approval connector posted for a gate visit, as its opaque reference,
-- kept until the gate closes and the message is updated to say how.
CREATE TABLE remote_messages (
    run_id TEXT NOT NULL,
    stage_index INTEGER NOT NULL,
    visit INTEGER NOT NULL,
    world_id TEXT NOT NULL,
    package TEXT NOT NULL,
    message_ref TEXT NOT NULL
);
CREATE INDEX remote_messages_gate ON remote_messages (run_id, stage_index, visit);
