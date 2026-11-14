"""The report: per-rule counts (terminal and JSON), and the pytest-xdist worker payload the controller merges."""
from __future__ import annotations

from typing import Any

import pytest

from .ablation import _Ablation

KINDS = ("ok", "violation", "rule_error", "parse_error", "abstain")


class _Report(_Ablation):
    """InvSession's report, and what pytest-xdist workers send the controller."""

    # --- pytest-xdist -------------------------------------------------------------------------
    def worker_payload(self) -> dict[str, Any]:
        """What a worker sends the controller (plain lists and dicts: execnet carries them)."""
        return {"statuses": [[rid, st] for rid, st in self.statuses.items()],
                "expect": [[rid, [list(e) for e in rows]] for rid, rows in self._expect.items()],
                "uncollected": [[rid, [list(u) for u in rows]] for rid, rows in self._uncollected.items()],
                "results": [[rid, nodeid, res] for (rid, nodeid), res in self._results.items()],
                "notes": list(self.ablation_notes), "totals": self.totals,
                "verify": {"problems": self.verify_problems, "count": self.verify_count}}

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        """Controller: merge one worker's plan, results, counts and verify result."""
        data = (getattr(node, "workeroutput", None) or {}).get("invariants")
        if not isinstance(data, dict):
            return
        self.workers_reported += 1
        for rid, st in data["statuses"]:
            self.statuses.setdefault(rid, st)
        for rid, rows in data["expect"]:                # every worker collects the same items
            self._expect.setdefault(rid, [(int(a), int(b), str(c)) for a, b, c in rows])
        for rid, rows in data["uncollected"]:
            self._uncollected.setdefault(rid, [(int(a), str(b)) for a, b in rows])
        for rid, nodeid, res in data["results"]:
            self._results[(rid, nodeid)] = res
        self.ablation_notes += [n for n in data["notes"] if n not in self.ablation_notes]
        for rid, row in data["totals"].items():
            mine = self.totals.setdefault(rid, {})
            for k, v in row.items():
                mine[k] = mine.get(k, 0) + v
        verify = data["verify"]
        self.verify_problems += [p for p in verify["problems"] if p not in self.verify_problems]
        if verify["count"] is not None:
            self.verify_count = verify["count"]

    # --- report -------------------------------------------------------------------------------
    def report_data(self) -> dict[str, Any]:
        statuses = self.statuses or {rid: r.status for rid, r in self.session_registry().rules.items()}
        ids = list(statuses) + sorted(set(self.totals) - set(statuses))
        rules = {}
        for rid in ids:
            c = self.totals.get(rid, {})
            rules[rid] = {"status": statuses.get(rid), "reach": c.get("runs", 0),
                          **{k: c.get(k, 0) for k in KINDS}}
        data: dict[str, Any] = {"rules_file": self._shown(self.rules_path), "rules": rules}
        if self.verify_on:
            data["verify"] = {"ok": not self.verify_problems, "problems": self.verify_problems}
        if self.ablate_on:
            data["ablation"] = {"not_load_bearing": self.not_load_bearing, "unverified": self.unverified,
                                "rules": self.ablation, "notes": self.ablation_notes}
        return data

    def _write_report(self, tr: Any) -> None:
        rules = self.report_data()["rules"]
        tr.write_sep("-", "invariants report")
        if not rules:
            tr.write_line("  no rules loaded and no rule evaluations recorded")
            return
        tr.write_line(f"  {'rule':<10} {'status':<10} {'reach':>6} {'ok':>6} {'violation':>9} "
                      f"{'rule_error':>10} {'parse_error':>11} {'abstain':>7}")
        for rid, r in rules.items():
            tr.write_line(f"  {rid:<10} {r['status'] or '-':<10} {r['reach']:>6} {r['ok']:>6} {r['violation']:>9} "
                          f"{r['rule_error']:>10} {r['parse_error']:>11} {r['abstain']:>7}")
