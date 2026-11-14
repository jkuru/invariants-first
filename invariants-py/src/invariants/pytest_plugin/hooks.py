"""The plugin module's own hooks: the --inv-* options, and the InvSession each run registers."""
from __future__ import annotations

import pytest

from .. import registry
from .session import STASH, InvSession


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("invariants", "invariants rule checks")
    group.addoption("--inv-rules", metavar="PATH", default=None,
                    help="rule file (JSON or YAML) to load into the default registry (default, with --inv-ablate "
                         "or --inv-verify: the inv_rules ini option, else invariants.yaml, .yml or .json in the rootdir)")
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
