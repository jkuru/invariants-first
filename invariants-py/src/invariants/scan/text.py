"""The human-readable report."""
from __future__ import annotations

from .contracts import Report


def to_text(report: Report) -> str:
    """Human-readable report, one finding per line."""
    lines = []
    for f in report.findings:
        where = f"{f.file}:{f.line}:{f.col}" if f.file else "-"
        lines.append(f"{f.kind:<10} {f.rule:<8} {where}  {f.message}")
    for rid in report.scanned:
        c = report.counts(rid)
        summary = ", ".join(f"{n} {k}" for k, n in c.items()) or "no findings"
        lines.append(f"{rid}: {report.rule_result(rid)} ({summary})")
    for rid, why in report.not_scanned.items():
        lines.append(f"{rid}: not scanned ({why})")
    lines.append(f"scanned {report.files} file(s), {report.functions} function(s)")
    return "\n".join(lines)
