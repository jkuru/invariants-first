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
inv_events (every event recorded during the test, in any registry).

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
import json
import os
import pathlib
import tempfile
from typing import Any, Callable, Iterator

import pytest

from .errors import CoverageError, LoadError
from . import registry
from .loader import load
from .registry import ABLATE_ARM_ENV, ABLATE_ENV, ABLATE_TRACE_ENV, Registry, ablated_everywhere, capture, current, default_registry, isolated, observe

KINDS = ("ok", "violation", "rule_error", "parse_error")
STASH: pytest.StashKey[InvSession] = pytest.StashKey()


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
        self.ablation: dict[str, list[dict[str, str]]] = {}
        self.ablation_notes: list[str] = []
        self.counting = self.report_on or bool(self.json_path)
        self.totals: dict[str, dict[str, int]] = {}
        self._unobserve: Callable[[], None] | None = None
        self._main: dict[str, list[pytest.TestReport]] = {}

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
        if self.verify_on:
            reg = self.session_registry()
            if not reg.rules:
                self.verify_problems = ["no rules loaded: pass --inv-rules PATH (or set inv_rules)"]
                return
            try:
                self.verify_count = reg.verify()
            except CoverageError as e:
                self.verify_problems = str(e).split("; ")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._main.setdefault(report.nodeid, []).append(report)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtestloop(self, session: pytest.Session) -> Iterator[Any]:
        result = yield
        self.counting = False                       # ablation re-runs are not counted
        if self._unobserve is not None:
            self._unobserve()
            self._unobserve = None
        if self.ablate_on and not session.config.option.collectonly:
            self.run_ablation(session)
        return result

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
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

    # --- ablation -----------------------------------------------------------------------------
    def run_ablation(self, session: pytest.Session) -> None:
        """For each rule with falsifiers, re-run them with the rule ablated; each must fail."""
        from _pytest.runner import runtestprotocol      # the documented way plugins re-run an item

        reg = self.session_registry()
        if not reg.rules:
            self.ablation_notes.append("no rules loaded: pass --inv-rules PATH (or set inv_rules)")
            return
        for rule in reg.rules.values():
            falsifiers = rule.raw.get("falsified_by") or []
            if rule.status == "retired" or not falsifiers:
                continue
            rows = self.ablation.setdefault(rule.id, [])
            for f in falsifiers:
                items = [i for i in session.items if _matches(i.nodeid, f)]
                if not items:
                    rows.append({"falsifier": f, "result": "not collected"})
                for item in items:
                    main = _outcome(self._main.get(item.nodeid, []))
                    if main != "passed":
                        rows.append({"falsifier": item.nodeid, "result": f"not checked ({main} with the guard on)"})
                        continue
                    with capture() as events:
                        control = _outcome(runtestprotocol(item, nextitem=None, log=False))
                    kinds = {e.get("outcome") for e in events if e.get("rule") == rule.id}
                    if control != "passed":         # it fails without the ablation: not evidence
                        rows.append({"falsifier": item.nodeid,
                                     "result": f"inconclusive ({control} on a re-run without ablation)"})
                        continue
                    with tempfile.TemporaryDirectory() as d:
                        trace = os.path.join(d, "reached")
                        with ablated_everywhere(rule.id), _env_ablated(rule.id, trace):
                            out = _outcome(runtestprotocol(item, nextitem=None, log=False))
                        reached = _reached(trace, rule.id)
                    if out == "failed" and "rule_error" in kinds and not kinds & {"violation", "parse_error"}:
                        # with the guard on, the test passed because the rule errored (a typo, a missing
                        # key), not because it found the violation: a fail_closed error rejects too
                        result = "inconclusive (rule_error, not violation)"
                    elif out == "failed":
                        result = "caught" if reached else "inconclusive (rule not reached)"
                    else:
                        result = "still passes" if out == "passed" else f"inconclusive ({out})"
                    rows.append({"falsifier": item.nodeid, "result": result})

    @property
    def not_load_bearing(self) -> list[str]:
        return [rid for rid, rows in self.ablation.items() if any(r["result"] == "still passes" for r in rows)]

    @property
    def unverified(self) -> list[str]:
        """enforce rules with no caught falsifier (not collected, failing with the guard on, inconclusive)."""
        if self.lenient:
            return []
        reg = self.session_registry()
        return [rid for rid, rows in self.ablation.items()
                if rid in reg.rules and reg.rules[rid].status == "enforce" and rid not in self.not_load_bearing
                and not any(r["result"] == "caught" for r in rows)]

    @property
    def failed(self) -> bool:
        return bool(self.verify_problems or self.not_load_bearing or (self.ablate_on and self.unverified))

    def _write_ablation(self, tr: Any) -> None:
        bad, unverified = self.not_load_bearing, self.unverified
        if bad:
            tr.write_sep("=", f"invariants ablation FAILED: guard not load-bearing for {', '.join(bad)}",
                         red=True, bold=True)
        if unverified:
            tr.write_sep("=", f"invariants ablation FAILED: no falsifier caught the ablation of "
                              f"{', '.join(unverified)} (status enforce needs one)", red=True, bold=True)
        if not bad and not unverified:
            tr.write_sep("-", "invariants ablation", green=bool(self.ablation))
        for note in self.ablation_notes:
            tr.write_line(f"  {note}")
        for rid, rows in self.ablation.items():
            verdict = "guard not load-bearing" if rid in bad else (
                "load-bearing" if any(r["result"] == "caught" for r in rows) else "not checked")
            tr.write_line(f"  {rid}: {verdict}")
            for r in rows:
                tr.write_line(f"      {r['result']:<14} {r['falsifier']}")

    # --- report -------------------------------------------------------------------------------
    def report_data(self) -> dict[str, Any]:
        reg = self.session_registry()
        ids = list(reg.rules) + sorted(set(self.totals) - set(reg.rules))
        rules = {}
        for rid in ids:
            c = self.totals.get(rid, {})
            rule = reg.rules.get(rid)
            rules[rid] = {"status": rule.status if rule else None, "reach": c.get("runs", 0),
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
