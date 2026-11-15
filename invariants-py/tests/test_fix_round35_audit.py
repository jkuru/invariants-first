"""Fix round 35: an external audit of the CEL engine. Most items are in
packages/invariants-cel/tests/test_fix_round35_cel.py; this file has the pydantic ones (that package's
tests do not install pydantic).

M2. A pydantic model with extra="allow" read whole (size(), ==, in, the macros) missed its extra
    fields, which a rule reads by name and model_dump() has; a plain @property was readable by name
    but missing from the model read whole, so `o.twice == 2 && size(o) == 2` was a rule_error on
    cel-expr. Both are fields of the record read whole now, on both backends; pydantic's BaseModel API
    properties (model_extra, model_fields_set) never are.
M6. The type checker read a pydantic model's annotations only, so a @computed_field returning a type
    the mapping refuses (Decimal, a naive datetime) passed at load and was a rule_error on every call.
    Its return type is now its type: reading it, or the model whole, is refused at load.
"""
from __future__ import annotations

import datetime
import decimal

import pytest

from invariants_cel import CelError, available_backends, get_backend, typecheck

pydantic = pytest.importorskip("pydantic")
BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]


class Extra(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")
    a: int = 1


class WithProperty(pydantic.BaseModel):
    a: int = 1

    @property
    def twice(self) -> int:
        return 2 * self.a


def _eval(backend: str, expr: str, env: dict) -> object:
    try:
        return get_backend(backend).compile(expr).evaluate(env)
    except CelError as e:
        return f"rule_error: {e}"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr", [
    "size(o) == 2", '"z" in o', 'o == {"a": 1, "z": 3}', 'o.all(k, k in ["a", "z"])', "o.z == 3 && size(o) == 2",
])
def test_extra_fields_are_fields_of_the_model_read_whole(backend, expr):
    o = Extra(a=1, z=3)
    assert o.model_dump() == {"a": 1, "z": 3}
    assert _eval(backend, expr, {"o": o}) is True


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr", ["o.twice == 2 && size(o) == 2", 'o == {"a": 1, "twice": 2}',
                                  '!("model_extra" in o) && !("model_fields_set" in o)'])
def test_a_property_is_a_field_of_the_model_read_whole(backend, expr):
    assert _eval(backend, expr, {"o": WithProperty()}) is True


# ---- M6. computed fields in the type checker ----------------------------------------------------------

class Priced(pydantic.BaseModel):
    qty: int = 2

    @pydantic.computed_field         # type: ignore[prop-decorator]
    @property
    def price(self) -> decimal.Decimal:
        return decimal.Decimal("1.5")


class Timed(pydantic.BaseModel):
    qty: int = 2

    @pydantic.computed_field         # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return 2 * self.qty

    @pydantic.computed_field         # type: ignore[prop-decorator]
    @property
    def at(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)


@pytest.mark.parametrize("expr,msg", [
    ("o.price > 1", "o.price cannot be read by a rule: Decimal"),
    ("o == o", "it reads o whole, and o.price cannot be read by a rule: Decimal"),
    ("size(o) == 2", "o.price cannot be read by a rule"),
])
def test_a_refused_computed_field_is_refused_at_load(expr, msg):
    with pytest.raises(typecheck.TypeMismatch, match=msg):
        typecheck.check_clause(expr, {"o": typecheck.from_py(Priced)})


@pytest.mark.parametrize("expr,msg", [
    ('o.total == "x"', "no overload for =="), ("o.at > 1", "no overload for >"),
])
def test_a_computed_field_has_its_return_type(expr, msg):
    with pytest.raises(typecheck.TypeMismatch, match=msg):
        typecheck.check_clause(expr, {"o": typecheck.from_py(Timed)})


@pytest.mark.parametrize("expr", ["o.total == 4", "o.qty == 2", 'o.at > timestamp("2024-01-01T00:00:00Z")',
                                  "o.price2 == 1"])
def test_a_good_computed_field_still_passes(expr):
    env = {"o": typecheck.from_py(Timed)}
    if expr == "o.price2 == 1":
        with pytest.raises(typecheck.TypeMismatch, match="no field 'price2'"):
            typecheck.check_clause(expr, env)
        return
    typecheck.check_clause(expr, env)
