"""The scan inside invariants-first: it is the invariants-scan library (invariants_scan), which imports
nothing of invariants (test_layering_packages.py). It reads ablation and the confinement root through
invariants_scan.hooks, which the package wires; `invariants.scan` re-exports its public API.
"""
import invariants_scan
from invariants_scan import hooks

import invariants
import invariants.scan


def test_ablation_hooks_are_wired_by_the_package():
    assert hooks.ablated is not hooks._none and hooks.reached is not hooks._ignore
    assert hooks.confine_root is invariants.confinement.confine_root
    assert invariants._registry is invariants.registry


def test_invariants_scan_is_the_librarys_public_api():
    assert invariants.scan.__all__ == invariants_scan.__all__
    for name in invariants_scan.__all__:
        assert getattr(invariants.scan, name) is getattr(invariants_scan, name), name


def test_the_scan_librarys_tests_exclude_what_inv_scan_excludes():
    """packages/invariants-scan/tests scan with the excludes `inv scan` passes by default."""
    import importlib.util
    import pathlib

    from invariants import lock
    path = pathlib.Path(__file__).resolve().parents[2] / "packages" / "invariants-scan" / "tests" / "scan_support.py"
    spec = importlib.util.spec_from_file_location("_scan_support", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DEFAULT_EXCLUDE == lock.DEFAULT_SCAN_EXCLUDE
