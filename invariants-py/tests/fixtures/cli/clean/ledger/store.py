"""Raw storage. write_balance is the protected sink (L-1)."""


def read_balance(conn, account: str) -> int:
    return conn.execute("SELECT balance FROM accounts WHERE id = ?", (account,)).fetchone()[0]


def write_balance(conn, account: str, balance: int) -> None:
    conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (balance, account))


def append_entry(conn, account: str, amount: int) -> None:
    conn.execute("INSERT INTO entries (account, amount) VALUES (?, ?)", (account, amount))
