-- A pause or abort asked for while a stage runs ('pause' or 'abort'), so the stop check every
-- agent consults ends its session at the next step instead of when the stage ends. Written by
-- RunControl from any process; the run's workflow still writes the run's state.
ALTER TABLE runs ADD COLUMN stop TEXT;
