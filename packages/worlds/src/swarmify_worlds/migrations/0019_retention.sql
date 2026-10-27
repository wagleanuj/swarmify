-- Days a world keeps run binaries (videos, images); 0 keeps them forever. Markdown stays always.
ALTER TABLE worlds ADD COLUMN retention_days INTEGER NOT NULL DEFAULT 90;
