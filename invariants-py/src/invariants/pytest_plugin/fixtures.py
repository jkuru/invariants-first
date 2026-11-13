"""The fixtures: inv_scan (and the ScanRun it returns), inv_registry, inv_events, inv_ablated."""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import subprocess
import sys
from typing import Any, Callable, Iterator, Sequence

import pytest

from ..errors import LoadError
from ..registry import Registry, capture, current, isolated
from .session import STASH, InvSession

SCAN_ABLATED = 4            # `inv scan`'s exit code when it skipped an ablated enforce rule (cli.ABLATED)


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
