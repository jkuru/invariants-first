"""Round 27 (lock and confinement): findings from an external review.

1. schema diff: a required list reordered (or a duplicate dropped) is no weakening; a removed type is;
2. inv status: a door-only rule never needs ablation, with or without --ablation;
3. confinement: every module:attr import goes through confinement.import_module, a namespace
   package of the project is a project package, the lock file is read confined, and a git ref
   that looks like an option is refused;
4. a pinned annotation with no recognised form is pinned without its repr;
5. the release blockers and the test that pins them flip together.
"""
from __future__ import annotations

import json
import pathlib
import ast
import subprocess
import textwrap

import pytest

from cli_support import run_inv, write_project
from invariants import lock
from invariants.cli import git_base
from invariants.confinement import CONFINE_ENV
from invariants.errors import LoadError
from invariants.lock.contracts import _json_paths
from invariants.lock.schema_diff import constraint_weakening
from test_fix_round26_confine import _mcp


def _weakenings(old: dict, new: dict) -> list[tuple[str, str | None]]:
    o, n = {"pydantic": old}, {"pydantic": new}
    return [(p, constraint_weakening(p, a, b, o, n)) for p, a, b in _json_paths(o, n)]


# --- 1. schema diff ------------------------------------------------------------------------------
def test_required_reordered_or_deduplicated_is_not_weakened():
    assert all(w is None for _, w in _weakenings({"required": ["b", "a"]}, {"required": ["a", "b"]}))
    assert all(w is None for _, w in _weakenings({"required": ["a", "a"]}, {"required": ["a"]}))
    assert all(w is None for _, w in _weakenings({"required": ["a"]}, {"required": ["a", "a"]}))


def test_required_added_tightens_and_removed_weakens():
    assert all(w is None for _, w in _weakenings({"required": ["a"]}, {"required": ["a", "b"]}))
    assert all(w is None for _, w in _weakenings({}, {"required": ["a"]}))
    for old, new in (({"required": ["a", "b"]}, {"required": ["a"]}), ({"required": ["a"]}, {}),
                     ({"required": ["a", "c"]}, {"required": ["a", "b"]}),
                     ({"required": ["b", "a", "c"]}, {"required": ["a", "b"]})):
        assert any(w for _, w in _weakenings(old, new)), (old, new)


def test_type_removed_or_widened_weakens_and_narrowed_does_not():
    prop = lambda s: {"type": "object", "properties": {"a": s}}                      # noqa: E731
    assert any(w for _, w in _weakenings(prop({"type": "string"}), prop({})))        # any JSON type passes
    assert any(w for _, w in _weakenings(prop({"type": "string"}), prop({"type": ["string", "null"]})))
    assert all(w is None for _, w in _weakenings(prop({"type": ["string", "null"]}), prop({"type": "string"})))
    assert all(w is None for _, w in _weakenings(prop({"type": ["string", "null"]}), prop({"type": ["string"]})))
    assert all(w is None for _, w in _weakenings(prop({}), prop({"type": "string"})))


# --- 2. inv status: door-only rules ---------------------------------------------------------------
_SERVICE = """
import invariants as inv

class BillingService:
    @inv.guard("REFUND")
    def refund(self, charge_id: str, amount: int) -> None:
        pass
"""
_RULES = """
spec: invariant/v0.1
rules:
  - id: REFUND
    guard: "py:PKG.service.BillingService.refund"
    bad: [{when: "args.amount <= 0", message: "refund must be positive"}]
    tests: ["tests/test_refund.py::test_refuses"]
  - id: REFUND-DOOR
    door: "py:PKG.service.BillingService.refund"
    sink: "py:PKG.repo.RefundRepo.save_refund"
    mediation: {chokepoint: "refunds are saved only through BillingService.refund"}
"""


def _door_project(tmp_path, pkg):
    return write_project(tmp_path / "p", {
        f"{pkg}/__init__.py": "",
        f"{pkg}/repo.py": "class RefundRepo:\n    def save_refund(self, a: int) -> None:\n        pass\n",
        f"{pkg}/service.py": _SERVICE,
        "invariants.yaml": textwrap.dedent(_RULES.replace("PKG", pkg))})


def test_door_only_rule_is_never_unproven_for_want_of_ablation(tmp_path):
    root = _door_project(tmp_path, "bill27s")
    rows = {x["id"]: x for x in json.loads(run_inv("status", "--json", cwd=root).stdout)["rules"]}
    door = rows["REFUND-DOOR"]
    assert door["ablation"] == "not needed"
    assert door["standing"] == "held", door
    assert door["ablation_proven"] is False
    assert not any("ablation" in r for r in door["reasons"]), door["reasons"]
    text = run_inv("status", cwd=root).stdout
    door_line = next(line for line in text.splitlines() if line.startswith("REFUND-DOOR"))
    assert "unproven" not in door_line, text


def test_ablation_report_does_not_downgrade_a_door_only_rule(tmp_path):
    root = _door_project(tmp_path, "bill27t")
    (root / "abl.json").write_text(json.dumps({"ablation": {"rules": {}}}))
    r = run_inv("status", "--json", "--ablation", "abl.json", cwd=root)
    rows = {x["id"]: x for x in json.loads(r.stdout)["rules"]}
    door = rows["REFUND-DOOR"]
    assert door["standing"] == "held" and door["ablation"] == "not needed", door
    assert not any("ablation" in x for x in door["reasons"]), door["reasons"]
    assert rows["REFUND"]["standing"] == "enforce(known-callers)"     # a guard rule is still held to its ablation
    assert any(x.startswith("ablation:") for x in rows["REFUND"]["reasons"])


# --- 3. confinement -------------------------------------------------------------------------------
def test_pydantic_contract_ref_is_confined(tmp_path, monkeypatch):
    from invariants.integrations.pydantic import resolve_ref
    from invariants.lock.pin_contracts import _python_ref
    monkeypatch.setenv(CONFINE_ENV, str(tmp_path))
    for call in (lambda: resolve_ref("json:dumps"), lambda: _python_ref("c", "json:dumps")):
        with pytest.raises(LoadError, match="confines imports"):
            call()
    monkeypatch.delenv(CONFINE_ENV)
    assert resolve_ref("json:dumps") is json.dumps and _python_ref("c", "json:dumps") is json.dumps
    with pytest.raises(LoadError, match="has no '_default_encoder'"):
        resolve_ref("json:_default_encoder")                   # a pydantic ref names a public attribute
    assert _python_ref("c", "json:_default_encoder") is json._default_encoder    # type: ignore[attr-defined]


def test_no_unconfined_import_of_a_name_from_a_rule_file():
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "invariants"
    def imports(node: ast.AST) -> bool:
        f = node.func if isinstance(node, ast.Call) else None
        return (isinstance(f, ast.Name) and f.id == "__import__") or (
            isinstance(f, ast.Attribute) and f.attr == "import_module"
            and isinstance(f.value, ast.Name) and f.value.id == "importlib")
    found = {p.relative_to(src).as_posix() for p in src.rglob("*.py")
             if any(imports(n) for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))))}
    # confinement itself; otel and anthropic import a fixed name of their own optional dependency
    assert found == {"confinement.py", "integrations/otel.py", "integrations/anthropic.py"}, found


def test_namespace_package_of_the_project_is_confined(tmp_path):
    marker = tmp_path / "RAN_OUTSIDE"
    out = tmp_path / "outside"
    (out / "nsapp27").mkdir(parents=True)
    (out / "nsapp27" / "evil.py").write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n")
    proj = write_project(tmp_path / "proj", {
        "nsapp27/svc.py": f"import sys\nsys.path.append({str(out)!r})\nimport nsapp27.evil\n\n\n"
                          f"def refund(amount):\n    return amount\n",
        "invariants.yaml": "spec: invariant/v0.1\nrules:\n  - id: PAY-1\n    guard: py:nsapp27.svc.refund\n"
                           "    bad: 'args.amount > 100'\n"})
    text = _mcp("check", proj)
    assert not marker.exists(), text
    assert "module nsapp27.evil resolves outside" in text, text
    r = run_inv("check", cwd=proj)                             # plain inv still follows it
    assert marker.exists(), r.stdout + r.stderr


def test_namespace_package_inside_root_still_loads(tmp_path):
    proj = write_project(tmp_path / "proj", {
        "nsok27/inner.py": "Y = 2\n",
        "nsok27/svc.py": "from nsok27 import inner\n\n\ndef refund(amount):\n    return amount + inner.Y\n",
        "invariants.yaml": "spec: invariant/v0.1\nrules:\n  - id: PAY-1\n    guard: py:nsok27.svc.refund\n"
                           "    bad: 'args.amount > 100'\n"})
    text = _mcp("check", proj)
    steps = {s["step"]: s["result"] for s in json.loads(text)["steps"]}
    assert steps["rules"] == "ok" and "resolves outside" not in text, text


def test_symlinked_lock_is_not_read_outside_the_root(tmp_path, monkeypatch):
    secret = tmp_path / "outside.json"
    secret.write_text('{"lock_version": 1, "SECRET27": true}')
    root = tmp_path / "root"
    root.mkdir()
    (root / "invariants.lock").symlink_to(secret)
    monkeypatch.setenv(CONFINE_ENV, str(root))
    with pytest.raises(lock.LockError, match="resolves outside") as e:
        lock.read(root / "invariants.lock")
    assert "SECRET27" not in str(e.value)
    monkeypatch.delenv(CONFINE_ENV)
    with pytest.raises(lock.LockVersionError):                 # unconfined: read, as before
        lock.read(root / "invariants.lock")


def test_base_ref_that_looks_like_an_option_reaches_only_rev_parse(tmp_path, monkeypatch):
    """Not an injection: the ref reaches git only as `<ref>^{commit}` under rev-parse --verify, which
    takes no option as the one revision it verifies, so a ref starting with - is "not found" there and
    never reaches ls-tree or show."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    (tmp_path / "invariants.lock").write_text("{}")
    seen: list[tuple[str, ...]] = []
    real = git_base._git

    def spy(cwd, *args):
        seen.append(args)
        return real(cwd, *args)
    monkeypatch.setattr(git_base, "_git", spy)
    for ref in ("--output=" + str(tmp_path / "x"), "--all", "-q", "--end-of-options", "--git-dir=/", "-"):
        with pytest.raises(git_base.BaseUnavailable, match="not found"):
            git_base._base_lock(tmp_path / "invariants.lock", ref)
    assert {a[0] for a in seen} == {"rev-parse"}, seen
    assert not (tmp_path / "x").exists()


# --- 4. an annotation object with no recognised form ---------------------------------------------
class _Marker:
    pass


class _Holder:
    def __repr__(self) -> str:
        return "Holder(token='sk-live-PLANTED27')"


def test_annotation_object_is_pinned_by_its_type_not_its_repr():
    from invariants.lock.pin_signatures import signature_types
    from invariants.lock.pin_types import Schemas

    def f(a: _Marker(), b: _Holder()) -> None:              # type: ignore[valid-type]
        pass
    got = signature_types(f, Schemas(inline=True))
    text = json.dumps(got)
    assert "0x" not in text and "PLANTED27" not in text, text
    assert got["a"] == {"object": f"{__name__}._Marker"} and got["b"] == {"object": f"{__name__}._Holder"}
    assert signature_types(f, Schemas(inline=True)) == got     # the same pin every time


def test_typing_forms_and_callable_parameters_pin_as_before():
    import collections.abc
    import typing

    from invariants.lock.pin_types import _type_schema
    assert _type_schema(typing.Never, {}, set()) == "typing.Never"
    assert _type_schema(collections.abc.Callable[..., int], {}, set()) == {"generic": "Callable",
                                                                          "args": ["Ellipsis", "int"]}
    assert _type_schema(collections.abc.Callable[[int, list[str]], int], {}, set()) == {
        "generic": "Callable", "args": ["[<class 'int'>, list[str]]", "int"]}
    holder = collections.abc.Callable[[int, _Holder()], int]   # type: ignore[misc]
    assert _type_schema(holder, {}, set())["args"][0] == f'[<class \'int\'>, {{"object": "{__name__}._Holder"}}]'
