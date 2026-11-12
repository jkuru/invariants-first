"""Example app for the FastAPI integration: an order API whose routes carry the core guard.

Guards name their target (py:...), so one guard covers every rule on that route."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict

import invariants as inv
from invariants.integrations import fastapi as inv_fastapi

UNIT_PRICE = 5


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    quantity: int
    kind: Literal["standard", "rush"] = "standard"


class Receipt(BaseModel):
    order_id: str
    sku: str
    quantity: int


class Quote(BaseModel):
    sku: str
    quantity: int
    total: int


STOCK = {"widget": 10, "gizmo": 500}
PLACED: list[Order] = []


@inv.provider("stock")
def load_stock(sku: str) -> int:
    if sku == "broken":
        raise RuntimeError("stock db unreachable at 10.0.0.7 (password=hunter2)")
    return STOCK.get(sku, 0)


router = APIRouter()


@router.post("/orders/{region}", operation_id="createOrder")
@inv_fastapi.guard("py:integ_fastapi_app.create_order")
def create_order(region: str, order: Order, dry_run: bool = False) -> Receipt:
    if not dry_run:
        PLACED.append(order)
    return Receipt(order_id=f"o-{len(PLACED)}", sku=order.sku, quantity=order.quantity)


@router.get("/quote/{sku}")
@inv_fastapi.guard("py:integ_fastapi_app.quote")
async def quote(sku: str, quantity: int) -> Quote:
    discount = 1 if quantity >= 50 else 0          # the bug R-Q1 catches: total != quantity * price
    return {"sku": sku, "quantity": quantity, "total": quantity * UNIT_PRICE - discount}  # type: ignore[return-value]


@router.get("/unguarded")
def unguarded() -> dict[str, str]:
    return {"ok": "yes"}


@inv.guard("py:integ_fastapi_app.double")
def double(n: int) -> int:
    """A plain guarded function, for the raw ASGI middleware tests."""
    return 2 * n


app = FastAPI()
app.include_router(router)
inv_fastapi.install(app)


def _order_rule(rid: str, when: str, msg: str, on_violation: str, **extra: object) -> dict[str, object]:
    return {"id": rid, "rev": 1, "status": "enforce", "class": "policy", "owner": "team-orders",
            "subject": {"contract": "orders_api", "operation": "createOrder",
                        "target": "py:integ_fastapi_app.create_order"},
            "bad": [{"when": when, "message": msg}],
            "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce",
                           "on_violation": on_violation, "on_error": "fail_closed"}],
            "falsified_by": ["tests/test_integrations_fastapi.py"], **extra}


RULES = {
    "spec": "invariant/v0.1",
    "contracts": {"orders_api": {"type": "openapi", "ref": "integ_fastapi_app:app"},
                  "quote_op": {"type": "openapi", "ref": "integ_fastapi_app:app", "operation": "GET /quote/{sku}"}},
    "targets": {"py:integ_fastapi_app.quote": {"effects": "none"}},
    "providers": {"stock": {"type": "int", "from": "py:integ_fastapi_app.load_stock",
                            "args": {"sku": "args.order.sku"}}},
    "rules": [
        _order_rule("R-H1", '!(args.region in ["eu", "us"])', "unknown region", "reject"),
        _order_rule("R-H2", "args.order.quantity > stock", "more than in stock", "reject"),
        _order_rule("R-H3", 'args.order.quantity > 100 && args.order.kind == "rush"',
                    "large rush orders need a person", "route"),
        {"id": "R-Q1", "rev": 1, "status": "enforce", "class": "law", "owner": "team-orders",
         "subject": {"contract": "quote_op", "target": "py:integ_fastapi_app.quote"},
         "bad": [{"when": "result.total != result.quantity * 5", "message": "total is not quantity x price"},
                 {"when": "result.sku != args.sku", "message": "quote is for another sku"}],
         "enforcers": [{"point": "decorator", "timing": "post_effect", "mode": "enforce",
                        "on_violation": "reject", "on_error": "fail_closed"}],
         "falsified_by": ["tests/test_integrations_fastapi.py::test_quote_post_effect_rejects_bad_total"]},
    ],
}
