"""InvSession: the plugin object registered per run, and its session-level hooks."""
from __future__ import annotations

import json
import pathlib
from typing import Any, Iterator, cast

import pytest

from ..errors import CoverageError
from ..registry import observe
from .report import _Report


class InvSession(_Report):
    """Per-run state of the plugin, registered as a plugin so its hooks run."""

    # --- hooks --------------------------------------------------------------------------------
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
        if self.report_on:
            self._write_report(tr)
        if self.json_path:
            tr.write_line(f"invariants report written to {self.json_path}")

    @property
    def failed(self) -> bool:
        no_worker = self.controller and self.ablate_on and not self.workers_reported
        return bool(self.verify_problems or self.not_load_bearing or (self.ablate_on and self.unverified)
                    or no_worker)


STASH: pytest.StashKey[InvSession] = pytest.StashKey()
