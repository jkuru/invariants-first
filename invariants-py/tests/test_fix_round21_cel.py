"""cel/subset after its split: receiver-form has() is a syntax error (it had leaked IndexError or
TypeError), and == or `in` over a Mapping whose reading raises is a CelError with the cause in .detail."""
from __future__ import annotations

from collections.abc import Mapping

import pytest

from invariants.cel import CelError, CelSyntaxError, available_backends, get_backend
from invariants.cel.subset import SubsetBackend


@pytest.mark.parametrize("src", ["x.has()", "x.has(a)", "x.has(a.b)", "[1].has(a.b)"])
def test_receiver_form_has_is_a_syntax_error(src):
    with pytest.raises(CelSyntaxError, match="global form"):
        SubsetBackend().compile(src)


class Boom(Mapping):
    def __getitem__(self, k):
        raise RuntimeError("SECRET")

    def __iter__(self):
        raise RuntimeError("SECRET")

    def __len__(self):
        return 1


@pytest.mark.parametrize("src", ["m == m", "m in [m]", "[m] == [m]"])
def test_equality_over_a_raising_mapping_is_a_cel_error(src):
    with pytest.raises(CelError) as ei:
        SubsetBackend().compile(src).evaluate({"m": Boom()})
    assert "SECRET" not in str(ei.value) and "SECRET" in (ei.value.detail or "")


@pytest.mark.skipif("cel-expr" not in available_backends(), reason="needs cel-expr-python")
def test_cel_expr_also_refuses_receiver_has():
    with pytest.raises((CelError, CelSyntaxError)):
        get_backend("cel-expr").compile("x.has(a)")
