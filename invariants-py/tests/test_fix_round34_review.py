"""Fix round 34: an external review. The CEL engine's items 3a-3c are in
packages/invariants-cel/tests/test_fix_round34_cel.py; this file has the others.

1. "invariants-schema-diff's mypy config lacks the cel_expr_python.* / google.* override, so mypy run
   in packages/invariants-schema-diff fails with cel-expr installed." Not reproduced: mypy there
   (cel-expr-python installed, fresh cache) is clean, because nothing invariants_schema_diff imports
   reaches cel_expr_python or google: invariants_cel imports them only in expr_python, which the
   library never imports. Pinned below from the import statements, the way mypy follows them.
2. "lock/pin_contracts.py's lazy import of loader.reading.check_expanded breaks the layering." Not
   reproduced: the intended direction (test_layering_packages.py, split phase 1) has the loader
   *below* the lock, which pins what it loads: the loader may not import the lock, and the lock may
   import the loader. Phase 1 injected cel_canon's type-environment builder because cel_canon moved
   into invariants-schema-diff, a library that imports nothing of invariants; pin_contracts stays in
   invariants. Pinned below.
3d. A pydantic model's @computed_field was readable by name (`p.z`) but missing from the model read
   whole (size(), ==, in, the macros), on both backends: _fields read model_fields only. A record
   is now the map of its fields and computed fields, as model_dump() has them.
4. The authors are settled (Claude, Gemini, Jerry Kuru; names only); the URLs are still example.com
   dummies, and the release guard still refuses on them, without relying on any email address.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tomllib

import pytest

from test_layering_packages import _lib_src, _module_file, _targets

import invariants
from invariants_cel import available_backends, get_backend

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = ROOT.parent
BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]


# ---- 1. what mypy reaches from invariants_schema_diff ----------------------------------------------

def _module_level_imports(path: pathlib.Path, src: pathlib.Path) -> set[str]:
    """The modules a file imports outside any function (mypy reads only these in an installed
    package, whose function bodies it skips)."""
    import ast
    tree = ast.parse(path.read_text())
    lazy = {id(n) for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
            for n in ast.walk(f)}
    lines = {n.lineno for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) and id(n) not in lazy}
    return {name for name, line in _targets(path, src) if line in lines}


def test_schema_diff_never_reaches_the_cel_expr_engine():
    """invariants_schema_diff imports nothing of cel_expr_python, google or invariants_cel.expr_python
    (at any depth), and invariants_cel reaches expr_python only from inside a function (_cel_expr),
    never at module level: so mypy, which follows an installed package's module-level imports only,
    never meets cel-expr from packages/invariants-schema-diff."""
    sd = _lib_src("invariants_schema_diff")
    for f in sd.rglob("*.py"):
        got = {n for n, _ in _targets(f, sd)}
        assert not {n for n in got if n.split(".")[0] in ("cel_expr_python", "google")
                    or n.startswith("invariants_cel.expr_python")}, f
    cel_src = _lib_src("invariants_cel")
    reached, todo = set(), [cel_src / "__init__.py"]
    while todo:
        f = todo.pop()
        if f in reached:
            continue
        reached.add(f)
        for name in _module_level_imports(f, cel_src):
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                hit = _module_file(".".join(parts[:i]), cel_src)
                if hit is not None:
                    todo.append(hit)
            assert parts[0] not in ("cel_expr_python", "google"), f"{f.name} imports {name} at module level"
    assert cel_src / "expr_python.py" not in reached and cel_src / "_engine.py" in reached


def test_the_cel_package_keeps_its_mypy_override():
    cfg = tomllib.loads((REPO / "packages" / "invariants-cel" / "pyproject.toml").read_text())
    mods = {m for o in cfg["tool"]["mypy"]["overrides"] for m in o["module"]}
    assert {"cel_expr_python.*", "google.*"} <= mods


@pytest.mark.skipif(shutil.which("mypy") is None and not __import__("importlib.util").util.find_spec("mypy"),
                    reason="mypy is not installed")
@pytest.mark.parametrize("lib", ["invariants-schema-diff"])
def test_mypy_is_clean_inside_the_library(lib, tmp_path):
    if __import__("importlib.util").util.find_spec("cel_expr_python") is None:
        pytest.skip("cel-expr-python is not installed: the reported setup needs it")
    r = subprocess.run([sys.executable, "-m", "mypy", "--cache-dir", str(tmp_path)], cwd=REPO / "packages" / lib,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


# ---- 2. the lock is above the loader -----------------------------------------------------------------

def test_the_lock_may_read_the_loader_and_not_the_other_way():
    import test_layering_packages as lp
    import test_layering_lock as ll
    assert "invariants.lock" in lp.FORBIDDEN["loader"]          # the loader never imports the lock
    assert "invariants.loader" not in ll.FORBIDDEN               # the lock may import the loader
    src = pathlib.Path(invariants.__file__).parent
    assert "invariants.loader.reading" in ll._imports(src / "lock" / "pin_contracts.py")


def test_an_openapi_yaml_alias_bomb_is_still_refused(tmp_path):
    from invariants.errors import LoadError
    from invariants.lock import pin_contracts
    f = tmp_path / "api.yaml"
    lines = ["a: &a [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 8):
        lines.append(f"{chr(97 + i)}: &{chr(97 + i)} [{', '.join(['*' + chr(96 + i)] * 10)}]")
    f.write_text("\n".join(lines) + "\n")
    with pytest.raises(LoadError, match="YAML aliases"):
        pin_contracts._openapi_file("api", f)


# ---- 3d. a pydantic model's computed fields are fields of the model read whole -----------------------

pydantic = pytest.importorskip("pydantic")


class Order(pydantic.BaseModel):
    qty: int = 2
    price: int = 5

    @pydantic.computed_field         # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return self.qty * self.price


class Boom(pydantic.BaseModel):
    a: int = 1

    @pydantic.computed_field         # type: ignore[prop-decorator]
    @property
    def bad(self) -> int:
        raise RuntimeError("no")


COMPUTED = [
    ("size(o) == 3", "violation"), ('o.exists(k, k == "total")', "violation"), ('"total" in o', "violation"),
    ('o == {"qty": 2, "price": 5, "total": 10}', "violation"), ('o == {"qty": 2, "price": 5}', "ok"),
    ("o.total == 10", "violation"), ('o["total"] == 10', "violation"), ("has(o.total)", "violation"),
    ('o.all(k, k in ["qty", "price", "total"])', "violation"),
]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr,want", COMPUTED)
def test_a_computed_field_is_a_field_of_the_record(backend, expr, want):
    from invariants_cel import CelError
    prog = get_backend(backend).compile(expr)
    try:
        got = "violation" if prog.evaluate({"o": Order()}) is True else "ok"
    except CelError as e:
        got = f"rule_error: {e}"
    assert got == want, expr
    assert Order().model_dump() == {"qty": 2, "price": 5, "total": 10}


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_computed_field_that_raises_is_a_rule_error(backend):
    from invariants_cel import CelError
    with pytest.raises(CelError):
        get_backend(backend).compile("size(o) == 2").evaluate({"o": Boom()})
    assert get_backend(backend).compile("o.a == 1").evaluate({"o": Boom()}) is True


# ---- 4. release metadata -------------------------------------------------------------------------------

PYPROJECTS = [ROOT / "pyproject.toml", *sorted((REPO / "packages").glob("*/pyproject.toml"))]
AUTHORS = [{"name": "Claude"}, {"name": "Gemini"}, {"name": "Jerry Kuru"}]


def test_the_authors_are_the_owners_choice_names_only():
    assert len(PYPROJECTS) == 4
    for p in PYPROJECTS:
        project = tomllib.loads(p.read_text())["project"]
        assert project["authors"] == AUTHORS, p
        assert "maintainers" not in project
    for f in (*PYPROJECTS, ROOT / "RELEASING.md", ROOT / "CHANGELOG.md", REPO / ".github" / "workflows" / "release.yml"):
        text = f.read_text()
        assert "Example Maintainer" not in text and "maintainer@example.com" not in text, f


def _guard_script() -> str:
    wf = (REPO / ".github" / "workflows" / "release.yml").read_text()
    m = re.search(r"- name: Refuse while the release blockers are open\n\s+run: \|\n((?:\s{10}.*\n)+)", wf)
    assert m, "release.yml has no release-blocker guard"
    return "\n".join(line[10:] for line in m.group(1).splitlines())


def _copy_for_guard(tmp: pathlib.Path) -> pathlib.Path:
    for p in PYPROJECTS:
        dst = tmp / p.relative_to(REPO)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(p.read_text())
    (tmp / "invariants-py" / "RELEASING.md").write_text((ROOT / "RELEASING.md").read_text())
    sarif = REPO / "packages" / "invariants-scan" / "src" / "invariants_scan" / "sarif.py"
    dst = tmp / sarif.relative_to(REPO)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(sarif.read_text())
    return tmp / "invariants-py"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("grep") is None, reason="needs bash and grep")
def test_the_release_guard_refuses_on_the_dummy_urls_alone(tmp_path):
    script = _guard_script()
    assert "email" not in script and "maintainer" not in script.lower()
    cwd = _copy_for_guard(tmp_path)
    run = lambda: subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True)  # noqa: E731
    assert run().returncode == 1                                  # today: blockers open, URLs dummy
    releasing = cwd / "RELEASING.md"
    releasing.write_text(releasing.read_text().replace("\n## Before publishing\n", "\n## Done before publishing\n"))
    r = run()
    assert r.returncode == 1 and "example.com" in r.stderr     # the dummy URLs alone still refuse
    for p in [cwd / "pyproject.toml", *sorted((tmp_path / "packages").glob("*/pyproject.toml")),
              tmp_path / "packages" / "invariants-scan" / "src" / "invariants_scan" / "sarif.py"]:
        p.write_text(p.read_text().replace("example.com", "project.invalid"))
    assert run().returncode == 0                                  # names-only authors never trip it
