"""The business layer: every money movement is checked here first."""
from shop import ledger

MAX_ORDER = 50_000


def place_order(conn, account: str, cents: int) -> None:
    if cents <= 0 or cents > MAX_ORDER:
        raise ValueError("order amount out of range")
    ledger.post_charge(conn, account, cents)


def give_refund(conn, account: str, cents: int) -> None:
    if cents <= 0 or cents > ledger.balance(conn, account) * -1:
        raise ValueError("refund above what was charged")
    ledger.post_credit(conn, account, cents)
