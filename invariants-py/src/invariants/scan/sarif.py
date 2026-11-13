"""The report as SARIF 2.1.0."""
from __future__ import annotations

from typing import Any

from .contracts import Report

_LEVEL = {"bypass": "error", "forbidden": "error", "missing": "error", "unresolved": "warning"}


def to_sarif(report: Report, version: str = "0") -> dict[str, Any]:
    """SARIF 2.1.0."""
    codes = sorted({f.code for f in report.findings})
    rules = [{"id": c, "name": c.replace("/", "-"), "shortDescription": {"text": c},
              "defaultConfiguration": {"level": _LEVEL[c.split("/")[0]]}} for c in codes]
    results = []
    for f in report.findings:
        res: dict[str, Any] = {
            "ruleId": f.code, "ruleIndex": codes.index(f.code), "level": _LEVEL[f.kind],
            "message": {"text": f"{f.rule}: {f.message}"},
            "properties": {"invRule": f.rule, "kind": f.kind, "path": f.path},
        }
        if f.file:
            res["locations"] = [{"physicalLocation": {
                "artifactLocation": {"uri": f.file},
                "region": {"startLine": max(f.line, 1), "startColumn": max(f.col, 1)}}}]
        if f.path:
            res["codeFlows"] = [{"threadFlows": [{"locations": [
                {"location": {"message": {"text": step}}} for step in f.path]}]}]
        results.append(res)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "inv scan", "version": version,
                                      "informationUri": "https://example.invalid/invariants", "rules": rules}},
                  "results": results}],
    }
