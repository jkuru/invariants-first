"""Operator scripts using idioms the scan forbids or cannot resolve."""
from fleet import service, store


def force_resize(conn, pool_id: str, n: int) -> None:
    getattr(store, "set_size")(conn, pool_id, n)              # forbidden: getattr on the sink name


def raw_resize(conn, pool_id: str, n: int) -> None:
    conn.execute("UPDATE pools SET size = ? WHERE id = ?", (n, pool_id))   # below the API: not a finding


def unguarded_resize(conn, pool_id: str, n: int) -> None:
    service.resize.__wrapped__(conn, pool_id, n, False)      # forbidden: the door without its guard


def dynamic(conn, name: str, *args) -> None:
    getattr(store, name)(conn, *args)                         # unresolved: computed attribute


HANDLERS = {"resize": store.set_size}                         # unresolved: the sink escapes as a value
