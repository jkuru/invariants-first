"""CEL backend conformance: the YAML cases in tests/conformance/cel, run on every installed backend.

Known disagreements are listed per backend in expected_failures.yaml and run as strict xfail.
Set INV_CEL_FULL_MATRIX=1 to also run the whole test suite once per backend.
"""
from __future__ import annotations

import datetime
import decimal
import os
import pathlib
import subprocess
import sys
import uuid
from typing import Any

import pytest
import yaml

import invariants_cel as cel
from cel_support import outcome

HERE = pathlib.Path(__file__).parent
CASES_DIR = HERE / "conformance" / "cel"
OUTCOMES = {"ok", "violation", "rule_error", "parse_error"}


def load_cases() -> list[dict[str, Any]]:
    """Every case, tagged with its id 'suite/name'."""
    out = []
    for f in sorted(CASES_DIR.glob("*.yaml")):
        if f.name == "expected_failures.yaml":
            continue
        doc = yaml.safe_load(f.read_text())
        for c in doc["cases"]:
            out.append({**c, "id": f"{doc['suite']}/{c['name']}"})
    return out


def load_xfails() -> dict[str, dict[str, str]]:
    return yaml.safe_load((CASES_DIR / "expected_failures.yaml").read_text()) or {}


CASES = load_cases()
XFAILS = load_xfails()
BACKENDS = cel.available_backends()


def strict_eq(a: Any, b: Any) -> bool:
    """Equality that tells int from double and bool from int."""
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(map(strict_eq, a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(strict_eq(a[k], b[k]) for k in a)
    return a == b


# Python values YAML cannot write, as one-key maps in a case's env (see conformance/cel/README.md)
_PY = {
    "$timestamp": lambda v: datetime.datetime.fromisoformat(v.replace("Z", "+00:00")),
    "$naive": datetime.datetime.fromisoformat,
    "$duration": lambda v: datetime.timedelta(seconds=v),
    "$uuid": uuid.UUID,
    "$date": datetime.date.fromisoformat,
    "$decimal": decimal.Decimal,
}


def py_value(v: Any) -> Any:
    """A case's env value as Python data, with the $-tagged values decoded."""
    if isinstance(v, dict):
        if len(v) == 1 and next(iter(v)) in _PY:
            k, x = next(iter(v.items()))
            return _PY[k](x)
        return {k: py_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [py_value(x) for x in v]
    return v


def run_case(backend: str, case: dict[str, Any]) -> tuple[str, Any]:
    """('parse_error'|'rule_error'|'ok'|'violation'|'value', value) through the real evaluator."""
    try:
        prog = cel.get_backend(backend).compile(case["expr"])
    except cel.CelSyntaxError:
        return "parse_error", None
    env = py_value(case.get("env") or {})
    if "value" in case:
        try:
            return "value", prog.evaluate(env)
        except cel.CelError as e:
            return "rule_error", str(e)
    return outcome(prog, env)


def params() -> list[Any]:
    out = []
    for b in BACKENDS:
        for c in CASES:
            if "backends" in c and b not in c["backends"]:
                continue
            reason = XFAILS.get(b, {}).get(c["id"])
            marks = [pytest.mark.xfail(reason=reason, strict=True)] if reason else []
            out.append(pytest.param(b, c, id=f"{b}:{c['id']}", marks=marks))
    return out


@pytest.mark.parametrize("backend,case", params())
def test_cel_conformance(backend: str, case: dict[str, Any]) -> None:
    kind, got = run_case(backend, case)
    if "value" in case:
        assert kind == "value", f"expected value {case['value']!r}, got {kind}: {got}"
        assert strict_eq(got, case["value"]), f"expected {case['value']!r}, got {got!r}"
    else:
        assert kind == case["expected"], f"expected {case['expected']}, got {kind} ({got})"


def test_cel_conformance_cases_well_formed() -> None:
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)) and len(ids) > 100
    for c in CASES:
        assert isinstance(c["expr"], str), c["id"]
        assert ("value" in c) != ("expected" in c), c["id"]
        assert "value" in c or c["expected"] in OUTCOMES, c["id"]
    suites = {i.split("/")[0] for i in ids}
    assert {"literals", "fields", "operators", "macros", "logic", "extensions", "types", "parse",
            "cost", "time", "pyvalues", "nesting"} <= suites


def test_cel_conformance_xfails_name_real_cases() -> None:
    ids = {c["id"] for c in CASES}
    for backend, entries in XFAILS.items():
        assert backend in cel._FACTORIES, backend
        for cid, reason in (entries or {}).items():
            assert cid in ids, f"{backend}: no such case {cid}"
            assert isinstance(reason, str) and reason.strip(), f"{backend}: {cid} needs a reason"


def test_cel_conformance_covers_every_installed_backend() -> None:
    assert "subset" in BACKENDS
    for b in BACKENDS:
        assert cel.get_backend(b).name == b


@pytest.mark.skipif(not os.environ.get("INV_CEL_FULL_MATRIX"), reason="set INV_CEL_FULL_MATRIX=1")
@pytest.mark.parametrize("backend", BACKENDS)
def test_cel_full_suite_per_backend(backend: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "INV_CEL_FULL_MATRIX"}
    env[cel.ENV_VAR] = backend
    r = subprocess.run([sys.executable, "-m", "pytest", str(HERE), "-q", "-p", "no:cacheprovider"],
                       env=env, cwd=HERE.parent, stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-4000:]
