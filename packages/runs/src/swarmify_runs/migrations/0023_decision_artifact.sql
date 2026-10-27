-- A decision may name the artifact it chose (a pick made from an idea card) or approved, as its
-- path in the world folder, so the record says which card, not only what it was called.
ALTER TABLE gate_decisions ADD COLUMN artifact TEXT;
