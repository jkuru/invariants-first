"""The edges cut so the CEL engine, the scan and the schema-diff core import nothing else of invariants
(test_layering_packages.py) keep their behaviour: what a lower layer reads from a higher one is wired
by the package or passed in by the caller."""
from __future__ import annotations

import dataclasses

import pytest

import invariants
from invariants_scan import hooks as _scan_hooks
from invariants_cel import typeinfo as _typeinfo
from invariants_cel import typecheck
from invariants import confinement, lock
from invariants.errors import LoadError
from invariants.loader import _type_env, binding_problem, schemas
from invariants_schema_diff import cel_canon
from invariants.registry import coverage


@dataclasses.dataclass
class _Broken:
    x: "NoSuchType"  # type: ignore[name-defined]  # noqa: F821


def test_an_unresolvable_annotation_is_still_a_load_error_when_pinned() -> None:
    with pytest.raises(_typeinfo.TypeCheckError) as raw:
        _typeinfo.class_shape(_Broken)
    assert typecheck.TypeCheckError is _typeinfo.TypeCheckError
    assert not isinstance(raw.value, LoadError)
    for pin in (lock.resolved_schema, lock.stdlib_json_schema):
        with pytest.raises(LoadError) as e:
            pin(_Broken)
        assert str(e.value) == str(raw.value)
        assert str(e.value).startswith("schema _Broken: cannot resolve its annotations (")
        assert isinstance(e.value.__cause__, NameError)


def test_the_package_wires_what_the_lower_layers_read() -> None:
    assert _scan_hooks.confine_root is confinement.confine_root
    assert coverage.binding_problem is binding_problem
    assert schemas.pydantic_closed is invariants._pydantic_closed


def test_the_scan_reads_the_confinement_root_through_its_hook(tmp_path, monkeypatch) -> None:
    from invariants_scan import sources
    seen = []
    monkeypatch.setattr(_scan_hooks, "confine_root", lambda: seen.append(1))
    list(sources.iter_sources([tmp_path], []))
    assert seen


def test_clause_types_reads_the_type_environments_it_is_given() -> None:
    reg = invariants.load({"rules": []})
    assert cel_canon.clause_types(reg, _type_env) == {}
    calls = []

    class _Rule:
        names_for = {0: {"args": "args"}}

    class _Reg:
        rules = {"R-1": _Rule()}

    def env(rule, reg, i, names):
        calls.append((i, names))
        return {"args": typecheck.from_py(int)}
    typer = cel_canon.clause_types(_Reg(), env)["R-1"]
    assert calls == [(0, {"args": "args"})] and typer is not None
