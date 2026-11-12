"""Where money moves. Nothing here is named like a repository."""
import sqlite3


def open_books(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS entries (account TEXT, cents INTEGER)")
    return conn


def post_charge(conn, account: str, cents: int) -> None:
    conn.execute("INSERT INTO entries (account, cents) VALUES (?, ?)", (account, -cents))


def post_credit(conn, account: str, cents: int) -> None:
    conn.execute("INSERT INTO entries (account, cents) VALUES (?, ?)", (account, cents))


def balance(conn, account: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(cents), 0) FROM entries WHERE account = ?", (account,)).fetchone()
    return int(row[0])
