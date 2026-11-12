"""Entrypoints. Every one reaches the sink only through service.withdraw."""
from ledger import service as svc
from ledger.service import Ledger, withdraw


def handle_withdraw(conn, account: str, amount: int) -> None:
    withdraw(conn, account, amount)


def handle_pay(conn, account: str, amount: int) -> None:
    Ledger(conn).pay(account, amount)


def handle_deposit(conn, account: str, amount: int) -> None:
    svc.deposit(conn, account, amount)
