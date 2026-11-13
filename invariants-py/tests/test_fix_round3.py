"""Regression tests for fix round 3: each test failed before its fix."""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
from typing import Any

import pytest

import invariants as inv
import round3 as r3
from cli_support import run_inv
from support import raises

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def iso():
    with inv.isolated() as r:
        yield r


def load(*rules: dict[str, Any], **kw: Any) -> inv.Registry:
    return inv.load(r3.doc(*rules, **kw))


# --- major: a py: guard with no rule file loaded fails closed ---------------------------------------
def test_py_key_guard_with_nothing_loaded_fails_closed(iso):
    with pytest.raises(inv.LoadError, match="no rule file is loaded"):
        r3.pykey(50)


def test_py_key_guard_naming_an_undeclared_target_fails_closed(iso):
    load(r3.rule("P-1", "pure", "args.n > 10"), targets={})
    iso.targets.pop("py:round3.pykey")
    with pytest.raises(inv.LoadError, match="no loaded rule or target declares"):
        r3.pykey(1)


def test_py_key_guard_on_a_declared_target_without_rules_proceeds(iso):
    load(r3.rule("P-1", "pure", "args.n > 10"))
    assert r3.pykey(50) == 50


# --- major: ill-typed clauses fail at load (and in inv validate) -----------------------------------
@pytest.mark.parametrize("target,bad", [
    ("pure", 'args.n > "x"'),
    ("pure", 'args.n + "x" > 1'),
    ("pure", "!args.n"),
    ("pure", "args.n && true"),
    ("pure", "args.n + 1"),
    ("pure", "label > 1"),
    ("pure", 'maybe > "x"'),
    ("pure", "size(args.n) > 0"),
    ("pure", 'args.n.startsWith("a")'),
    ("pure", "1 in args.n"),
    ("pure", 'args.n == "x"'),               # fix round 4: equality across type families is refused
    ("sized", 'args.req.missing > "x"'),     # fix round 4: Req is a dataclass with no field missing
    ("sized", "args.req.size > args.req.pool"),
    ("sized", 'args.req.tags.exists(t, t > "a")'),
    ("sized", "args.req.tags.map(t, t + 1).exists(u, u < true)"),
])
def test_ill_typed_clause_is_a_load_error(iso, target, bad):
    with pytest.raises(inv.LoadError, match="ill-typed"):
        load(r3.rule("P-X", target, bad))


def test_ill_typed_post_effect_result_is_a_load_error(iso):
    with pytest.raises(inv.LoadError, match="ill-typed"):
        load(r3.rule("P-1", "pure", 'result > "x"', timing="post_effect"))


@pytest.mark.parametrize("target,bad", [
    ("pure", 'args.n == null'),              # null compares with anything
    ("pure", "args.n > 1.5"),                # int and double compare
    ("pure", "maybe > 1"),                   # nullable: fine when set
    ("sized", 'args.anything > "x"'),        # Any is dyn
    ("sized", "args.req.size > 1 && args.req.pool.startsWith(\"p\")"),
    ("sized", "inv.sum(args.req.tags) > args.req.size"),
    ("sized", 'args.anything.missing > "x"'),  # a field on dyn is dyn
])
def test_well_typed_or_unknown_clauses_load(iso, target, bad):
    load(r3.rule("P-X", target, bad))


def test_ill_typed_reply_field_is_a_load_error(iso):
    import fixround as fx
    with pytest.raises(inv.LoadError, match="ill-typed"):
        inv.load(fx.doc(fx.rule("M-2", "smodel", "reply.cause > 1", fx.enf("model_call", on_violation="escalate"),
                                fx.enf("edge", target="py:fixround.sconsume"), model_policy=fx.POLICY)))


def test_inv_validate_fails_on_an_ill_typed_clause(tmp_path):
    (tmp_path / "tapp.py").write_text(
        "import invariants as inv\n\n@inv.guard('P-1')\ndef f(n: int) -> int:\n    return n\n")
    (tmp_path / "bad.yaml").write_text(
        "spec: invariant/v0.1\ntargets:\n  py:tapp.f: {effects: none}\nrules:\n"
        "- id: P-1\n  status: detect\n  subject: {target: 'py:tapp.f'}\n  bad: 'args.n > \"x\"'\n"
        "  enforcers: [{point: decorator, timing: pre_effect, mode: detect}]\n"
        "  falsified_by: [tests/x.py]\n")
    r = run_inv("validate", tmp_path / "bad.yaml", cwd=tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ill-typed" in r.stdout + r.stderr


# --- minor: @inv.guard above @classmethod / @staticmethod ------------------------------------------
@pytest.mark.parametrize("name", ["cm", "cm2", "sm"])
def test_guard_works_in_either_order_with_classmethod_and_staticmethod(iso, name):
    rid = {"cm": "C-1", "cm2": "C-2", "sm": "C-3"}[name]
    load(r3.rule(rid, f"Typed.{name}", "args.n > 10"), targets={f"{r3.M}.Typed.{name}": {"effects": "none"}})
    fn = getattr(r3.Typed, name)
    assert fn(3) == 3 and getattr(r3.Typed(), name)(4) == 4
    assert raises(inv.Rejected, fn, 50) is not None
    assert iso.verify(["no-such-module"]) == 1          # the rule's target carries its guard


class C(inv.Checked): ...


def test_model_call_and_edge_accept_staticmethod():
    class Holder:
        @inv.model_call("M-9", schema={"v": int})
        @staticmethod
        def call(prompt: str) -> C:
            return '{"v": 1}'  # type: ignore[return-value]

        @inv.edge("M-9")
        @staticmethod
        def use(r: C) -> None: ...
    assert isinstance(Holder.__dict__["call"], staticmethod) and isinstance(Holder.__dict__["use"], staticmethod)


# --- major: the shipped typecheck job passes ---------------------------------------------------------
@pytest.mark.skipif(shutil.which("mypy") is None and not (pathlib.Path(sys.executable).parent / "mypy").exists(),
                    reason="mypy not installed (pip install -e '.[dev]')")
def test_mypy_is_clean():
    r = subprocess.run([sys.executable, "-m", "mypy"], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-4000:]


# --- minor: env ablation outside pytest logs at ERROR -----------------------------------------------
def test_env_ablation_outside_pytest_logs_an_error(monkeypatch, caplog):
    from invariants import registry
    monkeypatch.setenv("INV_ABLATE", "Z-1")
    monkeypatch.setenv("INV_ABLATE_TESTING", "1")
    monkeypatch.setattr(registry, "under_pytest", lambda: False)
    monkeypatch.setattr(registry._SESSION, "warned_armed", set())
    with caplog.at_level("INFO", logger="invariants"):
        assert registry.env_ablated() == {"Z-1"}
        with inv.isolated():
            inv.load({"spec": r3.SPEC, "rules": [{"id": "Z-1", "status": "candidate", "bad": "false"}]})
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("OFF in this process" in r.getMessage() for r in errors)
    assert any("Z-1" in r.getMessage() and "ablated by" in r.getMessage() for r in errors)


def test_env_ablation_under_pytest_stays_quiet(monkeypatch, caplog):
    from invariants import registry
    monkeypatch.setenv("INV_ABLATE", "Z-1")
    monkeypatch.setenv("INV_ABLATE_TESTING", "1")
    monkeypatch.setattr(registry._SESSION, "warned_armed", set())
    with caplog.at_level("INFO", logger="invariants"):
        assert registry.env_ablated() == {"Z-1"}
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


# --- minor: the integrations' public surface ---------------------------------------------------------
def test_fastapi_integration_reexports_the_problem_surface():
    pytest.importorskip("fastapi")
    from invariants.integrations import asgi, fastapi as inv_fastapi
    for name in ("PROBLEM_JSON", "StatusMap", "problem"):
        assert name in inv_fastapi.__all__ and getattr(inv_fastapi, name) is getattr(asgi, name)
    assert set(asgi.__all__) == {"PROBLEM_JSON", "StatusMap", "problem", "encode", "InvariantMiddleware"}
    assert all(hasattr(inv_fastapi, n) for n in inv_fastapi.__all__)


def test_readme_documents_exit_code_4_and_the_integration_surface():
    text = "\n".join(p.read_text(encoding="utf-8") for p in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))])
    assert "- 4: the scan skipped an `enforce` rule because it is ablated" in text
    assert "PROBLEM_JSON" in text and "## Performance" in text


# --- minor: open-source hygiene ----------------------------------------------------------------------
def test_gitignore_covers_build_artifacts():
    if not (ROOT / ".gitignore").exists():         # an unpacked archive without it (MANIFEST.in ships it)
        pytest.skip("no .gitignore next to the tests")
    lines = (ROOT / ".gitignore").read_text().split()
    for entry in ("build/", "dist/", ".mypy_cache/", ".pytest_cache/", "__pycache__/", "*.egg-info/"):
        assert entry in lines
