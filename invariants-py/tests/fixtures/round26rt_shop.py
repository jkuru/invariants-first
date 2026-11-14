"""Fixture app for the fix-round-26 runtime tests: FastAPI handlers that return a Response."""
from __future__ import annotations

from typing import Any

import pydantic
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from invariants.integrations import fastapi as inv_fastapi

M = "py:round26rt_shop"


class Receipt(pydantic.BaseModel):
    total: int


app = FastAPI()


@app.get("/orders/{oid}")
@inv_fastapi.guard(f"{M}.get_order")
def get_order(oid: str) -> Receipt:
    if oid == "missing":
        return JSONResponse({"detail": "not found"}, status_code=404)    # type: ignore[return-value]
    return Receipt(total=500 if oid == "big" else 5)


@app.get("/aorders/{oid}")
@inv_fastapi.guard(f"{M}.aget_order", response_model=Receipt)
async def aget_order(oid: str) -> Any:
    if oid == "missing":
        return JSONResponse({"detail": "not found"}, status_code=404)
    return {"total": 500 if oid == "big" else 5}


def doc(bad: str, on_error: str = "fail_closed", timing: str = "pre_effect") -> dict[str, Any]:
    rules = []
    for fn in ("get_order", "aget_order"):
        rules.append({"id": f"SH-{fn}", "status": "enforce", "subject": {"target": f"{M}.{fn}"}, "bad": bad,
                      "enforcers": [{"point": "decorator", "timing": timing, "mode": "enforce",
                                     "on_error": on_error}],
                      "falsified_by": ["tests/test_fix_round26_runtime.py::test_shop"]})
    return {"targets": {f"{M}.get_order": {"effects": "none"}, f"{M}.aget_order": {"effects": "none"}},
            "rules": rules}
