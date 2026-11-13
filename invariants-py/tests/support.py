"""Shared helpers for the core tests."""
from __future__ import annotations

import copy
import json
import pathlib
from typing import Any, Callable

import invariants as inv

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
RULE_FILE = FIXTURES / "invariants.json"
YAML_RULE_FILE = FIXTURES / "invariants.yaml"
RULES: dict[str, Any] = json.loads(RULE_FILE.read_text())
GOOD = '{"cause": "OUT_OF_MEMORY", "message": "killed at 8 GiB"}'


def raises(exc: type[BaseException], fn: Callable[..., Any], *a: Any, **kw: Any) -> Any:
    """Return the exception fn raises (of type exc), or None."""
    try:
        fn(*a, **kw)
    except exc as e:
        return e
    return None


def variant(mutate: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """A deep copy of the rule file with one change."""
    doc = copy.deepcopy(RULES)
    mutate(doc)
    return doc


def rule(doc: dict[str, Any], rid: str) -> dict[str, Any]:
    return next(r for r in doc["rules"] if r["id"] == rid)


def load_variant(mutate: Callable[[dict[str, Any]], Any]) -> inv.Registry:
    return inv.load(variant(mutate), install=False, base_dir=FIXTURES)


def load_error(mutate: Callable[[dict[str, Any]], Any]) -> inv.LoadError | None:
    return raises(inv.LoadError, load_variant, mutate)


def model(*replies: str, job: str = "J-oom") -> tuple[Any, Any]:
    """Script the fake model client and call propose_cause; return (checked, error)."""
    import triage
    triage.client.script(*replies)
    try:
        return triage.propose_cause("stack trace ...", job), None
    except inv.InvariantError as err:
        return None, err


read_doc = inv.read_document
