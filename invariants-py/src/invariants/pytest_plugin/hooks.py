"""The plugin module's own hooks: the --inv-* options, and the InvSession each run registers."""
from __future__ import annotations

import pytest

from .. import registry
from .session import STASH, Bypass, InvSession


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
    group.addoption("--inv-doors", action="store_true", default=False,
                    help="check every path law at run time (inv.doors.install, mode enforce): a sink reached without "
                         "its door raises inv.BypassError, and any such call fails the session, even a caught one")
    group.addoption("--inv-report", action="store_true", default=False,
                    help="print per-rule reach, violation and rule_error counts at session end")
    group.addoption("--inv-report-json", metavar="PATH", default=None,
                    help="write the per-rule counts (and verify/ablation results) as JSON")
    parser.addini("inv_rules", "rule file to load (relative to the ini file)", default=None)


def pytest_configure(config: pytest.Config) -> None:
    registry.pytest_session_started()      # env ablation logs quietly only inside a real session
    config.addinivalue_line("markers", "inv_bypass(*rule_ids, reason=''): under --inv-doors, the test reaches "
                            "these path laws' sinks without a door on purpose; those misses are expected")
    sess = InvSession(config)
    config.stash[STASH] = sess
    config.pluginmanager.register(sess, "invariants-session")
    config.pluginmanager.register(Bypass(sess), "invariants-bypass")


def pytest_unconfigure(config: pytest.Config) -> None:
    registry.pytest_session_ended()
    sess = config.stash.get(STASH, None)
    if sess is not None:
        sess.uninstall_doors()
        sess.restore_default()
