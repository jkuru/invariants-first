"""`inv status`: computed standing per rule, from the registry's standing, `inv verify` and `inv scan`.

Each row says, in plain words, what the rule checks, whether a guard wraps it, whether ablation proved
its tests fail without it, and what to do next. The JSON keeps the computed `standing` and its
`reasons` (the precise terms) beside those plain fields."""
from __future__ import annotations

import json
import pathlib
from typing import Any, Iterable, Mapping

from .errors import CoverageError
from .model import GUARD_POINTS, Rule
from .registry import Registry
from .registry.coverage import _ablation_problems, _target_problems
from .scan import Report

DOWNGRADE = {"held": "enforce(known-callers)"}
# an unresolved site cannot go in allow: (that freezes a resolved call path); a person who reviewed
# it accepts it in allow_unresolved, by the sha the scan gives it
UNRESOLVED_NEXT = ("make each unresolved call resolvable, or accept a reviewed unresolved site in "
                   "allow_unresolved (inv scan --format json gives its sha)")
ABLATE_CMD = ("pytest --inv-rules invariants.yaml --inv-ablate --inv-report-json build/inv-report.json, then "
              "inv status --ablation build/inv-report.json")
NO_ABLATION = (f"held is unproven: no ablation results were given, so nothing shows a falsifier fails with "
               f"the rule off (run {ABLATE_CMD})")
UNPROVEN = "held (unproven: no --ablation)"

# what `ablation` says: proved, not proved, not run (no --ablation report), or not needed (the scan
# is a door rule's whole check)
ABLATION_STATES = ("yes", "no", "not run", "not needed")
STATUS_WORDS = {"enforce": "blocks a bad call", "detect": "logs a bad call, does not block it",
                "candidate": "drafted, not checked", "contested": "under review, not checked",
                "retired": "retired, not checked"}


def verify_result(reg: Registry, imports: Iterable[str] = ()) -> tuple[bool, list[str]]:
    """(passed, problems) of `inv verify` on a loaded registry, over the same modules `inv verify`
    checks: the rule file's scope plus the top-level package of each --import."""
    try:
        reg.verify(sorted(reg.scope() | {m.split(".")[0] for m in imports}))
    except CoverageError as e:
        return False, str(e).split("; ")
    return True, []


def _scan_reason(report: Report, rule_id: str, pure: bool = False) -> str | None:
    result = report.rule_result(rule_id)
    accepted = report.counts(rule_id).get("accepted", 0)
    if result == "pass" and accepted:
        # reviewed unresolved sites (mediation.allow_unresolved): a person's judgment, not the scan's proof
        return (f"inv scan pass with {accepted} unresolved site(s) accepted after review in "
                f"mediation.allow_unresolved (the scan could not follow them)")
    if result == "pass":
        return None
    if result == "not_scanned":
        if pure:
            return None          # effects: none: no effect sink to reach; the guard wraps the function itself
        return f"inv scan cannot check it: {report.not_scanned.get(rule_id, 'not scanned')} (add mediation.sink)"
    counts = ", ".join(f"{n} {k}" for k, n in report.counts(rule_id).items())
    return f"inv scan {result}: {counts}"


def _door_only(rule: Rule) -> bool:
    """A door rule checked only by the scan: structure enforcers, nothing evaluated at run time."""
    live = [e for e in rule.enforcers if e.get("mode") != "off"]
    return bool(live) and all(e.get("point") == "structure" for e in live)


def _names(v: Any) -> str:
    items = [v] if isinstance(v, str) else list(v or [])
    return " or ".join(str(x) for x in items)


def checks_text(rule: Rule) -> str:
    """What the rule checks, in a sentence."""
    med = rule.raw.get("mediation") or {}
    parts = []
    if not _door_only(rule):
        clauses = [m if m != rule.id else str(c) for c, m in zip(_whens(rule), (m for _, m in rule.clauses))]
        where = rule.target_key or "its guard"
        blocks = rule.status == "enforce"             # below enforce a rule logs what it would refuse
        if rule.policy is not None:
            parts.append(f"the model reply of {where} parses into {rule.policy.get('schema')} and is "
                         f"{'refused' if blocks else 'flagged (logged, not refused)'} when: " + "; ".join(clauses))
        else:
            parts.append(f"{where} {'refuses' if blocks else 'flags (logs, does not refuse)'} a call when: "
                         + "; ".join(clauses))
    if med.get("sink"):
        door = med.get("door") or rule.target_key
        parts.append(f"every call to {_names(med['sink'])} goes through {_names(door)}")
    return "; and ".join(parts) or "nothing yet"


def _whens(rule: Rule) -> list[str]:
    bad = rule.raw.get("bad")
    if isinstance(bad, str):
        return [bad]
    return [str(c.get("when")) if isinstance(c, dict) else str(c) for c in (bad or [])]


def guarded(rule: Rule) -> bool | None:
    """Whether a guard wraps the rule's target (None: the rule has no guard to wrap: a door rule, or a
    rule with no target)."""
    if rule.target is None or _door_only(rule):
        return None
    if not rule.policy and not ({e["point"] for e in rule.enforcers} & GUARD_POINTS):
        return None
    return not _target_problems(rule)


def ablation_state(rule: Rule, ablation: Mapping[str, Any] | None) -> str:
    """yes | no | not run | not needed: whether ablation showed the rule's tests fail without it."""
    if _door_only(rule):
        return "not needed"
    if ablation is None:
        return "not run"
    return "no" if _ablation_problems(rule, ablation) else "yes"


def next_steps(rule: Rule, report: Report, row: dict[str, Any]) -> list[str]:
    """What to do next for one rule, most important first; [] when nothing is left."""
    out: list[str] = []
    if rule.status in ("retired", "candidate", "contested"):
        return out
    if row["guarded"] is False:
        out.append(f"put @inv.guard({rule.id!r}) on {rule.target_key} (inv verify lists why it does not count)")
    result = report.rule_result(rule.id)
    if result in ("fail", "unresolved"):
        kinds = {f.kind for f in report.findings if f.rule == rule.id}
        if kinds - {"unresolved"} or not kinds:
            out.append("fix the call paths `inv scan` reports (or freeze a known one in allow:)")
        if "unresolved" in kinds:
            out.append(UNRESOLVED_NEXT)
    if not _door_only(rule):
        if not rule.raw.get("falsified_by"):
            out.append("write a test that makes it refuse (expects inv.Rejected) and list it under tests: "
                       "(falsified_by: in a long-form rule), then the rule blocks instead of logging")
        elif row["ablation"] == "not run":
            out.append(f"prove its tests fail without it: {ABLATE_CMD}")
        elif row["ablation"] == "no":
            out.append("make a test under tests: fail when the rule is off (pytest --inv-ablate says why it did not)")
    if any(r.startswith("verify fails") for r in row["reasons"]) and row["guarded"] is not False:
        out.append("fix what `inv verify` reports")
    if any(r.startswith("ablated") for r in row["reasons"]):
        out.append("it is switched off in this process (INV_ABLATE or inv.ablated): unset it")
    med = rule.raw.get("mediation") or {}
    if med.get("residual_paths"):
        out.append(f"a known way around it is declared (residual_paths: {'; '.join(map(str, med['residual_paths']))}); "
                   f"close it to call the rule held")
    if any(r.startswith("mediation.allow keeps") for r in row["reasons"]):
        out.append("fix the back doors frozen in allow:, then delete each from allow")
    if result == "not_scanned" and any(r.startswith("inv scan cannot check it") for r in row["reasons"]):
        out.append("optional: add sink: (the function that writes what the guard protects), so `inv scan` "
                   "proves no call path skips the guard")
    return out


def status_text(rule: Rule) -> str:
    """What the rule's status does, in words."""
    if rule.status == "enforce" and _door_only(rule):
        return "inv check fails a code path that skips the door"
    return STATUS_WORDS.get(rule.status, rule.status)


def rule_status(reg: Registry, report: Report, rule_id: str,
                ablation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One rule's row: status, standing, reasons, scan result, and the plain fields `checks`,
    `guarded` (true, false, or null when there is no guard to wrap), `ablation` (yes, no, not run, not
    needed) and `next`. ablation: the pytest plugin's --inv-report-json data (see load_ablation): a rule
    no falsifier caught is not held. Without it a held row is labelled unproven (ablation_proven
    false, a reason saying so); ablation_proven is true only for a held row backed by ablation."""
    rule = reg.rules[rule_id]
    door_only = _door_only(rule)          # the scan is its whole check: ablation has nothing to switch off
    standing, reasons = reg.standing(rule_id, scan=True, ablation=None if door_only else ablation)
    reasons = list(reasons)
    if standing in ("held", "enforce(known-callers)"):
        pure = (rule.effects == "none" and rule.target is not None
                and report.not_scanned.get(rule_id) == "mediation declares no sink")
        why = _scan_reason(report, rule_id, pure)
        if why:
            reasons.append(why)
            standing = DOWNGRADE.get(standing, standing)
    if standing == "held" and ablation is None and not door_only:
        reasons.append(NO_ABLATION)          # labelled, never reported as ablation-proven
    row = {"id": rule_id, "status": rule.status, "standing": standing, "reasons": reasons,
           "ablation_proven": standing == "held" and ablation is not None,
           "scan": report.rule_result(rule_id), "scan_counts": report.counts(rule_id),
           "status_text": status_text(rule), "checks": checks_text(rule), "guarded": guarded(rule),
           "ablation": ablation_state(rule, ablation)}
    row["next"] = next_steps(rule, report, row)
    return row


def load_ablation(path: str | pathlib.Path) -> dict[str, Any]:
    """The ablation results in a pytest --inv-report-json file (run with --inv-ablate)."""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("ablation"), dict):
        raise ValueError(f"{path}: no ablation results (run pytest --inv-ablate --inv-report-json {path})")
    return data


def build(reg: Registry, report: Report, ablation: Mapping[str, Any] | None = None,
          imports: Iterable[str] = ()) -> dict[str, Any]:
    """The whole status report (verify runs the registry's verifiers too: installed FastAPI apps)."""
    ok, problems = verify_result(reg, imports)
    rows = [rule_status(reg, report, rid, ablation) for rid in reg.rules]
    return {"verify": {"passed": ok, "problems": problems}, "rules": rows}


_SCAN_WORDS = {"pass": "pass, no path skips it", "fail": "FAIL", "unresolved": "unresolved calls (not a pass)"}


def _guard_line(r: dict[str, Any]) -> str:
    if r.get("guarded") is None:
        return "not needed (inv scan checks the code paths)" if r.get("ablation") == "not needed" else "none"
    return "yes" if r["guarded"] else "NO"


def to_text(data: dict[str, Any]) -> str:
    lines = []
    for r in data["rules"]:
        proven = r.get("ablation_proven") or r.get("ablation") == "not needed"
        held = (f"; {UNPROVEN}" if not proven else "; held") if r["standing"] == "held" else ""
        words = r.get("status_text") or STATUS_WORDS.get(r["status"], r["status"])
        lines.append(f"{r['id']} ({r['status']}: {words}{held})")
        if "checks" in r:
            lines.append(f"  checks:  {r['checks']}")
            lines.append(f"  guard:   {_guard_line(r)}")
            scan = _SCAN_WORDS.get(r.get("scan", ""), "")
            if scan:
                counts = ", ".join(f"{n} {k}" for k, n in (r.get("scan_counts") or {}).items())
                lines.append(f"  scan:    {scan}" + (f" ({counts})" if counts else ""))
            lines.append(f"  ablation proved it: {r['ablation']}")
            for step in r.get("next") or []:
                lines.append(f"  next:    {step}")
        else:                                     # a row built by an older caller: its reasons
            lines += [f"  - {why}" for why in r["reasons"]]
    v = data["verify"]
    lines.append("verify: pass" if v["passed"] else f"verify: FAIL ({len(v['problems'])} problem(s))")
    lines += [f"  {p}" for p in v["problems"]] if not v["passed"] else []
    return "\n".join(lines)
