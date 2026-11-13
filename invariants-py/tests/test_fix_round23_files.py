"""Fix round 23 (scan: files and roots). A file several roots hold is indexed once, under its deepest
root's name, and every other root's name for it is an alias; a relative import resolves by directory and
one that names no module is reported (H1). A file is excluded only when the excludes leave it out from
every root that holds it, so adding a root never excludes more (H2). What Python imports under a name
wins (a package over a module of the same name) and the shadowed file is reported (H3). An importable
module with no source (.pyc, .so, .pyd) is reported, and so are calls into it (H4). The always-skipped
directories are counted in the report, and a file there that imports a sink's module is reported (M1)."""
from __future__ import annotations

import py_compile
import textwrap

from invariants import lock, scan
from cli_support import write_project

PAYMENTS = textwrap.dedent("""
    def send_to_gateway(charge_id, amount):
        pass

    def refund(charge_id, amount):
        send_to_gateway(charge_id, amount)
    """)


def _doc(module="payments"):
    door = f"py:{module}.refund"
    return {"targets": {door: {"effects": "external"}}, "rules": [{
        "id": "PAY-1", "status": "candidate", "subject": {"target": door}, "bad": "false",
        "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
        "mediation": {"sink": f"py:{module}.send_to_gateway", "door": door}}]}


def _codes(rep):
    return sorted(f.code for f in rep.findings)


def _app(tmp_path, files):
    return write_project(tmp_path, {"src/app/__init__.py": "", "src/app/payments.py": PAYMENTS,
                                    **{k: textwrap.dedent(v) for k, v in files.items()}})


SUB_OPS = {"src/app/sub/__init__.py": "",
           "src/app/sub/ops.py": """
               from ..payments import send_to_gateway
               def g():
                   send_to_gateway("c", 5)
               """}


# --- H1: overlapping roots ---------------------------------------------------------------------------------
def test_a_nested_root_keeps_a_relative_import_resolved(tmp_path):
    root = _app(tmp_path, SUB_OPS)
    for roots in ([root / "src", root], [root / "src", root, root / "src/app/sub"]):
        rep = scan.scan(_doc("app.payments"), roots)
        assert _codes(rep) == ["bypass"], (roots, rep.findings)


def test_a_call_through_another_roots_name_for_a_module_is_followed(tmp_path):
    root = _app(tmp_path, {"run.py": """
        from src.app.payments import send_to_gateway
        import src.app.payments
        def main():
            send_to_gateway("c", 1)
        def other():
            src.app.payments.send_to_gateway("c", 1)
        """})
    rep = scan.scan(_doc("app.payments"), [root / "src", root])
    assert _codes(rep) == ["bypass", "bypass"], rep.findings
    assert {f.owner for f in rep.findings} == {"run.main", "run.other"}


def test_a_name_from_any_root_resolves_a_rule_target(tmp_path):
    root = _app(tmp_path, SUB_OPS)
    rep = scan.scan(_doc("src.app.payments"), [root / "src", root])
    assert _codes(rep) == ["bypass"], rep.findings


def test_overlapping_roots_still_name_each_file_from_its_deepest_root(tmp_path):
    root = _app(tmp_path, SUB_OPS)
    names = [n for _, _, n in scan.iter_sources([root, root / "src"])]
    assert sorted(names) == ["app", "app.payments", "app.sub", "app.sub.ops"]


def test_a_relative_import_of_a_missing_module_is_reported(tmp_path):
    root = _app(tmp_path, {"src/app/ops.py": """
        from .gone import send_to_gateway
        def g():
            send_to_gateway("c", 5)
        """})
    rep = scan.scan(_doc("app.payments"), [root / "src"])
    assert _codes(rep) == ["unresolved/relative-import"], rep.findings
    f = rep.findings[0]
    assert (f.file, f.line, f.kind) == ("app/ops.py", 2, "unresolved") and "app.gone" in f.message
    assert rep.rule_result("PAY-1") == "unresolved"


def test_a_relative_import_above_every_root_is_reported(tmp_path):
    root = _app(tmp_path, SUB_OPS)
    rep = scan.scan(_doc("app.payments"), [root / "src/app/sub"])
    assert "unresolved/relative-import" in _codes(rep), rep.findings
    assert "climbs above" in rep.for_rule("PAY-1")[-1].message or any(
        "climbs above" in f.message for f in rep.findings)


def test_relative_imports_that_resolve_stay_quiet(tmp_path):
    root = _app(tmp_path, {"src/app/ns/x.py": "def f():\n    pass\n",          # a namespace package
                           "src/app/ops.py": """
                               from . import payments
                               from .payments import refund
                               from .ns import x
                               from .ns.x import f
                               def g():
                                   refund("c", 1)
                               """})
    rep = scan.scan(_doc("app.payments"), [root / "src", root])
    assert rep.findings == [], rep.findings


# --- H2: excludes and roots ---------------------------------------------------------------------------------
LEGACY = {"src/app/legacy/x.py": """
    from ..payments import send_to_gateway
    def g():
        send_to_gateway("c", 5)
    """}


def test_adding_a_root_never_excludes_more(tmp_path):
    root = _app(tmp_path, LEGACY)
    one = scan.scan(_doc("src.app.payments"), [root], ["app/legacy/*"])
    assert _codes(one) == ["bypass"], one.findings          # src/app/legacy/x.py does not match app/legacy/*
    two = scan.scan(_doc("app.payments"), [root, root / "src"], ["app/legacy/*"])
    assert _codes(two) == ["bypass"], two.findings          # it matches from src, not from .: still scanned
    assert any("app/legacy/*" in n for n in two.notes), two.notes
    assert "note: exclude 'app/legacy/*'" in scan.to_text(two)


def test_an_exclude_that_matches_from_every_root_excludes(tmp_path):
    root = _app(tmp_path, LEGACY)
    rep = scan.scan(_doc("app.payments"), [root, root / "src"], ["*/legacy/*"])
    assert rep.findings == [] and rep.notes == [], rep.findings


# --- H3: a package and a module of the same name -------------------------------------------------------------
def _top(tmp_path, files):
    return write_project(tmp_path, {"payments.py": PAYMENTS, **{k: textwrap.dedent(v) for k, v in files.items()}})


SINKS = "from payments import send_to_gateway\ndef g():\n    send_to_gateway('c', 5)\n"


def test_a_package_wins_over_a_module_of_the_same_name(tmp_path):
    root = _top(tmp_path, {"ops.py": "def g():\n    pass\n", "ops/__init__.py": SINKS,
                           "main.py": "import ops\ndef main():\n    ops.g()\n"})
    rep = scan.scan(_doc(), [root])
    assert _codes(rep) == ["bypass", "unresolved/shadowed-module"], rep.findings
    shadow = next(f for f in rep.findings if f.code == "unresolved/shadowed-module")
    assert shadow.file == "ops.py" and "ops/__init__.py" in shadow.message


def test_the_shadowed_module_is_not_read(tmp_path):
    root = _top(tmp_path, {"ops.py": SINKS, "ops/__init__.py": "def g():\n    pass\n"})
    rep = scan.scan(_doc(), [root])
    assert _codes(rep) == ["unresolved/shadowed-module"], rep.findings


def test_the_same_module_name_from_two_roots_is_reported(tmp_path):
    root = write_project(tmp_path, {"a/payments.py": PAYMENTS, "a/util.py": "def h():\n    pass\n",
                                    "b/util.py": SINKS})
    rep = scan.scan(_doc(), [root / "a", root / "b"])
    assert _codes(rep) == ["unresolved/shadowed-module"], rep.findings
    assert rep.findings[0].file == "util.py" and rep.findings[0].root == str((root / "b").resolve())


# --- H4: importable modules with no source -------------------------------------------------------------------
def test_a_sourceless_pyc_is_reported_and_so_are_calls_into_it(tmp_path):
    root = _top(tmp_path, {"main.py": "import ops\ndef main():\n    ops.g()\n"})
    src = tmp_path / "ops_src.py"
    src.write_text(SINKS)
    py_compile.compile(str(src), cfile=str(root / "ops.pyc"))
    src.unlink()
    rep = scan.scan(_doc(), [root])
    assert _codes(rep) == ["unresolved/excluded-callee", "unresolved/excluded-callee",
                           "unresolved/no-source"], rep.findings
    assert rep.rule_result("PAY-1") == "unresolved"
    assert any("compiled .pyc" in f.message for f in rep.findings if f.code == "unresolved/excluded-callee")


def test_an_extension_module_wins_over_its_source(tmp_path):
    root = _top(tmp_path, {"ops.py": "def g():\n    pass\n", "main.py": "import ops\ndef main():\n    ops.g()\n"})
    (root / "ops.cpython-312-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    rep = scan.scan(_doc(), [root])
    assert "unresolved/no-source" in _codes(rep) and "unresolved/shadowed-module" in _codes(rep), rep.findings
    assert rep.rule_result("PAY-1") == "unresolved"


def test_a_stale_pyc_beside_its_source_and_a_virtualenvs_extensions_stay_quiet(tmp_path):
    root = _top(tmp_path, {"ops.py": "def g():\n    pass\n"})
    py_compile.compile(str(root / "ops.py"), cfile=str(root / "ops.pyc"))
    site = root / ".venv/lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (site / "_speedups.cpython-312-darwin.so").write_bytes(b"\xcf\xfa")
    (root / "__pycache__").mkdir()
    py_compile.compile(str(root / "ops.py"), cfile=str(root / "__pycache__/ops.cpython-312.pyc"))
    rep = scan.scan(_doc(), [root])
    assert rep.findings == [], rep.findings


# --- M1: the always-skipped directories ---------------------------------------------------------------------
def test_a_standalone_script_in_a_skipped_directory_that_imports_the_sink_is_reported(tmp_path):
    for where in ("build", ".ops", "dist"):
        root = _top(tmp_path / where, {f"{where}/payout.py": SINKS + "g()\n",
                                       f"{where}/other.py": "import os\n"})
        rep = scan.scan(_doc(), [root], lock.DEFAULT_SCAN_EXCLUDE)
        assert _codes(rep) == ["unresolved/skipped-importer"], (where, rep.findings)
        assert rep.findings[0].file == f"{where}/payout.py" and rep.findings[0].line == 1
        assert rep.skipped == {where: 2}
        assert f"skipped (directories the scan always skips): {where}/ 2 file(s)" in scan.to_text(rep)


def test_a_build_copy_of_the_package_stays_quiet(tmp_path):
    root = _app(tmp_path, {"src/app/ops.py": "from app.payments import refund\n",
                           "build/lib/app/ops.py": "from app.payments import refund\n",
                           "build/lib/app/payments.py": PAYMENTS})
    rep = scan.scan(_doc("app.payments"), [root / "src", root])
    assert rep.findings == [], rep.findings
    assert rep.skipped == {"build": 2}


def test_the_skip_list_the_lock_pins(tmp_path):
    assert scan.SKIPPED == (".*", "__pycache__", "build", "dist", "node_modules", "site-packages", "venv")
