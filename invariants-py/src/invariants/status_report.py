"""`inv status`: computed standing per rule, from the registry's standing, `inv verify` and `inv scan`."""
from __future__ import annotations

import json
import pathlib
from typing import Any, Mapping

from .errors import CoverageError
from .registry import Registry
from .scan import Report

DOWNGRADE = {"held": "enforce(known-callers)"}


def verify_result(reg: Registry) -> tuple[bool, list[str]]:
    """(passed, problems) of `inv verify` on a loaded registry."""
    try:
        reg.verify()
    except CoverageError as e:
        return False, str(e).split("; ")
    return True, []


def _scan_reason(report: Report, rule_id: str, pure: bool = False) -> str | None:
    result = report.rule_result(rule_id)
    if result == "pass":
        return None
    if result == "not_scanned":
        if pure:
            return None          # effects: none: no effect sink to reach; the guard wraps the function itself
        return f"inv scan cannot check it: {report.not_scanned.get(rule_id, 'not scanned')} (add mediation.sink)"
    counts = ", ".join(f"{n} {k}" for k, n in report.counts(rule_id).items())
    return f"inv scan {result}: {counts}"


def rule_status(reg: Registry, report: Report, rule_id: str,
                ablation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One rule's row: status, standing, reasons, scan result. ablation: the pytest plugin's
    --inv-report-json data (see load_ablation); when given, a rule no falsifier caught is not held."""
    rule = reg.rules[rule_id]
    standing, reasons = reg.standing(rule_id, scan=True, ablation=ablation)
    reasons = list(reasons)
    if standing in ("held", "enforce(known-callers)"):
        pure = (rule.effects == "none" and rule.target is not None
                and report.not_scanned.get(rule_id) == "mediation declares no sink")
        why = _scan_reason(report, rule_id, pure)
        if why:
            reasons.append(why)
            standing = DOWNGRADE.get(standing, standing)
    return {"id": rule_id, "status": rule.status, "standing": standing, "reasons": reasons,
            "scan": report.rule_result(rule_id), "scan_counts": report.counts(rule_id)}


def load_ablation(path: str | pathlib.Path) -> dict[str, Any]:
    """The ablation results in a pytest --inv-report-json file (run with --inv-ablate)."""
    data = json.loads(pathlib.Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("ablation"), dict):
        raise ValueError(f"{path}: no ablation results (run pytest --inv-ablate --inv-report-json {path})")
    return data


def build(reg: Registry, report: Report, ablation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The whole status report (verify runs the registry's verifiers too: installed FastAPI apps)."""
    ok, problems = verify_result(reg)
    rows = [rule_status(reg, report, rid, ablation) for rid in reg.rules]
    return {"verify": {"passed": ok, "problems": problems}, "rules": rows}


def to_text(data: dict[str, Any]) -> str:
    lines = []
    for r in data["rules"]:
        lines.append(f"{r['id']:<8} {r['status']:<10} {r['standing']}")
        lines += [f"         - {why}" for why in r["reasons"]]
    v = data["verify"]
    lines.append("verify: pass" if v["passed"] else f"verify: FAIL ({len(v['problems'])} problem(s))")
    return "\n".join(lines)
