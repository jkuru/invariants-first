"""Errors every CEL backend raises."""
from __future__ import annotations

import contextvars
from typing import Any


class CelSyntaxError(Exception):
    """Rule text does not compile: a load-time failure."""


class CelError(Exception):
    """Evaluation error: becomes outcome rule_error, never violation.

    detail: the message of the exception behind it (a property that raised, say). It may hold
    secrets, so it is not in str(self); the guards add it to outcome errors only with debug on
    (inv.configure(debug=True) or INV_DEBUG=1).
    """

    detail: str | None = None


def raised(what: str, e: BaseException) -> CelError:
    """CelError('<what> raised <Type>'), with e's message kept apart in .detail."""
    err = CelError(f"{what} raised {type(e).__name__}")
    try:
        err.detail = str(e)
    except Exception:
        err.detail = None
    return err


# ---- non-finite numbers ------------------------------------------------------------------------
# Every IEEE comparison with NaN is false, so a clause like `x < 0.0 || x > 1.0` is silently false
# for x = NaN. By default a rule that reads a NaN or +-Infinity is a rule_error on every backend;
# inv.configure(allow_non_finite=True) sets this flag for the evaluation instead.
ALLOW_NON_FINITE: contextvars.ContextVar[bool] = contextvars.ContextVar("inv_allow_non_finite", default=False)
NON_FINITE = "non-finite number (NaN or Infinity) in rule input"
NON_FINITE_RESULT = "non-finite number (NaN or Infinity) from arithmetic (division by zero or overflow)"


def check_finite(v: float) -> float:
    """v, unless it is NaN or +-Infinity and non-finite numbers are not allowed (CelError)."""
    if v - v == 0.0 or ALLOW_NON_FINITE.get():
        return v
    raise CelError(NON_FINITE)


def check_result(v: Any) -> Any:
    """v, unless it is a double NaN or +-Infinity made by evaluation (arithmetic, double(), inv.sum)
    and non-finite numbers are not allowed (CelError). Every backend calls this on such results."""
    if type(v) is not float or v - v == 0.0 or ALLOW_NON_FINITE.get():
        return v
    raise CelError(NON_FINITE_RESULT)
