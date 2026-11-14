"""The report as SARIF 2.1.0."""
from __future__ import annotations

from typing import Any

from .contracts import Finding, Report

_LEVEL = {"bypass": "error", "forbidden": "error", "missing": "error", "unresolved": "warning"}


def to_sarif(report: Report, version: str = "0") -> dict[str, Any]:
    """SARIF 2.1.0. An accepted finding (mediation.allow_unresolved) is a result with an accepted external
    suppression, its justification the entry's why."""
    codes = sorted({f.code for f in [*report.findings, *report.accepted]})
    rules = [{"id": c, "name": c.replace("/", "-"), "shortDescription": {"text": c},
              "defaultConfiguration": {"level": _LEVEL[c.split("/")[0]]}} for c in codes]
    results = [_result(f, codes) for f in report.findings]
    for f in report.accepted:
        res = _result(f, codes)
        why = (f.why or "") + (f" ({f.accepted_by})" if f.accepted_by else "")    # a shared entry says which
        res["suppressions"] = [{"kind": "external", "status": "accepted", "justification": why}]
        results.append(res)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "inv scan", "version": version,
                                      "informationUri": "https://example.com/invariants-first", "rules": rules}},
                  "results": results}],
    }


def _result(f: Finding, codes: list[str]) -> dict[str, Any]:
    res: dict[str, Any] = {
        "ruleId": f.code, "ruleIndex": codes.index(f.code), "level": _LEVEL[f.kind],
        "message": {"text": f"{f.rule}: {f.message}"},
        "properties": {"invRule": f.rule, "kind": f.kind, "path": f.path},
    }
    if f.sha:
        res["partialFingerprints"] = {"invSite/v3": f.sha}     # v3: the syntax-tree sha (fix round 29)
    if f.file:
        res["locations"] = [{"physicalLocation": {
            "artifactLocation": {"uri": f.file},
            "region": {"startLine": max(f.line, 1), "startColumn": max(f.col, 1)}}}]
    if f.path:
        res["codeFlows"] = [{"threadFlows": [{"locations": [
            {"location": {"message": {"text": step}}} for step in f.path]}]}]
    return res
