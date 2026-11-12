CREATE TABLE pools     (id TEXT PRIMARY KEY, size INTEGER NOT NULL CHECK (size >= 0));
CREATE TABLE leases    (id INTEGER PRIMARY KEY, pool_id TEXT NOT NULL REFERENCES pools(id),
                        n INTEGER NOT NULL CHECK (n > 0));
CREATE TABLE approvals (id INTEGER PRIMARY KEY, pool_id TEXT NOT NULL REFERENCES pools(id),
                        new_size INTEGER NOT NULL, state TEXT NOT NULL);
-- triggers: R-20 storage enforcer (view: rows), hand-written; conformance test is demo.py differential_r20
CREATE TRIGGER r20_lease BEFORE INSERT ON leases
WHEN (SELECT COALESCE(SUM(n), 0) FROM leases WHERE pool_id = NEW.pool_id) + NEW.n > (SELECT size FROM pools WHERE id = NEW.pool_id)
BEGIN SELECT RAISE(ABORT, 'R-20: leases exceed pool size'); END;
CREATE TRIGGER r20_resize BEFORE UPDATE OF size ON pools
WHEN (SELECT COALESCE(SUM(n), 0) FROM leases WHERE pool_id = NEW.id) > NEW.size
BEGIN SELECT RAISE(ABORT, 'R-20: leases exceed pool size'); END;
