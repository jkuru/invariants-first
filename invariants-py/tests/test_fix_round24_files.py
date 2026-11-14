"""The scan's file handling fails closed. A symlinked directory inside a root is followed (cycles cut, a
target outside every root scanned under the importing package's name and noted), and an import of a
module a scanned package does not hold is unresolved, never third-party (1). A file in an always-skipped
directory that imports any scanned module, statically or by a literal dynamic import, or that runs code
it names at run time, is reported; only a recognised virtualenv or node package and exact copies of
scanned modules stay quiet (2). A zip archive holding Python code in a root is reported like a module
with no source, and scanned code that changes where Python finds modules (sys.path, sys.meta_path,
sys.path_hooks, importlib.invalidate_caches, site.addsitedir) is unresolved/reflection (3)."""
from __future__ import annotations

import os
import textwrap
import zipfile

import pytest

from invariants import scan
from cli_support import write_project

PAYMENTS = textwrap.dedent("""
    def send_to_gateway(charge_id, amount):
        pass

    def refund(charge_id, amount):
        send_to_gateway(charge_id, amount)
    """)
DOC = {"targets": {"py:payments.refund": {"effects": "external"}}, "rules": [{
    "id": "PAY-1", "status": "candidate", "subject": {"target": "py:payments.refund"}, "bad": "false",
    "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
    "mediation": {"sink": "py:payments.send_to_gateway", "door": "py:payments.refund"}}]}
FACADE = {"facade.py": "from payments import send_to_gateway\n"}
CALLS = "from facade import send_to_gateway\nsend_to_gateway(1, 500)\n"


def _top(tmp_path, files):
    return write_project(tmp_path, {"payments.py": PAYMENTS, **{k: textwrap.dedent(v) for k, v in files.items()}})


def _codes(rep):
    return sorted(f.code for f in rep.findings)


def _scan(root, **kw):
    return scan.scan(DOC, [root], **kw)


# --- 1. symlinked directories and unknown submodules -------------------------------------------------------
def test_a_symlinked_package_directory_into_a_dot_directory_is_scanned(tmp_path):
    root = _top(tmp_path, {**FACADE, "pkg/__init__.py": "", ".vendor/admin/__init__.py": "",
                           ".vendor/admin/tasks.py": CALLS})
    os.symlink("../.vendor/admin", root / "pkg/admin")
    rep = _scan(root)
    assert _codes(rep) == ["bypass"], rep.findings
    assert rep.findings[0].file == "pkg/admin/tasks.py"
    assert "pkg.admin.tasks" in {n for _, _, n in scan.iter_sources([root])}


def test_a_symlink_to_a_directory_outside_every_root_is_scanned_and_noted(tmp_path):
    root = _top(tmp_path / "repo", {"pkg/__init__.py": "", "ops.py": "import pkg.admin.tasks\n"})
    write_project(tmp_path / "shared", {"admin/__init__.py": "",
                                        "admin/tasks.py": "from payments import send_to_gateway\n"
                                                          "send_to_gateway(1, 2)\n"})
    os.symlink(tmp_path / "shared/admin", root / "pkg/admin")
    rep = _scan(root)
    assert _codes(rep) == ["bypass"], rep.findings
    assert any("pkg/admin" in n and "outside" in n for n in rep.notes), rep.notes


def test_a_symlink_cycle_ends(tmp_path):
    root = _top(tmp_path, {"pkg/__init__.py": "", "pkg/sub/__init__.py": "", "pkg/sub/x.py": "def f():\n    pass\n"})
    os.symlink("..", root / "pkg/sub/up")
    os.symlink(".", root / "pkg/here")
    names = {n for _, _, n in scan.iter_sources([root])}
    assert {"pkg", "pkg.sub", "pkg.sub.x", "payments"} <= names and len(names) < 12, names
    assert _scan(root).findings == []


@pytest.mark.parametrize("ops", ["import pkg.admin.tasks\n", "from pkg.admin import tasks\n",
                                 "from pkg import admin\n", "import ns.gone\n"])
def test_an_unknown_submodule_of_a_scanned_package_is_unresolved(tmp_path, ops):
    root = _top(tmp_path, {"pkg/__init__.py": "VALUE = 1\n", "ns/here.py": "", "ops.py": ops})
    os.symlink("../../nowhere/admin", root / "pkg/admin")             # dangling: nothing the scan can read
    rep = _scan(root)
    assert _codes(rep) == ["unresolved/unknown-module"], rep.findings
    assert rep.findings[0].file == "ops.py" and rep.findings[0].line == 1
    assert rep.rule_result("PAY-1") == "unresolved"


@pytest.mark.parametrize("ops", ["from pkg import VALUE, helper, sub\n", "import pkg.sub\n", "import os.path\n",
                                 "import requests.adapters\n", "from payments import refund\n",
                                 "from pkg.sub import thing\n", "from lazy import anything\n"])
def test_a_known_submodule_or_a_name_bound_in_the_package_stays_quiet(tmp_path, ops):
    root = _top(tmp_path, {"pkg/__init__.py": "VALUE = 1\ndef helper():\n    pass\n", "pkg/sub.py": "",
                           "lazy/__init__.py": "def __getattr__(name):\n    return name\n", "ops.py": ops})
    assert _scan(root).findings == []


# --- 2. files in always-skipped directories ----------------------------------------------------------------
@pytest.mark.parametrize("path, text", [
    (".ops/run.py", "import sys; sys.path.insert(0, '.')\n" + CALLS),
    ("build/run.py", "import importlib\nm = importlib.import_module('payments')\nm.send_to_gateway(1, 5)\n"),
    (".ops/run.py", "m = __import__('payments')\nm.send_to_gateway(1, 5)\n"),
    (".ops/run.py", "import importlib\nm = importlib.import_module('pay' + 'ments')\nm.send_to_gateway(1, 5)\n"),
    (".ops/run.py", "exec(open('payments.py').read())\n"),
    ("site-packages/run.py", "import payments\npayments.send_to_gateway(1, 5)\n"),
    ("node_modules/x/run.py", "import payments\npayments.send_to_gateway(1, 5)\n"),
    ("venv/bin/tool.py", "import facade\n"),
])
def test_a_skipped_file_that_imports_any_scanned_module_is_reported(tmp_path, path, text):
    root = _top(tmp_path, {**FACADE, path: text})
    rep = _scan(root)
    assert _codes(rep) == ["unresolved/skipped-importer"], rep.findings
    assert rep.findings[0].file == path


def test_a_skipped_file_that_reaches_no_scanned_code_stays_quiet(tmp_path):
    root = _top(tmp_path, {**FACADE, ".ops/tool.py": "import re, json, torch\nre.compile('x')\ntorch.nn.Module().eval()\n"
                                                     "import importlib.util\nimportlib.util.find_spec('payments')\n"})
    assert _scan(root).findings == []


def test_a_virtualenv_file_that_writes_sys_path_and_imports_project_code_is_reported(tmp_path):
    site = "venv/lib/python3.12/site-packages"
    root = _top(tmp_path, {**FACADE, "venv/pyvenv.cfg": "", f"{site}/facade.py": "",
                           f"{site}/hook.py": "import sys\nsys.path[0:0] = ['.']\nimport facade\n"})
    rep = _scan(root)
    assert _codes(rep) == ["unresolved/skipped-importer"], rep.findings


def test_a_stale_build_copy_with_a_runner_is_reported(tmp_path):
    root = _top(tmp_path, {"build/lib/payments.py": "def send_to_gateway(c, a):\n    print(c)\n",
                           "build/lib/runner.py": "import payments\npayments.send_to_gateway(1, 2)\n"})
    rep = _scan(root)
    assert _codes(rep) == ["unresolved/skipped-importer"], rep.findings
    assert rep.findings[0].file == "build/lib/runner.py"


def test_a_recognised_virtualenv_stays_quiet_unless_it_imports_project_code_it_does_not_hold(tmp_path):
    site = ".venv/lib/python3.12/site-packages"
    root = _top(tmp_path, {**FACADE, ".venv/pyvenv.cfg": "home = /usr/bin\n",
                           f"{site}/facade/__init__.py": "import importlib\nimportlib.import_module(__name__ + '.x')\n",
                           f"{site}/plugin/__init__.py": "import facade\n",
                           f"{site}/plugin_x/loader.py": "import importlib\ndef load(n):\n"
                                                         "    return importlib.import_module(n)\n"})
    assert _scan(root).findings == []
    (root / site / "evil.py").write_text("import payments\npayments.send_to_gateway(1, 5)\n")
    rep = _scan(root)
    assert _codes(rep) == ["unresolved/skipped-importer"], rep.findings
    assert rep.findings[0].file == f"{site}/evil.py"


def test_a_site_packages_with_distribution_metadata_and_a_node_package_stay_quiet(tmp_path):
    root = _top(tmp_path, {**FACADE, "site-packages/six.py": "import facade\n",
                           "site-packages/facade.py": "",
                           "site-packages/six-1.0.dist-info/METADATA": "",
                           "node_modules/gyp/package.json": "{}",
                           "node_modules/gyp/pylib/gyp/__init__.py": "import gyp.common\nimport importlib\n"
                                                                     "importlib.import_module(gyp.x)\n",
                           "node_modules/gyp/pylib/gyp/common.py": ""})
    assert _scan(root).findings == []


def test_a_skipped_file_that_is_the_same_file_as_a_scanned_module_stays_quiet(tmp_path):
    root = _top(tmp_path, {"pkg/__init__.py": "", ".vendor/admin/__init__.py": "",
                           ".vendor/admin/tasks.py": "from payments import refund\nrefund(1, 2)\n"})
    os.symlink("../.vendor/admin", root / "pkg/admin")
    assert _scan(root).findings == []


# --- 3. archives and import-path writes --------------------------------------------------------------------
def _zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)


@pytest.mark.parametrize("name", ["vendor.zip", ".ops/vendor.zip", "plugins.egg", "tool.pyz"])
def test_an_archive_holding_python_code_is_reported(tmp_path, name):
    root = _top(tmp_path, {})
    _zip(root / name, {"plugin.py": "import payments\npayments.send_to_gateway(1, 5)\n"})
    rep = _scan(root)
    assert _codes(rep) == ["unresolved/no-source"], rep.findings
    assert rep.findings[0].file == name


def test_a_data_archive_and_an_excluded_one_stay_quiet(tmp_path):
    root = _top(tmp_path, {})
    _zip(root / "data.zip", {"rows.csv": "a,b\n"})
    _zip(root / "tests/fixtures/plugin.zip", {"plugin.py": "x = 1\n"})
    (root / "broken.zip").write_bytes(b"not a zip")
    assert _scan(root, exclude=scan.anywhere("tests")).findings == []


@pytest.mark.parametrize("ops", [
    "import sys\nsys.path.insert(0, 'vendor.zip')\n",
    "import sys\nsys.path = ['x'] + sys.path\n",
    "import sys\nsys.path[0:0] = ['x']\n",
    "import sys\nsys.path += ['x']\n",
    "import sys\nsys.meta_path.append(object())\n",
    "import sys\nsys.path_hooks.insert(0, object())\n",
    "import sys\nsys.path_importer_cache.clear()\n",
    "import importlib\nimportlib.invalidate_caches()\n",
    "from sys import path\npath.append('x')\n",
    "import sys as s\ndef f():\n    s.path.extend(['x'])\n",
    "import site\nsite.addsitedir('x')\n",
    "import zipimport\nzipimport.zipimporter('vendor.zip')\n",
])
def test_scanned_code_that_changes_where_python_finds_modules_is_unresolved(tmp_path, ops):
    rep = _scan(_top(tmp_path, {"ops.py": ops}))
    assert _codes(rep) == ["unresolved/reflection"], rep.findings
    assert rep.findings[0].file == "ops.py"


def test_reading_sys_path_stays_quiet(tmp_path):
    ops = "import sys\nfor p in sys.path:\n    print(p, sys.path.index(p), list(sys.path), sys.path[0])\n"
    assert _scan(_top(tmp_path, {"ops.py": ops})).findings == []
