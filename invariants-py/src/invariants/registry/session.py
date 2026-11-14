"""Process-wide runtime state: pytest sessions, warn-once flags and the trace files the plugin reads.

One ProcessSession per process (registry._SESSION). Every read and write takes its lock.

After os.fork the child keeps the parent's values (a child of a pytest process is still inside
that session, and a warning the parent gave is not repeated), but gets a fresh lock: a lock held
by another parent thread at the moment of the fork would otherwise never be released in the child.
"""
from __future__ import annotations

import os
import threading
from typing import Any

ABLATE_ENV = "INV_ABLATE"
ABLATE_ARM_ENV = "INV_ABLATE_TESTING"     # INV_ABLATE is honoured only while this is "1" (the pytest plugin)
ABLATE_TRACE_ENV = "INV_ABLATE_TRACE"     # file the plugin reads: rule ids whose ablation was reached
DECISION_TRACE_ENV = "INV_DECISION_TRACE"  # file the plugin reads: rule ids whose enforcement decided something


class ProcessSession:
    """The process-level state the registry keeps outside any one registry, behind one lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.pytest_sessions = 0              # pytest sessions configured in this process (the plugin counts them)
        self.warned_unarmed = False           # the "INV_ABLATE is ignored" warning was given
        self.warned_armed: set[str] = set()   # rule sets the "guards are OFF" error was given for
        self.noted: set[tuple[str, str]] = set()   # (trace file, line) already written

    def _after_fork_in_child(self) -> None:
        self._lock = threading.Lock()

    def started(self) -> None:
        with self._lock:
            self.pytest_sessions += 1

    def ended(self) -> None:
        with self._lock:
            self.pytest_sessions = max(0, self.pytest_sessions - 1)

    def active(self) -> bool:
        with self._lock:
            return self.pytest_sessions > 0

    def first_unarmed(self) -> bool:
        """True once: the caller gives the unarmed-ablation warning."""
        with self._lock:
            first, self.warned_unarmed = not self.warned_unarmed, True
            return first

    def first_armed(self, key: str) -> bool:
        """True once per rule set key: the caller logs that those guards are off."""
        with self._lock:
            if key in self.warned_armed:
                return False
            self.warned_armed.add(key)
            return True

    def trace(self, path: str, line: str) -> None:
        """Append line to path once (per process); an OSError leaves it unnoted, to be tried again."""
        with self._lock:
            if (path, line) in self.noted:
                return
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self.noted.add((path, line))
            except OSError:
                pass


_SESSION = ProcessSession()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_SESSION._after_fork_in_child)


def pytest_session_started() -> None:
    """Called by the pytest plugin's pytest_configure: this process is running a test session."""
    _SESSION.started()


def pytest_session_ended() -> None:
    """Called by the pytest plugin's pytest_unconfigure."""
    _SESSION.ended()


def under_pytest() -> bool:
    """True while this process runs a pytest session with the invariants plugin configured.

    Neither `import pytest` (a dependency may do it in production) nor PYTEST_CURRENT_TEST
    (inherited or settable) is proof: only the plugin's pytest_configure counts.
    """
    return _SESSION.active()


def _trace(env: str, line: str) -> None:
    """Append line to the file named by env (once per file and line); nothing when env is unset."""
    path = os.environ.get(env)
    if path:
        _SESSION.trace(path, line)


def note_ablation_reached(rule_id: str, via: str = "enforcer") -> None:
    """Note in the INV_ABLATE_TRACE file that an enforcement point skipped rule_id because it is ablated.

    via is "enforcer" (a guard, an edge, a model call, a monitor) or "scan" (`inv scan` skipped it).
    The pytest plugin sets INV_ABLATE_TRACE for each ablated re-run, so it can tell a falsifier that
    failed because the rule was off from one that failed for an unrelated reason. Subprocesses
    inherit the variable, so an app or `inv scan` they start reports reach too. Asking
    `inv.is_ablated()` is not reaching the rule: only the library's own enforcement points note it.
    """
    _trace(ABLATE_TRACE_ENV, f"{rule_id} {via}")


def note_decision(rule_id: str) -> None:
    """Note in the INV_DECISION_TRACE file that rule_id's enforcement decided something.

    A decision is a violation, a parse_error the enforcer acted on (a retry or a rejection, not a
    detect-mode proceed), or a model call's abstain. The pytest plugin sets INV_DECISION_TRACE for a
    falsifier's control re-run: a falsifier whose rule never decided anything there passes whether
    or not the rule holds, so its failure under ablation proves nothing.
    """
    _trace(DECISION_TRACE_ENV, rule_id)


def is_decision(kind: str, event: dict[str, Any]) -> bool:
    """True for an event that is a decision of its rule (see note_decision)."""
    return kind == "violation" or (kind in ("parse_error", "abstain") and event.get("action") not in (None, "proceed"))
