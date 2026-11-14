"""Helpers for the CEL engine's tests: one clause's outcome, as a rule checker reads it."""
from __future__ import annotations

from typing import Any

import invariants_cel as cel


def outcome(prog: cel.Program, env: dict[str, Any]) -> tuple[str, str | None]:
    """('violation' | 'ok' | 'rule_error', error) for one `bad` clause: true is a violation, false is ok,
    and an evaluation error or a value that is not a bool is a rule_error, never an exception (as
    invariants-first's evaluate_env reads a one-clause rule; invariants-py's test_cel_conformance.py runs
    the same cases through it)."""
    try:
        v = prog.evaluate(env)
    except cel.CelError as e:
        return "rule_error", str(e)
    except Exception as e:                  # a backend or conversion defect is a rule_error too
        return "rule_error", f"evaluation raised {type(e).__name__}"
    if not isinstance(v, bool):
        return "rule_error", f"bad evaluated to {type(v).__name__}, not bool"
    return ("violation" if v else "ok"), None
