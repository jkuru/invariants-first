"""The door: resize checks approval (F-1) before it writes."""
import invariants as inv

from fleet import store


@inv.guard("F-1")
def resize(conn, pool_id: str, new_size: int, approved: bool) -> None:
    store.set_size(conn, pool_id, new_size)
