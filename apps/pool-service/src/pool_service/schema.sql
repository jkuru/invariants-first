-- pool-service schema. The triggers are the store's own over-lease check, for concurrent writers. They
-- are below the API: no rule names them, and the R-20 check on lease_slots does not depend on them.
-- Their names and message say "db constraint", not a rule id, so a refusal is never read as a rule's.
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
DROP TRIGGER IF EXISTS r20_lease;          -- the earlier names, which reused the rule id
DROP TRIGGER IF EXISTS r20_resize;
CREATE TRIGGER IF NOT EXISTS pool_lease_capacity BEFORE INSERT ON leases
WHEN (SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = NEW.pool_id) + NEW.slots
     > (SELECT size FROM pools WHERE id = NEW.pool_id)
BEGIN SELECT RAISE(ABORT, 'pool lease capacity exceeded (db constraint)'); END;
CREATE TRIGGER IF NOT EXISTS pool_resize_capacity BEFORE UPDATE OF size ON pools
WHEN (SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = NEW.id) > NEW.size
BEGIN SELECT RAISE(ABORT, 'pool lease capacity exceeded (db constraint)'); END;
