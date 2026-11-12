"""Request handlers (wired to a web framework elsewhere)."""
from shop import checkout, ledger


def handle_buy(conn, body: dict) -> dict:
    checkout.place_order(conn, body["account"], body["cents"])
    return {"ok": True}


def handle_refund(conn, body: dict) -> dict:
    checkout.give_refund(conn, body["account"], body["cents"])
    return {"ok": True}


def handle_goodwill(conn, body: dict) -> dict:
    ledger.post_credit(conn, body["account"], body["cents"])        # skips checkout: a back door
    return {"ok": True}


def handle_balance(conn, account: str) -> dict:
    return {"balance": ledger.balance(conn, account)}
