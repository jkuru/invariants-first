"""Helpers of one falsifier's re-runs: its outcome, the fixtures that live across the re-runs, the
falsified_by match, and the environment and trace files that carry ablation and decisions to subprocesses."""
from __future__ import annotations

import contextlib
import os
from typing import Iterator

import pytest

from ..registry import ABLATE_ARM_ENV, ABLATE_ENV, ABLATE_TRACE_ENV, DECISION_TRACE_ENV


def _outcome(reports: list[pytest.TestReport]) -> str:
    """passed | failed | skipped for one run of one test (xfail counts as skipped)."""
    if any(r.failed for r in reports):
        return "failed"
    if any(r.skipped or hasattr(r, "wasxfail") for r in reports):
        return "skipped"
    return "passed"


def _broad_fixtures(item: pytest.Item) -> list[str]:
    """The fixtures broader than the function the item uses (pytest's own left out), as "name (scope)"."""
    info = getattr(item, "_fixtureinfo", None)
    out = []
    for name in getattr(item, "fixturenames", ()):
        defs = info.name2fixturedefs.get(name) if info is not None else None
        if not defs:
            continue
        fd = defs[-1]
        if fd.scope != "function" and not getattr(fd.func, "__module__", "").startswith(("_pytest", "pytest")):
            out.append(f"{name} ({fd.scope})")
    return out


def _carried(item: pytest.Item) -> str:
    """Why a re-run may differ from the first run: the broader fixtures that live across the re-runs."""
    broad = _broad_fixtures(item)
    return (f"fixtures broader than the function: {', '.join(broad)}" if broad else
            "it uses no fixture broader than the function: other shared state, or a flaky test")


def _matches(nodeid: str, falsifier: str) -> bool:
    """A falsified_by entry names a test node id, or a prefix of one (module, class, parametrised test)."""
    f = falsifier.replace(os.sep, "/").removeprefix("./")
    return nodeid == f or nodeid.startswith(f + "[") or nodeid.startswith(f + "::")


@contextlib.contextmanager
def _env_ablated(rule_id: str, trace: str | None = None) -> Iterator[None]:
    """Add rule_id to INV_ABLATE (and arm it with INV_ABLATE_TESTING=1) for the block.

    Subprocesses and new registries then see the rule off too. With trace, INV_ABLATE_TRACE names
    the file where any process notes that the ablation was reached.
    """
    saved = {k: os.environ.get(k) for k in (ABLATE_ENV, ABLATE_ARM_ENV, ABLATE_TRACE_ENV)}
    os.environ[ABLATE_ENV] = ",".join(filter(None, [saved[ABLATE_ENV], rule_id]))
    os.environ[ABLATE_ARM_ENV] = "1"
    if trace is not None:
        os.environ[ABLATE_TRACE_ENV] = trace
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _env_decisions(trace: str) -> Iterator[None]:
    """Set INV_DECISION_TRACE for the block: any process notes there the rules that decided something."""
    saved = os.environ.get(DECISION_TRACE_ENV)
    os.environ[DECISION_TRACE_ENV] = trace
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(DECISION_TRACE_ENV, None)
        else:
            os.environ[DECISION_TRACE_ENV] = saved


def _lines(trace: str) -> list[list[str]]:
    try:
        with open(trace, encoding="utf-8") as f:
            return [line.split() for line in f if line.strip()]
    except OSError:
        return []


def _reached(trace: str, rule_id: str) -> set[str]:
    """How some process noted reaching rule_id's ablation in the trace file: "enforcer", "scan" (none: empty)."""
    return {(parts[1:] or ["enforcer"])[0] for parts in _lines(trace) if parts[0] == rule_id}


def _decided(trace: str) -> set[str]:
    """Rule ids some process noted a decision for (a violation, an acted-on parse_error, an abstain)."""
    return {parts[0] for parts in _lines(trace)}
