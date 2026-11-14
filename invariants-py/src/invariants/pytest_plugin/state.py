"""The plugin's per-run state: the options, the rule file and the default registry it is installed in."""
from __future__ import annotations

import pathlib
from typing import Any, Callable

import pytest

from ..errors import LoadError
from ..loader import load
from ..registry import Registry, default_registry

DEFAULT_RULE_FILES = ("invariants.yaml", "invariants.yml", "invariants.json")   # found in the rootdir
NO_RULES = ("no rules loaded, so nothing was checked: pass --inv-rules PATH, set inv_rules in the pytest ini, "
            "or put invariants.yaml in the rootdir ({root})")


class _SessionState:
    """What every part of InvSession reads: the options, the rule file, the counts and the ablation plan."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        opt = config.getoption
        self.verify_on: bool = opt("inv_verify")
        self.ablate_on: bool = opt("inv_ablate")
        self.lenient: bool = opt("inv_ablate_lenient")
        self.report_on: bool = opt("inv_report")
        self.json_path: str | None = opt("inv_report_json")
        self.discovered = False                     # the rule file was found in the rootdir
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
        self.digests: dict[str, str] = {}           # every loaded rule's digest (registry.coverage.rule_digest)
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

    def _shown_root(self) -> str:
        """pytest's rootdir, relative to where pytest was run when it is under it."""
        try:
            rel = pathlib.Path(self.config.rootpath).resolve().relative_to(
                pathlib.Path(self.config.invocation_params.dir).resolve())
            return rel.as_posix() if rel.parts else "."
        except (ValueError, OSError):
            return str(self.config.rootpath)

    def _rules_path(self) -> pathlib.Path | None:
        """--inv-rules, else the inv_rules ini option, else (with --inv-ablate or --inv-verify, which
        need rules) the invariants.yaml, .yml or .json in pytest's rootdir."""
        cli = self.config.getoption("inv_rules")
        if cli:
            return pathlib.Path(self.config.invocation_params.dir, cli).resolve()
        ini = self.config.getini("inv_rules")
        if ini:
            base = self.config.inipath.parent if self.config.inipath else self.config.rootpath
            return pathlib.Path(base, ini).resolve()
        if self.ablate_on or self.verify_on:
            for name in DEFAULT_RULE_FILES:
                found = pathlib.Path(self.config.rootpath, name)
                if found.is_file():
                    self.discovered = True
                    return found.resolve()
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

    def count(self, rule_id: str, kind: str, event: dict[str, Any]) -> None:
        """Observer: count one evaluation in any registry while counting is on."""
        if self.counting:
            row = self.totals.setdefault(rule_id, {})
            if kind != "ablated":
                row["runs"] = row.get("runs", 0) + 1
            row[kind] = row.get(kind, 0) + 1
