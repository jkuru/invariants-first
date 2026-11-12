"""An 'ops hotfix' module that skips the doors. inv scan must flag every function here."""
import sqlite3

from pool_service import store
from pool_service.service import ResizeService, ResizeRequest


def emergency_resize(conn: sqlite3.Connection, pool_id: str, new_size: int) -> None:
    """bypass: calls the sink directly, with no approval and no R-22 check."""
    store.set_size(conn, pool_id, new_size)


def hotfix_sql(conn: sqlite3.Connection, pool_id: str) -> None:
    """forbidden/raw-sql: writes pools.size with raw SQL."""
    conn.execute("UPDATE pools SET size = 999 WHERE id = ?", (pool_id,))


def unguarded_apply(svc: ResizeService, req: ResizeRequest) -> None:
    """forbidden/unwrap: calls the door without its guard."""
    ResizeService.resize_door.__wrapped__(svc, req)


def sneaky_resize(conn: sqlite3.Connection, pool_id: str) -> None:
    """forbidden/getattr-sink: hides the sink call from a name search."""
    getattr(store, "set_size")(conn, pool_id, 1)


def drop_guard_trigger(conn: sqlite3.Connection) -> None:
    """forbidden/raw-sql: drops the R-20 storage enforcer."""
    conn.execute("DROP TRIGGER r20_lease")


def overbook(conn: sqlite3.Connection, pool_id: str) -> None:
    """bypass (R-20): inserts a lease without the door."""
    store.insert_lease(conn, pool_id, "rogue", 1000)
