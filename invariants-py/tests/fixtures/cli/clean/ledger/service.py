"""The door: withdraw is the only path to store.write_balance."""
import invariants as inv

from . import store


@inv.guard("L-1")
def withdraw(conn, account: str, amount: int) -> None:
    _apply(conn, account, store.read_balance(conn, account) - amount)


def _apply(conn, account: str, new_balance: int) -> None:
    """A helper only the door calls, so the door still dominates the sink."""
    store.write_balance(conn, account, new_balance)


@inv.guard("L-2")
def deposit(conn, account: str, amount: int) -> None:
    store.append_entry(conn, account, amount)


class Ledger:
    def __init__(self, conn) -> None:
        self.conn = conn

    def pay(self, account: str, amount: int) -> None:
        withdraw(self.conn, account, amount)

    @inv.guard("L-3")
    def close(self, reason: str) -> None:
        self.conn.close()
