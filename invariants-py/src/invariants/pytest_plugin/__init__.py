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
re-run without ablation, and on that control run the rule's enforcement must have decided
something (a violation at a guard, an edge, a middleware or a monitor; at a model call a
violation, a parse_error it retried or rejected, or an abstain; INV_DECISION_TRACE carries that
from this process or a subprocess). The ablated re-run must reach the rule (an enforcement point
or a scan, in this process or a subprocess, skipped it because it is ablated; INV_ABLATE_TRACE
carries that; asking `inv.is_ablated()` is not reaching it), and the test must pass again on a
second control re-run after the ablated one. Otherwise it is inconclusive. A rule that never
fired on the control run gives "inconclusive (the rule never fired on the control run ...)": the
test passes whether or not the rule holds, so whatever fails it under ablation is not the rule.
A falsifier whose ablated run reached the rule through `inv scan` keeps the scan's own check
(the scan's findings are what it asserts; there is no in-process decision to see).
A broader fixture lives across the re-runs, so state it carries (a module-scoped quota each run
spends) can fail a re-run without the ablation: a second control that fails makes the falsifier
"inconclusive (state carried between runs ...)", and a failing first control is inconclusive too;
both name the fixtures broader than the function the test used. It is also inconclusive ("rule_error, not violation") when, on the
control re-run in this process, the rule's evaluations gave rule_error and never a violation or
parse_error: the test passed because the rule could not be evaluated, not because it held.

The package is split by seam; this module is the one pytest registers (entry point pytest11, or
`-p invariants.pytest_plugin` with plugin autoload off), so it re-exports every hook and fixture,
and keeps the namespace the single pytest_plugin.py had:
- hooks.py: the --inv-* options, and pytest_configure / pytest_unconfigure registering an InvSession
- state.py, ablation.py, report.py, session.py: InvSession, one layer per module (its state and rule
  file; the ablation protocol; the report and the pytest-xdist payload; the session-level hooks)
- rerun.py: helpers of one falsifier's re-runs (outcome, broader fixtures, env and trace files)
- fixtures.py: inv_scan (and ScanRun), inv_registry, inv_events, inv_ablated
"""
from __future__ import annotations

# hooks and fixtures: pytest finds them on this module (pluggy and parsefactories read its namespace)
from .fixtures import SCAN_ABLATED, ScanRun, inv_ablated, inv_events, inv_registry, inv_scan  # noqa: F401
from .hooks import pytest_addoption, pytest_configure, pytest_unconfigure  # noqa: F401

from .ablation import NEVER_FIRED, NO_VERDICT, _Ablation  # noqa: F401
from .report import KINDS, _Report  # noqa: F401
from .rerun import (_broad_fixtures, _carried, _decided, _env_ablated, _env_decisions, _lines,  # noqa: F401
                    _matches, _outcome, _reached)
from .session import STASH, InvSession  # noqa: F401
from .state import _SessionState  # noqa: F401

# names pytest_plugin.py imported, kept so invariants.pytest_plugin.<name> still resolves (and the
# annotations of the classes below, whose module is this one, evaluate here)
from typing import Any, Callable, Iterator, Sequence, cast  # noqa: F401
import pytest  # noqa: F401
from .. import registry  # noqa: F401
from ..errors import CoverageError, LoadError  # noqa: F401
from ..loader import load  # noqa: F401
from ..registry import (ABLATE_ARM_ENV, ABLATE_ENV, ABLATE_TRACE_ENV, DECISION_TRACE_ENV, Registry,  # noqa: F401
                        ablated_everywhere, capture, current, default_registry, isolated, observe)

# `from invariants.pytest_plugin import *` exports exactly the public names pytest_plugin.py's star import
# gave (it had no __all__), and no modules: without this list it would bind the submodules over a caller's
# own names. The modules pytest_plugin.py leaked (contextlib, dataclasses, json, os, pathlib, pytest,
# registry, subprocess, sys, tempfile) are not exported.
__all__ = ["ABLATE_ARM_ENV", "ABLATE_ENV", "ABLATE_TRACE_ENV", "Any", "Callable", "CoverageError",
           "DECISION_TRACE_ENV", "InvSession", "Iterator", "KINDS", "LoadError", "NEVER_FIRED", "NO_VERDICT",
           "Registry", "SCAN_ABLATED", "STASH", "ScanRun", "Sequence", "ablated_everywhere", "annotations",
           "capture", "cast", "current", "default_registry", "inv_ablated", "inv_events", "inv_registry",
           "inv_scan", "isolated", "load", "observe", "pytest_addoption", "pytest_configure",
           "pytest_unconfigure"]

# Classes keep the module name they had before the split (reprs, pickles and pinned signatures name it).
for _cls in (InvSession, ScanRun):
    _cls.__module__ = "invariants.pytest_plugin"
del _cls
