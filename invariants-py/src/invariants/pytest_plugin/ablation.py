"""The ablation protocol: match falsifiers to collected items, re-run each one (control, ablated, second
control) inside the normal test protocol, and judge each rule from those runs."""
from __future__ import annotations

import os
import tempfile
from typing import Any, Callable, Sequence, cast

import pytest

from ..registry import Registry, ablated_everywhere, capture, is_decision
from .rerun import _carried, _decided, _env_ablated, _env_decisions, _matches, _outcome, _reached
from .state import NO_RULES, _SessionState

NO_VERDICT = "inconclusive (read an ablated scan's exit code or pass; assert its findings)"
NEVER_FIRED = "inconclusive (the rule never fired on the control run: the test passes whether or not it holds)"


class _Ablation(_SessionState):
    """InvSession's ablation: the plan, the re-runs inside the test protocol, and the results."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool | None:
        """A falsifier runs as usual, then its control and ablated re-runs, then the normal teardown."""
        rule_ids = self._falsifier_of.get(item.nodeid)
        if not rule_ids or self.config.getoption("setuponly", False) or self.config.getoption("setupplan", False):
            return None                                 # pytest's own protocol
        from _pytest.runner import runtestprotocol      # the documented way plugins (re-)run an item

        # Tear each run down to the item only (its parent's chain stays set up), so every fixture
        # broader than the function lives across the re-runs; _teardown_to then does what pytest's
        # own teardown with the real nextitem would have done.
        keep = cast(pytest.Item, item.parent)
        ihook = item.ihook
        ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        try:
            main = _outcome(runtestprotocol(item, nextitem=keep, log=True))
            if main != "passed":
                self._note(rule_ids, item, f"not checked ({main} with the guard on)")
            elif item.session.shouldfail or item.session.shouldstop:
                self._note(rule_ids, item, "not checked (the session is stopping)")
            else:
                sections = list(item._report_sections)      # the re-runs' captured output is not this test's
                try:
                    self.ablate_item(item, keep, rule_ids, runtestprotocol)
                finally:
                    item._report_sections[:] = sections
        finally:
            self._teardown_to(item, nextitem)
            ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        return True

    def _note(self, rule_ids: Sequence[str], item: pytest.Item, result: str) -> None:
        for rid in rule_ids:
            self._results[(rid, item.nodeid)] = result

    def _teardown_to(self, item: pytest.Item, nextitem: pytest.Item | None) -> None:
        """Tear down what the next item does not share, reporting an error as this item's teardown."""
        stopping = item.session.shouldfail or item.session.shouldstop
        target = None if stopping else nextitem
        state = item.session._setupstate             # what pytest's own teardown step calls

        def teardown() -> None:
            __tracebackhide__ = True
            state.teardown_exact(target)
        call = pytest.CallInfo.from_call(teardown, when="teardown", reraise=(pytest.exit.Exception, KeyboardInterrupt))
        if call.excinfo is not None:
            report = item.ihook.pytest_runtest_makereport(item=item, call=call)
            item.ihook.pytest_runtest_logreport(report=report)

    def plan_ablation(self, session: pytest.Session, reg: Registry) -> None:
        """Match each rule's falsified_by entries to the collected items."""
        if not reg.rules:
            # a green run that ablated nothing proves nothing: a usage error (exit 4), not a pass
            raise pytest.UsageError(f"--inv-ablate: {NO_RULES.format(root=self._shown_root())}")
        index = {item.nodeid: i for i, item in enumerate(session.items)}
        for rule in reg.rules.values():
            falsifiers = rule.raw.get("falsified_by") or []
            if rule.status == "retired" or not falsifiers:
                continue
            expect, missing = self._expect.setdefault(rule.id, []), self._uncollected.setdefault(rule.id, [])
            for fi, f in enumerate(falsifiers):
                items = [i for i in session.items if _matches(i.nodeid, f)]
                if not items:
                    missing.append((fi, f))
                for item in items:
                    if any(nodeid == item.nodeid for _, _, nodeid in expect):
                        continue                    # named twice (a module and a test in it): run once
                    expect.append((fi, index[item.nodeid], item.nodeid))
                    self._falsifier_of.setdefault(item.nodeid, []).append(rule.id)

    def _control_run(self, item: pytest.Item, keep: pytest.Item, rerun: Callable[..., list[pytest.TestReport]]
                     ) -> tuple[str, list[dict[str, Any]], set[str]]:
        """The control re-run, without ablation: its outcome, its events, and the rules that decided something."""
        with tempfile.TemporaryDirectory() as d:
            decisions = os.path.join(d, "decided")
            with capture() as events, _env_decisions(decisions):
                control = _outcome(rerun(item, nextitem=keep, log=False))
            decided = _decided(decisions)
        return control, events, decided

    def _ablated_run(self, item: pytest.Item, keep: pytest.Item, rid: str,
                     rerun: Callable[..., list[pytest.TestReport]]) -> tuple[str, set[str]]:
        """The re-run with rid ablated everywhere: its outcome, and how the ablation was reached."""
        self.no_verdict = []
        with tempfile.TemporaryDirectory() as d:
            trace = os.path.join(d, "reached")
            self.ablating = rid
            try:
                with ablated_everywhere(rid), _env_ablated(rid, trace):
                    out = _outcome(rerun(item, nextitem=keep, log=False))
            finally:
                self.ablating = None
            reached = _reached(trace, rid)
        return out, reached

    def ablate_item(self, item: pytest.Item, keep: pytest.Item, rule_ids: Sequence[str],
                    rerun: Callable[..., list[pytest.TestReport]]) -> None:
        """The falsifier passed with its rules on: a control re-run, then one ablated re-run per rule."""
        counting, self.counting = self.counting, False  # re-runs are not counted in the report
        try:
            control, events, decided = self._control_run(item, keep, rerun)
            for rid in rule_ids:
                if control != "passed":                 # it fails without the ablation: not evidence
                    self._note([rid], item, f"inconclusive ({control} on a control re-run without ablation; "
                                            f"{_carried(item)})")
                    continue
                mine = [e for e in events if e.get("rule") == rid]
                kinds = {e.get("outcome") for e in mine}
                # an edge's refusal of a received verdict (even of a rule_error one), and an escalated
                # abstain, are decisions of the rule: never "errored, not violated"
                decisive = bool(kinds & {"violation", "parse_error"}) or any(
                    is_decision(str(e.get("outcome")), e) for e in mine)
                out, reached = self._ablated_run(item, keep, rid, rerun)
                if out == "failed" and "rule_error" in kinds and not decisive:
                    # with the guard on, the test passed because the rule errored (a typo, a missing
                    # key), not because it found the violation: a fail_closed error rejects too
                    result = "inconclusive (rule_error, not violation)"
                elif out == "failed" and not reached:
                    result = "inconclusive (rule not reached)"
                elif out == "failed" and "scan" not in reached and rid not in decided:
                    # the ablated run skipped the rule, but on the control run the rule never
                    # rejected anything: it was not what kept the test passing (a scan falsifier
                    # has no in-process decision; its findings are what the test asserts)
                    result = NEVER_FIRED
                elif out == "failed":
                    # did the ablation fail it, or state a broader fixture carries from the earlier runs
                    # (a quota, a seeded row)? A second control, with the rule on, tells them apart
                    again = _outcome(rerun(item, nextitem=keep, log=False))
                    result = "caught" if again == "passed" else (
                        f"inconclusive (state carried between runs: {again} on a control re-run after the "
                        f"ablated one; {_carried(item)})")
                elif out == "skipped" and self.no_verdict:
                    result = NO_VERDICT
                else:
                    result = "still passes" if out == "passed" else f"inconclusive ({out})"
                self._note([rid], item, result)
        finally:
            self.counting = counting

    @property
    def ablation(self) -> dict[str, list[dict[str, str]]]:
        """Per rule, one row per falsifier entry or item, in falsified_by order then collection order."""
        out: dict[str, list[dict[str, str]]] = {}
        for rid, expect in self._expect.items():
            rows = [(fi, -1, {"falsifier": f, "result": "not collected"}) for fi, f in self._uncollected.get(rid, [])]
            rows += [(fi, ii, {"falsifier": nodeid, "result": self._results.get((rid, nodeid), "not run")})
                     for fi, ii, nodeid in expect]
            out[rid] = [row for _, _, row in sorted(rows, key=lambda r: (r[0], r[1]))]
        return out

    @property
    def not_load_bearing(self) -> list[str]:
        return [rid for rid, rows in self.ablation.items() if any(r["result"] == "still passes" for r in rows)]

    @property
    def unverified(self) -> list[str]:
        """enforce rules with no caught falsifier (not collected, failing with the guard on, inconclusive)."""
        if self.lenient:
            return []
        bad = self.not_load_bearing
        return [rid for rid, rows in self.ablation.items()
                if self.statuses.get(rid) == "enforce" and rid not in bad
                and not any(r["result"] == "caught" for r in rows)]

    def _write_ablation(self, tr: Any) -> None:
        ablation = self.ablation
        bad, unverified = self.not_load_bearing, self.unverified
        if bad:
            tr.write_sep("=", f"invariants ablation FAILED: guard not load-bearing for {', '.join(bad)}",
                         red=True, bold=True)
        if unverified:
            tr.write_sep("=", f"invariants ablation FAILED: no falsifier caught the ablation of "
                              f"{', '.join(unverified)} (status enforce needs one)", red=True, bold=True)
        if not bad and not unverified:
            tr.write_sep("-", "invariants ablation", green=bool(ablation))
        for note in self.ablation_notes:
            tr.write_line(f"  {note}")
        for rid, rows in ablation.items():
            verdict = "guard not load-bearing" if rid in bad else (
                "load-bearing" if any(r["result"] == "caught" for r in rows) else "not checked")
            tr.write_line(f"  {rid}: {verdict}")
            for r in rows:
                tr.write_line(f"      {r['result']:<14} {r['falsifier']}")
