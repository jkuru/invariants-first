"""Package surface: public API, version, CLI, entry points, schema bundled."""
from __future__ import annotations

import importlib.metadata
import re

import invariants as inv
from invariants import cli

GUIDE_API = ["provider", "guard", "model_call", "edge", "Checked", "verify", "standing", "Routed",
             "Escalation", "load", "check_snapshot"]
REMOVED_API = ["transaction", "RolledBack", "WiringError", "storage_rejection", "storage_errors"]   # below the API


def test_guide_api_is_exported():
    for name in GUIDE_API:
        assert hasattr(inv, name), name
    assert set(inv.__all__) <= set(dir(inv))
    assert not any(hasattr(inv, name) for name in REMOVED_API)


PUBLIC_API = {
    # guide section 4.4
    "provider", "guard", "model_call", "edge", "Checked", "verify", "standing", "load",
    "check_snapshot", "Escalation", "Routed",
    # the other outcomes and errors
    "InvariantError", "Rejected", "Halted", "LoadError", "CoverageError", "ParseError", "ReviewNotOpened",
    "BypassError",
    # loading, evaluation and records
    "parse", "read_document", "schema_path", "verify_coverage", "evaluate", "evaluate_env", "fill_view",
    "effective_mode", "Outcome", "Rule", "Provider",
    # registries
    "Registry", "current", "default_registry", "using", "isolated", "ablated", "is_ablated",
    # operations: hooks, hand-offs, test capture, out-of-call points, packaged rule files
    "configure", "observe", "capture", "Handoff", "ReviewStore", "MemoryReviews", "JsonlReviews", "ErrorSpike",
    "monitor", "load_resource", "Dependency",
    # CEL and metadata
    "cel", "CEL_BACKEND", "__version__",
}


def test_public_api_is_exactly_the_documented_names():
    import types
    assert set(inv.__all__) == PUBLIC_API and len(inv.__all__) == len(PUBLIC_API)
    public = {n for n, v in vars(inv).items()
              if not n.startswith("_") and not (isinstance(v, types.ModuleType) and n != "cel")}
    assert public == PUBLIC_API - {"__version__"}


def test_public_api_is_type_hinted():
    import inspect
    import typing
    for name in sorted(PUBLIC_API):
        obj = getattr(inv, name)
        if inspect.isfunction(obj):
            hints = typing.get_type_hints(obj)
            params = [p for p in inspect.signature(obj).parameters.values()
                      if p.kind not in (p.VAR_KEYWORD,)]
            assert "return" in hints, f"{name} has no return annotation"
            assert all(p.name in hints for p in params), f"{name} has unannotated parameters"


def test_py_typed_is_shipped():
    import pathlib
    assert (pathlib.Path(inv.__file__).parent / "py.typed").is_file()


# PEP 440's canonical public version (its Appendix B), without the packaging library: a release
# segment, then optional pre-, post- and dev-release parts.
PEP440 = re.compile(r"([1-9][0-9]*!)?(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*((a|b|rc)(0|[1-9][0-9]*))?"
                    r"(\.post(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?")


def test_version():
    assert inv.__version__ == importlib.metadata.version("invariants-first")
    assert PEP440.fullmatch(inv.__version__), inv.__version__
    for good in ("0.1", "1.0.0", "2!1.0rc1.post2.dev3"):
        assert PEP440.fullmatch(good)
    for bad in ("v0.1.0", "0.1.0-beta", "01.0", "0.1.0+local", ""):
        assert not PEP440.fullmatch(bad)


def test_cli_without_command_is_usage_error(capsys):
    assert cli.main([]) == 2
    assert "choose a command" in capsys.readouterr().err


def test_entry_points():
    eps = importlib.metadata.entry_points()
    assert any(e.name == "inv" and e.value == "invariants.cli:main" for e in eps.select(group="console_scripts"))
    assert any(e.name == "invariants" and e.value == "invariants.pytest_plugin" for e in eps.select(group="pytest11"))


def test_schema_is_bundled():
    assert inv.schema_path().is_file()


def test_library_does_not_import_example_apps():
    import pathlib
    src = pathlib.Path(inv.__file__).parent
    for f in src.rglob("*.py"):
        text = f.read_text()
        for app in ("import jobs", "import pool", "import triage"):
            assert app not in text, f"{f} imports an example app"
        assert "eval(" not in text.replace("evaluate(", "").replace("_eval(", "") and "exec(" not in text, f


def test_the_design_guide_and_its_monorepo_copy_are_identical():
    """docs/design-guide.md and the monorepo root's Invariant_first.md are one handbook in two places;
    edit docs/design-guide.md and copy it. (The root copy is absent from an sdist: nothing to compare.)"""
    import pathlib
    import pytest
    here = pathlib.Path(__file__).resolve().parents[1]
    copy = here.parent / "Invariant_first.md"
    if not copy.is_file():
        pytest.skip("not in the monorepo")
    assert copy.read_bytes() == (here / "docs" / "design-guide.md").read_bytes(), \
        "Invariant_first.md drifted from docs/design-guide.md: cp docs/design-guide.md ../Invariant_first.md"
