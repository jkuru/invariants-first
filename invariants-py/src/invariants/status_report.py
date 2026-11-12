"""`inv status`: computed standing per rule, from the registry's standing, `inv verify` and `inv scan`."""
from __future__ import annotations

from typing import Any

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


def rule_status(reg: Registry, report: Report, rule_id: str) -> dict[str, Any]:
    """One rule's row: status, standing, reasons, scan result."""
    rule = reg.rules[rule_id]
    standing, reasons = reg.standing(rule_id, scan=True)
    reasons = list(reasons)
    if standing in ("held", "enforce(known-callers)"):
        pure = (rule.effects == "none" and rule.target is not None
                and report.not_scanned.get(rule_id) == "mediation declares no sink")
        why = _scan_reason(report, rule_id, pure)
        if why:
            reasons.append(why)
            standing = DOWNGRADE.get(standing, standing)
        if rule_id in report.scanned and rule_id in report.warnings:
            reasons.append(f"inv scan: {report.warnings[rule_id]}")
            standing = DOWNGRADE.get(standing, standing)
    return {"id": rule_id, "status": rule.status, "standing": standing, "reasons": reasons,
            "scan": report.rule_result(rule_id), "scan_counts": report.counts(rule_id),
            "scan_warning": report.warnings.get(rule_id)}


def build(reg: Registry, report: Report) -> dict[str, Any]:
    """The whole status report."""
    ok, problems = verify_result(reg)
    rows = [rule_status(reg, report, rid) for rid in reg.rules]
    return {"verify": {"passed": ok, "problems": problems}, "rules": rows}


def to_text(data: dict[str, Any]) -> str:
    lines = []
    for r in data["rules"]:
        lines.append(f"{r['id']:<8} {r['status']:<10} {r['standing']}")
        lines += [f"         - {why}" for why in r["reasons"]]
    v = data["verify"]
    lines.append("verify: pass" if v["passed"] else f"verify: FAIL ({len(v['problems'])} problem(s))")
    return "\n".join(lines)
