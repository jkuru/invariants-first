CREATE TABLE pools     (id TEXT PRIMARY KEY, size INTEGER NOT NULL CHECK (size >= 0));
CREATE TABLE leases    (id INTEGER PRIMARY KEY, pool_id TEXT NOT NULL REFERENCES pools(id),
                        n INTEGER NOT NULL CHECK (n > 0));
CREATE TABLE approvals (id INTEGER PRIMARY KEY, pool_id TEXT NOT NULL REFERENCES pools(id),
                        new_size INTEGER NOT NULL, state TEXT NOT NULL);
