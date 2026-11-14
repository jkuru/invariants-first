"""Split phase 2: invariants-first is four distributions. The CEL engine (invariants-cel), the scan
(invariants-scan) and the schema-diff judges (invariants-schema-diff) are libraries under packages/;
invariants imports them by their own names and keeps its documented public paths."""
from __future__ import annotations

import pathlib
import tomllib

import invariants_cel
import invariants_scan
import invariants_schema_diff

import invariants as inv
import invariants.cel
import invariants.lock
import invariants.scan
from invariants.cli import common

REPO = pathlib.Path(__file__).resolve().parents[2]
LIBS = ("invariants-cel", "invariants-scan", "invariants-schema-diff")


def test_the_documented_public_paths_still_work() -> None:
    assert "cel" in inv.__all__ and inv.cel is invariants.cel
    assert invariants.cel.__all__ == invariants_cel.__all__
    for name in invariants_cel.__all__:
        assert getattr(invariants.cel, name) is getattr(invariants_cel, name), name
    assert invariants.cel.subset.ARITY is invariants_cel.subset.ARITY            # docs/cel.md
    assert invariants.cel.set_string_limit is invariants_cel.values.set_string_limit
    assert invariants.scan.SKIP_DIRS is invariants_scan.SKIP_DIRS and invariants.scan.scan is invariants_scan.scan
    assert invariants.lock.constraint_weakening is invariants_schema_diff.constraint_weakening
    assert invariants.lock.compare_bad is invariants_schema_diff.compare_bad
    assert invariants.lock.Change is invariants_schema_diff.Change
    assert invariants.lock.FIELD_POLICY["rule.bad"] == "analysed"                # docs/rule-file.md, lockfile.md


def test_the_distributions_pin_one_another() -> None:
    first = tomllib.loads((REPO / "invariants-py" / "pyproject.toml").read_text())["project"]
    libs = {d: tomllib.loads((REPO / "packages" / d / "pyproject.toml").read_text())["project"] for d in LIBS}
    assert all(p["version"] == first["version"] for p in libs.values())
    assert [f"{d}=={first['version']}" for d in LIBS] == first["dependencies"][1:]
    assert first["optional-dependencies"]["cel"] == [f"invariants-cel[cel]=={first['version']}"]
    assert libs["invariants-cel"]["dependencies"] == libs["invariants-scan"]["dependencies"] == []
    assert libs["invariants-schema-diff"]["dependencies"] == [f"invariants-cel=={first['version']}"]
    # the cel extra moved with the engine, with the same pins
    assert libs["invariants-cel"]["optional-dependencies"]["cel"] == ["cel-expr-python>=0.1.4", "protobuf>=4.21"]
    assert libs["invariants-scan"]["scripts"] == {"inv-scan": "invariants_scan.__main__:main"}
    for d, p in libs.items():
        assert p["license"] == first["license"] and p["requires-python"] == first["requires-python"], d
        assert (REPO / "packages" / d / "src" / d.replace("-", "_") / "py.typed").is_file(), d


def _raise_in(name: str, code: str, inner: str | None = None) -> None:
    """Run `code` as the top-level code of a module called `name` (a `<module>` frame), with `inner`
    run the same way from inside it."""
    g = {"__name__": name, "_inner": (lambda: _raise_in(inner, "raise ValueError('boom')")) if inner else None}
    exec(compile(code, f"<{name}>", "exec"), g)


def test_an_error_in_a_librarys_top_level_code_is_not_put_on_the_app() -> None:
    """`inv` names the app module whose import failed (cli.common._importing); a library's own
    top-level code (invariants_cel, invariants_scan, invariants_schema_diff, as invariants) is never
    that module: before the split it was part of invariants."""
    for lib in ("invariants_cel.values", "invariants_scan", "invariants_schema_diff.schemas", "invariants.cel"):
        try:
            _raise_in("app.models", "_inner()", inner=lib)
        except ValueError as e:
            assert common._importing(e) == ("app.models", False), lib
    try:
        _raise_in("app.models", "_inner()", inner="invariants_celery")         # not a library: an app module
    except ValueError as e:
        assert common._importing(e) == ("invariants_celery", False)
