"""The loader, bootstrap and pytest_plugin packages: their star exports, and the plugin module pytest loads."""
from __future__ import annotations

import types

import pytest

from invariants import bootstrap, loader, pytest_plugin

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("pkg", [loader, bootstrap, pytest_plugin], ids=lambda m: m.__name__)
def test_star_import_binds_the_public_names_and_no_module(pkg: types.ModuleType) -> None:
    ns: dict[str, object] = {}
    exec(f"from {pkg.__name__} import *", ns)
    names = set(ns) - {"__builtins__"}
    assert names == set(pkg.__all__)
    assert not [n for n in names if isinstance(ns[n], types.ModuleType)]


def test_the_plugin_module_carries_every_hook_and_fixture(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_x(inv_registry, inv_events, inv_ablated, inv_scan):\n    pass\n")
    result = pytester.runpytest("--inv-report")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*invariants report*"])
    impl = pytest_plugin.InvSession.pytest_runtest_protocol
    assert impl.pytest_impl["tryfirst"]  # type: ignore[attr-defined]
    assert pytest_plugin.InvSession.pytest_runtestloop.pytest_impl["wrapper"]  # type: ignore[attr-defined]
