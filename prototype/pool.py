"""Example targets for R-20 (leases never exceed pool size), R-21 (resize needs approval) and P-1, guide 6.2.
Guards read shared state through providers (D1); resize_door is transactional (D3)."""
import dataclasses, pathlib, sqlite3
import invariants as inv

SCHEMA = pathlib.Path(__file__).with_name("pool_schema.sql").read_text()
MARK = "-- triggers"
_AFTER_READ = []      # test seam: callbacks run right after pool_leased reads (forces the 2.7 interleaving)

def connect(triggers=True, schema=SCHEMA):
    conn = sqlite3.connect(":memory:", isolation_level=None)   # autocommit unless a txn is open
    conn.execute("PRAGMA foreign_keys = ON")
    tables, trig = schema.split(MARK)
    conn.executescript(tables + (MARK + trig if triggers else ""))
    return conn

def leased(conn, pool_id):
    return conn.execute("SELECT COALESCE(SUM(n), 0) FROM leases WHERE pool_id = ?", (pool_id,)).fetchone()[0]

def size(conn, pool_id):
    return conn.execute("SELECT size FROM pools WHERE id = ?", (pool_id,)).fetchone()[0]

def set_size(conn, pool_id, new_size):
    """pool_store.set_size: the raw effect. Only resize_door should call it (R-23, not modelled)."""
    conn.execute("UPDATE pools SET size = ? WHERE id = ?", (new_size, pool_id))

@inv.provider("pool_leased")
def pool_leased(conn: sqlite3.Connection, pool_id: str) -> int:
    v = leased(conn, pool_id)
    while _AFTER_READ: _AFTER_READ.pop(0)()
    return v

@inv.provider("pool_size")
def pool_size(conn: sqlite3.Connection, pool_id: str) -> int:
    return size(conn, pool_id)

@inv.guard("R-20")
def acquire(conn, pool_id: str, slots: int) -> None:
    """The lease door. leased and size come from providers (view), not from the caller (D1, D5)."""
    conn.execute("INSERT INTO leases (pool_id, n) VALUES (?, ?)", (pool_id, slots))

@dataclasses.dataclass
class ResizePoolRequest:
    pool_id: str
    new_size: int

@dataclasses.dataclass
class Approval:
    pool_id: str
    new_size: int
    state: str

def approve(conn, pool_id, new_size, state="APPROVED"):
    conn.execute("INSERT INTO approvals (pool_id, new_size, state) VALUES (?, ?, ?)", (pool_id, new_size, state))

@inv.provider("approval")
def load_approval(conn: sqlite3.Connection, pool_id: str) -> Approval | None:
    """The latest approval for the pool, read inside the write's transaction (consistency same_txn)."""
    row = conn.execute("SELECT pool_id, new_size, state FROM approvals WHERE pool_id = ? ORDER BY id DESC LIMIT 1",
                       (pool_id,)).fetchone()
    return Approval(*row) if row else None

class PoolService:
    def __init__(self, conn):
        self.conn = conn

    @property
    def sizes(self):
        """Plain data read from the database (inside the current transaction): P-1 snapshots old_self.sizes."""
        return {r[0]: r[1] for r in self.conn.execute("SELECT id, size FROM pools")}

    @inv.transaction
    def txn(self):
        """The declared transaction boundary for resize_door (targets: in invariants.json)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try: yield self
        except BaseException:
            self.conn.execute("ROLLBACK"); raise
        else: self.conn.execute("COMMIT")

    @inv.guard("py:pool.PoolService.resize_door")    # target form: every rule on this target (R-21, P-1)
    def resize_door(self, req: ResizePoolRequest) -> None:
        set_size(self.conn, req.pool_id, req.new_size)
