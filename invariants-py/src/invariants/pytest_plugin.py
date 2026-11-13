"""pytest plugin: load a rule file, check guard coverage, ablate rules against their falsifiers, report reach.

Options (all off by default; without them the plugin only adds the fixtures):
  --inv-rules PATH       rule file to load into the default registry (ini: inv_rules)
  --inv-verify           after collection, every target must carry its guard
  --inv-ablate           re-run each rule's falsified_by tests with the rule ablated; they must fail, and
                         every enforce rule needs at least one falsifier that is caught
  --inv-ablate-lenient   with --inv-ablate, fail only on a falsifier that still passes
  --inv-report           print per-rule reach / violation / rule_error counts at session end
  --inv-report-json PATH also write those counts (and verify/ablation results) as JSON

Fixtures: inv_registry (an isolated registry, holding the session rules if any), inv_ablated(rule_id),
inv_events (every event recorded during the test, in any registry), inv_scan (run `inv scan` the way
a falsifier should read it).

A falsifier's re-runs happen inside the normal test protocol, right after its own run: each run is
torn down only to the item, so session, package, module and class fixtures are set up once, as
without the plugin. Under pytest-xdist each worker ablates the falsifiers it runs and the controller
reports the merged results.

During an ablation re-run INV_ABLATE also names the rule, so a falsifier that runs `inv scan` or
the app in a subprocess sees the rule turned off too.

A falsifier counts as caught only when the ablation caused its failure: it must pass on a control
re-run without ablation, and the ablated re-run must reach the rule (a guard, `inv.is_ablated` or
a scan, in this process or a subprocess, acted on the ablation; INV_ABLATE_TRACE carries that).
Otherwise it is inconclusive. It is also inconclusive ("rule_error, not violation") when, on the
control re-run in this process, the rule's evaluations gave rule_error and never a violation or
parse_error: the test passed because the rule could not be evaluated, not because it held.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Sequence, cast

import pytest

from .errors import CoverageError, LoadError
from . import registry
from .loader import load
from .registry import ABLATE_ARM_ENV, ABLATE_ENV, ABLATE_TRACE_ENV, Registry, ablated_everywhere, capture, current, default_registry, isolated, observe

KINDS = ("ok", "violation", "rule_error", "parse_error")
STASH: pytest.StashKey[InvSession] = pytest.StashKey()
SCAN_ABLATED = 4            # `inv scan`'s exit code when it skipped an ablated enforce rule (cli.ABLATED)
NO_VERDICT = "inconclusive (read an ablated scan's exit code or pass; assert its findings)"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("invariants", "invariants rule checks")
    group.addoption("--inv-rules", metavar="PATH", default=None,
                    help="rule file (JSON or YAML) to load into the default registry")
    group.addoption("--inv-verify", action="store_true", default=False,
                    help="after collection, fail the session if a rule's target lacks its guard")
    group.addoption("--inv-ablate", action="store_true", default=False,
                    help="re-run each rule's falsified_by tests with the rule ablated; they must fail, and each "
                         "enforce rule needs one caught falsifier")
    group.addoption("--inv-ablate-lenient", action="store_true", default=False,
                    help="with --inv-ablate, fail only when a falsifier still passes with its rule ablated")
    group.addoption("--inv-report", action="store_true", default=False,
                    help="print per-rule reach, violation and rule_error counts at session end")
    group.addoption("--inv-report-json", metavar="PATH", default=None,
                    help="write the per-rule counts (and verify/ablation results) as JSON")
    parser.addini("inv_rules", "rule file to load (relative to the ini file)", default=None)


def pytest_configure(config: pytest.Config) -> None:
    registry.pytest_session_started()      # env ablation logs quietly only inside a real session
    sess = InvSession(config)
    config.stash[STASH] = sess
    config.pluginmanager.register(sess, "invariants-session")


def pytest_unconfigure(config: pytest.Config) -> None:
    registry.pytest_session_ended()
    sess = config.stash.get(STASH, None)
    if sess is not None:
        sess.restore_default()


def _outcome(reports: list[pytest.TestReport]) -> str:
    """passed | failed | skipped for one run of one test (xfail counts as skipped)."""
    if any(r.failed for r in reports):
        return "failed"
    if any(r.skipped or hasattr(r, "wasxfail") for r in reports):
        return "skipped"
    return "passed"


def _matches(nodeid: str, falsifier: str) -> bool:
    """A falsified_by entry names a test node id, or a prefix of one (module, class, parametrised test)."""
    f = falsifier.replace(os.sep, "/").removeprefix("./")
    return nodeid == f or nodeid.startswith(f + "[") or nodeid.startswith(f + "::")


@contextlib.contextmanager
def _env_ablated(rule_id: str, trace: str | None = None) -> Iterator[None]:
    """Add rule_id to INV_ABLATE (and arm it with INV_ABLATE_TESTING=1) for the block.

    Subprocesses and new registries then see the rule off too. With trace, INV_ABLATE_TRACE names
    the file where any process notes that the ablation was reached.
    """
    saved = {k: os.environ.get(k) for k in (ABLATE_ENV, ABLATE_ARM_ENV, ABLATE_TRACE_ENV)}
    os.environ[ABLATE_ENV] = ",".join(filter(None, [saved[ABLATE_ENV], rule_id]))
    os.environ[ABLATE_ARM_ENV] = "1"
    if trace is not None:
        os.environ[ABLATE_TRACE_ENV] = trace
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _reached(trace: str, rule_id: str) -> bool:
    """True if some process noted reaching rule_id's ablation in the trace file."""
    try:
        with open(trace, encoding="utf-8") as f:
            return rule_id in {line.strip() for line in f}
    except OSError:
        return False


class InvSession:
    """Per-run state of the plugin, registered as a plugin so its hooks run."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        opt = config.getoption
        self.verify_on: bool = opt("inv_verify")
        self.ablate_on: bool = opt("inv_ablate")
        self.lenient: bool = opt("inv_ablate_lenient")
        self.report_on: bool = opt("inv_report")
        self.json_path: str | None = opt("inv_report_json")
        self.rules_path = self._rules_path()
        self.rules: Registry | None = None          # the loaded rule file (not installed anywhere)
        self._saved: Registry | None = None         # the default registry's contents before we installed
        self.verify_problems: list[str] = []
        self.verify_count: int | None = None
        self.ablation_notes: list[str] = []
        self.counting = self.report_on or bool(self.json_path)
        self.totals: dict[str, dict[str, int]] = {}
        self._unobserve: Callable[[], None] | None = None
        self.statuses: dict[str, str] = {}          # every loaded rule's status, in rule-file order
        # the ablation plan, made after collection: per rule, its falsifier items (falsifier index,
        # item index, node id) and the falsified_by entries that match no collected test
        self._expect: dict[str, list[tuple[int, int, str]]] = {}
        self._uncollected: dict[str, list[tuple[int, str]]] = {}
        self._falsifier_of: dict[str, list[str]] = {}   # node id -> the rules it falsifies
        self._results: dict[tuple[str, str], str] = {}  # (rule id, node id) -> result
        self.ablating: str | None = None                # the rule ablated by the re-run in progress
        self.no_verdict: list[str] = []                 # inv_scan verdicts asked for during that re-run
        self.worker = hasattr(config, "workerinput")    # a pytest-xdist worker: the controller reports
        self.workers_reported = 0

    def _shown(self, path: pathlib.Path | None) -> str | None:
        """path relative to the pytest rootdir when it is under it (reports carry no local absolute paths)."""
        if path is None:
            return None
        try:
            return path.resolve().relative_to(pathlib.Path(self.config.rootpath).resolve()).as_posix()
        except (ValueError, OSError):
            return str(path)

    def _rules_path(self) -> pathlib.Path | None:
        cli = self.config.getoption("inv_rules")
        if cli:
            return pathlib.Path(self.config.invocation_params.dir, cli).resolve()
        ini = self.config.getini("inv_rules")
        if ini:
            base = self.config.inipath.parent if self.config.inipath else self.config.rootpath
            return pathlib.Path(base, ini).resolve()
        return None

    @property
    def controller(self) -> bool:
        """The pytest-xdist controller: it runs no tests, and merges what the workers send."""
        return self.config.pluginmanager.has_plugin("dsession")

    # --- loading ------------------------------------------------------------------------------
    def load_rules(self) -> Registry | None:
        """Load the rule file once (after collection, so conftest sys.path changes apply)."""
        if self.rules is None and self.rules_path is not None:
            if not self.rules_path.is_file():
                raise pytest.UsageError(f"--inv-rules: {self.rules_path} does not exist")
            try:
                self.rules = load(self.rules_path, install=False)
            except LoadError as e:
                raise pytest.UsageError(f"--inv-rules: {self.rules_path}: {e}") from e
        return self.rules

    def install_default(self) -> None:
        """Put the session rules into the default registry, remembering what was there."""
        if self.rules is not None and self._saved is None:
            self._saved = Registry().install(default_registry())
            default_registry().install(self.rules)

    def restore_default(self) -> None:
        if self._saved is not None:
            default_registry().install(self._saved)
            self._saved = None

    def session_registry(self) -> Registry:
        return default_registry()

    # --- hooks --------------------------------------------------------------------------------
    def count(self, rule_id: str, kind: str, event: dict[str, Any]) -> None:
        """Observer: count one evaluation in any registry while counting is on."""
        if self.counting:
            row = self.totals.setdefault(rule_id, {})
            if kind != "ablated":
                row["runs"] = row.get("runs", 0) + 1
            row[kind] = row.get(kind, 0) + 1

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        if self.counting:
            self._unobserve = observe(self.count)

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        if self.load_rules() is not None:
            self.install_default()
        reg = self.session_registry()
        self.statuses = {rid: r.status for rid, r in reg.rules.items()}
        if self.ablate_on and not session.config.option.collectonly:
            self.plan_ablation(session, reg)
        if self.verify_on:
            if not reg.rules:
                self.verify_problems = ["no rules loaded: pass --inv-rules PATH (or set inv_rules)"]
                return
            try:
                self.verify_count = reg.verify()
            except CoverageError as e:
                self.verify_problems = str(e).split("; ")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtestloop(self, session: pytest.Session) -> Iterator[Any]:
        try:
            return (yield)
        finally:
            self.counting = False
            if self._unobserve is not None:
                self._unobserve()
                self._unobserve = None

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

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.worker:                             # the controller writes the report and decides
            cast(Any, self.config).workeroutput["invariants"] = self.worker_payload()
            return
        if self.controller and not self.workers_reported and (self.ablate_on or self.verify_on):
            self.ablation_notes.append("no pytest-xdist worker reported its invariants results")
            if self.verify_on and not self.verify_problems:
                self.verify_problems = ["no pytest-xdist worker reported its verify result"]
        if self.json_path:
            path = pathlib.Path(self.config.invocation_params.dir, self.json_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.report_data(), indent=2, sort_keys=True) + "\n")
        if self.failed and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        tr = terminalreporter
        if self.verify_on:
            if self.verify_problems:
                tr.write_sep("=", "invariants verify FAILED", red=True, bold=True)
                for p in self.verify_problems:
                    tr.write_line(f"  {p}")
            elif self.verify_count is not None:
                tr.write_sep("-", f"invariants verify: {self.verify_count} targeted rule(s) guarded", green=True)
        if self.ablate_on:
            self._write_ablation(tr)
        if self.report_on:
            self._write_report(tr)
        if self.json_path:
            tr.write_line(f"invariants report written to {self.json_path}")

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

    # --- ablation -----------------------------------------------------------------------------
    def plan_ablation(self, session: pytest.Session, reg: Registry) -> None:
        """Match each rule's falsified_by entries to the collected items."""
        if not reg.rules:
            self.ablation_notes.append("no rules loaded: pass --inv-rules PATH (or set inv_rules)")
            return
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

    def ablate_item(self, item: pytest.Item, keep: pytest.Item, rule_ids: Sequence[str],
                    rerun: Callable[..., list[pytest.TestReport]]) -> None:
        """The falsifier passed with its rules on: a control re-run, then one ablated re-run per rule."""
        counting, self.counting = self.counting, False  # re-runs are not counted in the report
        try:
            with capture() as events:
                control = _outcome(rerun(item, nextitem=keep, log=False))
            for rid in rule_ids:
                if control != "passed":                 # it fails without the ablation: not evidence
                    self._note([rid], item, f"inconclusive ({control} on a re-run without ablation)")
                    continue
                kinds = {e.get("outcome") for e in events if e.get("rule") == rid}
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
                if out == "failed" and "rule_error" in kinds and not kinds & {"violation", "parse_error"}:
                    # with the guard on, the test passed because the rule errored (a typo, a missing
                    # key), not because it found the violation: a fail_closed error rejects too
                    result = "inconclusive (rule_error, not violation)"
                elif out == "failed":
                    result = "caught" if reached else "inconclusive (rule not reached)"
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

    @property
    def failed(self) -> bool:
        no_worker = self.controller and self.ablate_on and not self.workers_reported
        return bool(self.verify_problems or self.not_load_bearing or (self.ablate_on and self.unverified)
                    or no_worker)

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
                      f"{'rule_error':>10} {'parse_error':>11}")
        for rid, r in rules.items():
            tr.write_line(f"  {rid:<10} {r['status'] or '-':<10} {r['reach']:>6} {r['ok']:>6} {r['violation']:>9} "
                          f"{r['rule_error']:>10} {r['parse_error']:>11}")


# --- inv_scan -------------------------------------------------------------------------------------
@dataclasses.dataclass
class ScanRun:
    """One `inv scan --format json` run, read the way a falsifier should read it.

    An ablated enforce rule makes `inv scan` exit 4: the scan refused to judge that rule, which is
    neither a pass nor a fail. So `failed(rule)` and `findings()` answer what the scan reported (an
    ablated rule has no findings, so a falsifier asserting `failed(rule)` fails under --inv-ablate:
    caught), while `code` and `passed(rule)` have no answer for an ablated rule: under --inv-ablate
    they skip the re-run, which the plugin reports as inconclusive, and anywhere else they fail the
    test. A falsifier therefore asserts the rule's findings, never `code != 0`.
    """

    exit_code: int                  # the raw exit code, 4 included; read `code` in assertions
    report: dict[str, Any]
    stderr: str
    session: InvSession | None = None

    @property
    def ablated(self) -> list[str]:
        """Rules the scan skipped because they are ablated (INV_ABLATE)."""
        return [rid for rid, why in (self.report.get("not_scanned") or {}).items() if str(why).startswith("ablated")]

    def findings(self, rule: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        return [f for f in self.report.get("findings") or []
                if (rule is None or f.get("rule") == rule) and (kind is None or f.get("kind") == kind)]

    def failed(self, rule: str) -> bool:
        """True when the scan reported a bypass, forbidden or missing finding for the rule."""
        return (self.report.get("rules") or {}).get(rule, {}).get("result") == "fail"

    def passed(self, rule: str) -> bool:
        """True when the rule was scanned and nothing failed; no answer for an ablated rule."""
        if rule in self.ablated:
            self._no_verdict(f"inv scan skipped {rule} because it is ablated, so it did not pass")
        return (self.report.get("rules") or {}).get(rule, {}).get("result") == "pass"

    @property
    def code(self) -> int:
        """The exit code; no answer when it is 4 (an ablated enforce rule was skipped)."""
        if self.exit_code == SCAN_ABLATED:
            self._no_verdict(f"inv scan exited {SCAN_ABLATED}: it skipped ablated enforce rule(s) "
                             f"{', '.join(self.ablated)}, which is neither a pass nor a fail")
        return self.exit_code

    def _no_verdict(self, why: str) -> None:
        if self.session is not None and self.session.ablating is not None:
            self.session.no_verdict.append(why)
            pytest.skip(f"invariants: {why}; a falsifier asserts the rule's findings")
        pytest.fail(f"invariants: {why} (INV_ABLATE is set outside an --inv-ablate re-run)", pytrace=False)


@pytest.fixture
def inv_scan(request: pytest.FixtureRequest) -> Callable[..., ScanRun]:
    """inv_scan(rule_file="invariants.yaml", *roots, exclude=(), cwd=None) -> ScanRun.

    Runs `inv scan --format json` in a subprocess, the way CI runs it, so an ablation re-run's
    INV_ABLATE reaches it. Relative paths resolve against cwd (default: the pytest rootdir).
    """
    sess = request.config.stash.get(STASH, None)

    def scan(rule_file: str | os.PathLike[str] = "invariants.yaml", *roots: str | os.PathLike[str],
             exclude: Sequence[str] = (), cwd: str | os.PathLike[str] | None = None) -> ScanRun:
        args = [sys.executable, "-m", "invariants.cli", "scan", os.fspath(rule_file), "--format", "json"]
        args += [a for r in roots for a in ("--root", os.fspath(r))]
        args += [a for g in exclude for a in ("--exclude", g)]
        p = subprocess.run(args, cwd=cwd or request.config.rootpath, capture_output=True, text=True, timeout=600)
        try:
            report = json.loads(p.stdout)
        except ValueError:
            pytest.fail(f"inv scan exited {p.returncode} without a JSON report:\n{p.stdout}{p.stderr}", pytrace=False)
        return ScanRun(p.returncode, report, p.stderr, sess)
    return scan


# --- fixtures -------------------------------------------------------------------------------------
@pytest.fixture
def inv_registry(request: pytest.FixtureRequest) -> Iterator[Registry]:
    """A fresh registry that guards use for this test; it holds the session rule file if one is set."""
    sess = request.config.stash.get(STASH, None)
    rules = sess.load_rules() if sess is not None else None
    with isolated() as reg:
        if rules is not None:
            reg.install(rules)
        yield reg


@pytest.fixture
def inv_events() -> Iterator[list[dict[str, Any]]]:
    """Every event recorded during the test, in any registry (the sink itself is a bounded ring)."""
    with capture() as events:
        yield events


@pytest.fixture
def inv_ablated() -> Callable[..., contextlib.AbstractContextManager[None]]:
    """inv_ablated(rule_id) -> context manager that ablates the rule in the current registry."""
    def ablate(rule_id: str, reg: Registry | None = None) -> contextlib.AbstractContextManager[None]:
        target = reg or current()
        if rule_id not in target.rules:
            raise LoadError(f"inv_ablated: rule {rule_id!r} is not loaded in the current registry")
        return target.ablate(rule_id)
    return ablate
