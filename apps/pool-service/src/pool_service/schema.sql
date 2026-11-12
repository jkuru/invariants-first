-- pool-service schema. The triggers are the R-20 storage enforcer (view: rows).
-- invariants.yaml pins this file by sha256; re-run tests/test_lease.py::test_r20_door_and_trigger_agree
-- and `inv lock` after any change.
CREATE TABLE IF NOT EXISTS pools (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0)
);
CREATE TABLE IF NOT EXISTS leases (
    id      INTEGER PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES pools(id),
    holder  TEXT NOT NULL,
    slots   INTEGER NOT NULL CHECK (slots > 0)
);
CREATE TABLE IF NOT EXISTS resize_proposals (
    id          INTEGER PRIMARY KEY,
    pool_id     TEXT NOT NULL REFERENCES pools(id),
    new_size    INTEGER NOT NULL,
    proposed_by TEXT NOT NULL,
    approved_by TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'PENDING'
);
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    pool_id    TEXT NOT NULL REFERENCES pools(id),
    oom_killed INTEGER NOT NULL,
    elapsed_s  INTEGER NOT NULL,
    timeout_s  INTEGER NOT NULL,
    exit_code  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS remediations (
    id     INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    cause  TEXT NOT NULL,
    action TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS r20_lease BEFORE INSERT ON leases
WHEN (SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = NEW.pool_id) + NEW.slots
     > (SELECT size FROM pools WHERE id = NEW.pool_id)
BEGIN SELECT RAISE(ABORT, 'R-20: leases exceed pool size'); END;
CREATE TRIGGER IF NOT EXISTS r20_resize BEFORE UPDATE OF size ON pools
WHEN (SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = NEW.id) > NEW.size
BEGIN SELECT RAISE(ABORT, 'R-20: leases exceed pool size'); END;
