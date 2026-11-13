"""The loader, bootstrap and pytest_plugin packages keep the namespaces the single modules had."""
from __future__ import annotations

import types

import pytest

from invariants import bootstrap, loader, pytest_plugin
from invariants.pytest_plugin import ablation, fixtures, hooks, session

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("pkg", [loader, bootstrap, pytest_plugin], ids=lambda m: m.__name__)
def test_star_import_binds_the_old_public_names_and_no_module(pkg: types.ModuleType) -> None:
    ns: dict[str, object] = {}
    exec(f"from {pkg.__name__} import *", ns)
    names = set(ns) - {"__builtins__"}
    assert names == set(pkg.__all__)
    assert not [n for n in names if isinstance(ns[n], types.ModuleType)]


def test_classes_keep_their_module() -> None:
    for cls in (bootstrap.Unsupported, bootstrap._Cel, bootstrap.Guard, bootstrap.Model, bootstrap.Found):
        assert cls.__module__ == "invariants.bootstrap"
    for cls in (pytest_plugin.InvSession, pytest_plugin.ScanRun):
        assert cls.__module__ == "invariants.pytest_plugin"


def test_moved_names_are_the_same_objects() -> None:
    assert loader.schema_path().is_file()
    assert loader._type_env is loader.typebind._type_env and loader.load is loader.loading.load
    assert pytest_plugin.inv_scan is fixtures.inv_scan and pytest_plugin.pytest_configure is hooks.pytest_configure
    assert pytest_plugin.STASH is session.STASH and pytest_plugin.NEVER_FIRED is ablation.NEVER_FIRED


def test_the_plugin_module_carries_every_hook_and_fixture(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_x(inv_registry, inv_events, inv_ablated, inv_scan):\n    pass\n")
    result = pytester.runpytest("--inv-report")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*invariants report*"])
    impl = pytest_plugin.InvSession.pytest_runtest_protocol
    assert impl.pytest_impl["tryfirst"]  # type: ignore[attr-defined]
    assert pytest_plugin.InvSession.pytest_runtestloop.pytest_impl["wrapper"]  # type: ignore[attr-defined]
