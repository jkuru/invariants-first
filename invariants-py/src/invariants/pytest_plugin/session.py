"""InvSession: the plugin object registered per run, and its session-level hooks."""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Iterator, cast

import pytest

from ..errors import CoverageError
from ..loader import read_document
from ..registry import observe
from ..registry.coverage import rule_digest
from .report import _Report
from .state import NO_RULES


class InvSession(_Report):
    """Per-run state of the plugin, registered as a plugin so its hooks run."""

    # --- hooks --------------------------------------------------------------------------------
    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        if self.controller and self.ablate_on and not session.config.option.collectonly:
            self.controller_checks()
        if self.counting:
            self._unobserve = observe(self.count)

    def controller_checks(self) -> None:
        """pytest-xdist controller, before any worker starts: the usage errors of --inv-ablate that a
        worker would raise after collection for a rule file (a missing one, one with no rules) are
        raised here, so the run exits 4 (a usage error), not 3 (a worker's INTERNALERROR). The rule
        file is only read, not loaded: its python contracts may need the workers' collection. With no
        rule file the workers decide: rules the tests load themselves (a conftest's inv.load) are
        rules, as without xdist."""
        if self.rules_path is None:
            return
        if not self.rules_path.is_file():
            raise pytest.UsageError(f"--inv-rules: {self.rules_path} does not exist")
        try:
            doc = read_document(self.rules_path)
        except Exception:                      # the workers' load reports it, as without xdist
            return
        if isinstance(doc, dict) and not doc.get("rules"):
            raise pytest.UsageError(f"--inv-ablate: {NO_RULES.format(root=self._shown_root())}")

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        if self.load_rules() is not None:
            self.install_default()
        if self.doors_on and not session.config.option.collectonly:
            self.install_doors()
        reg = self.session_registry()
        self.statuses = {rid: r.status for rid, r in reg.rules.items()}
        if self.ablate_on:
            self.digests = {rid: rule_digest(r) for rid, r in reg.rules.items()}
        if self.ablate_on and not session.config.option.collectonly:
            self.plan_ablation(session, reg)
        if self.verify_on:
            if not reg.rules:
                self.verify_problems = [NO_RULES.format(root=self._shown_root())]
                return
            try:
                self.verify_count = reg.verify()
            except CoverageError as e:
                self.verify_problems = str(e).split("; ")

    # --inv-doors: the violations between a test's logstart and logfinish (its setup, call, teardown and
    # any --inv-ablate re-runs, which _Ablation.pytest_runtest_protocol brackets the same way) are its
    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        self.note_doors(None)                       # any from before it (outside a test)

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        self.note_doors(nodeid)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtestloop(self, session: pytest.Session) -> Iterator[Any]:
        try:
            return (yield)
        finally:
            self.counting = False
            self.note_doors(None)                       # any after the last test's protocol
            if self._unobserve is not None:
                self._unobserve()
                self._unobserve = None

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.worker:                             # the controller writes the report and decides
            cast(Any, self.config).workeroutput["invariants"] = self.worker_payload()
            return
        if self.controller and self.usage_problem:  # a worker's usage error: exit 4, as without xdist
            sys.stderr.write(f"ERROR: {self.usage_problem}\n")
            session.exitstatus = pytest.ExitCode.USAGE_ERROR
            return
        if self.controller and not self.workers_reported and (self.ablate_on or self.verify_on):
            self.ablation_notes.append("no pytest-xdist worker reported its invariants results")
            if self.verify_on and not self.verify_problems:
                self.verify_problems = ["no pytest-xdist worker reported its verify result"]
        if self.json_path:
            path = pathlib.Path(self.config.invocation_params.dir, self.json_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.report_data(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
        if self.doors_on:
            self._write_doors(tr)
        if self.report_on:
            self._write_report(tr)
        if self.json_path:
            tr.write_line(f"invariants report written to {self.json_path}")

    @property
    def failed(self) -> bool:
        no_worker = self.controller and self.ablate_on and not self.workers_reported
        return bool(self.verify_problems or self.not_load_bearing or (self.ablate_on and self.unverified)
                    or no_worker or self.doors_violations)


STASH: pytest.StashKey[InvSession] = pytest.StashKey()
