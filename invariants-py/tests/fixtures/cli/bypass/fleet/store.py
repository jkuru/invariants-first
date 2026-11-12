"""Raw storage. set_size is the protected sink (F-1)."""


def get_size(conn, pool_id: str) -> int:
    return conn.execute("SELECT size FROM pools WHERE id = ?", (pool_id,)).fetchone()[0]


def set_size(conn, pool_id: str, new_size: int) -> None:
    conn.execute("UPDATE pools SET size = ? WHERE id = ?", (new_size, pool_id))
