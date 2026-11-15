"""Fix round 28: false passes of the static scan (an independent review) and real-codebase adoption.

1. An absolute import found through the importing file's own directory (sys.path[0] for a script) or a
   deeper sys.path entry resolves to that module (`import fleet.store` in cli/main.py is
   cli/fleet/store.py); a name several modules below a root match is unresolved/unknown-module.
2. `cls(...)` in a @classmethod (whatever its first parameter is called) builds the class or a subclass.
3. Annotations of module-level defs, methods and module or class bodies are walked: a call in one runs,
   Annotated metadata are values a framework calls, an empty FastAPI Depends() builds the annotated
   class, a string annotation is parsed; a function body's variable annotation is not evaluated.
4. A literal lookup (getattr, __dict__[k], pydoc.locate, pkgutil.resolve_name, inspect.getattr_static,
   f.__globals__[k]) that names a scanned class builds it when called, and a class or module so named is
   a value.
5. An allowed `pkg.mod.<module>` excuses only the module body's frozen calls into allowed functions,
   never its own call into the sink or its sink value.
A. Shas are worked out only when read (JSON, SARIF, the API) or for a rule with allow_unresolved.
B. Writing a door's __doc__ (or __name__, __qualname__, __module__, __annotations__) is no monkeypatch.
C. A moved allow_unresolved entry written with a column is told its new line and column.
D. Black's magic trailing comma keeps the sha; a one-element tuple's comma does not.
E. A computed setattr on a plain class the scan sees whole is narrowed like a computed getattr.
F. An unwrap of an unknown object is reported only when a decorated function can reach the sink; an
   unknown `fn.__closure__` is unresolved, never forbidden.
G. inv init counts back doors and unresolved sites apart, and drafts the unresolved ones for review.
H. A relative import naming no module is reported under the indexed package name.
"""
from __future__ import annotations

import ast
import textwrap

import pytest
from cli_support import write_project

import invariants_scan as scan
from invariants import doors
from invariants_scan import accept

PAY = "def send_to_gateway(c, a):\n    pass\n\ndef refund(c, a):\n    send_to_gateway(c, a)\n"


def _rule(sink, *door, **med):
    return {"rules": [{"id": "R", "mediation": {"sink": sink, "door": list(door), **med}}]}


PAY_RULE = _rule("py:payments.send_to_gateway", "py:payments.refund")
K_RULE = _rule("py:payments.send_to_gateway", "py:svc.door")
K = ("import payments\nclass K:\n    def __init__(self):\n        payments.send_to_gateway(1, 2)\n"
     "def door():\n    K()\n")


def _scan(tmp_path, files, doc=PAY_RULE, pay=True):
    # with the door elsewhere, payments holds the sink alone (refund would be a back door)
    base = PAY if "py:payments.refund" in str(doc) else "def send_to_gateway(c, a):\n    pass\n"
    files = {**({"payments.py": base} if pay else {}), **{k: textwrap.dedent(v) for k, v in files.items()}}
    return scan.scan(doc, [write_project(tmp_path, files)])


def _codes(rep):
    return sorted({f.code for f in rep.findings})


# --- 1. imports found from the importing file's directory or a deeper sys.path entry ---------------------
SINKMOD = "def sink():\n    pass\ndef door():\n    sink()\n"
DEEP = _rule("py:cli.fleet.store.sink", "py:cli.fleet.store.door")


@pytest.mark.parametrize("main", [
    "import fleet.store\ndef run():\n    fleet.store.sink()\n",
    "from fleet.store import sink as s\ndef run():\n    s()\n",
    "from fleet import store\ndef run():\n    store.sink()\n",
    "import fleet.store as fs\ndef run():\n    fs.sink()\n",
    "import fleet\ndef run():\n    fleet.store.sink()\n",
])
def test_an_import_found_from_the_scripts_own_directory_is_followed(tmp_path, main):
    rep = _scan(tmp_path, {"cli/__init__.py": "", "cli/fleet/__init__.py": "from . import store\n",
                           "cli/fleet/store.py": SINKMOD, "cli/main.py": main}, DEEP, pay=False)
    assert [(f.code, f.path) for f in rep.findings] == [("bypass", ["cli.main.run", "cli.fleet.store.sink"])]


def test_a_deeper_sys_path_entry_is_followed_and_a_value_through_it_escapes(tmp_path):
    rep = _scan(tmp_path, {"svc.py": "from cli.fleet import store\ndef door():\n    store.h()\n",
                           "cli/__init__.py": "", "cli/fleet/__init__.py": "",
                           "cli/fleet/store.py": "import payments\ndef h():\n    payments.send_to_gateway(1, 2)\n",
                           "tools/main.py": "import threading, fleet.store\nthreading.Thread(target=fleet.store.h)\n"},
                _rule("py:payments.send_to_gateway", "py:svc.door"))
    assert _codes(rep) == ["unresolved/sink-escapes"], rep.findings


def test_a_scripts_folder_without_init_imports_its_sibling(tmp_path):
    rep = _scan(tmp_path, {"ops/pay.py": SINKMOD, "ops/run.py": "from pay import sink\nsink()\n"},
                _rule("py:ops.pay.sink", "py:ops.pay.door"), pay=False)
    assert [(f.code, f.file) for f in rep.findings] == [("bypass", "ops/run.py")]


def test_a_name_two_deeper_modules_match_is_unknown_module(tmp_path):
    rep = _scan(tmp_path, {"a/fleet/__init__.py": "", "a/fleet/store.py": "", "b/fleet/__init__.py": "",
                           "b/fleet/store.py": "", "tools/main.py": "import fleet.store\n"})
    hits = [f for f in rep.findings if f.code == "unresolved/unknown-module"]
    assert [f.file for f in hits] == ["tools/main.py"] and "a.fleet.store" in hits[0].message, rep.findings


def test_a_package_named_like_a_driver_does_not_take_the_drivers_submodule(tmp_path):
    # sqlalchemy/dialects/mysql/ beside `from mysql.connector import constants`: the installed driver
    rep = _scan(tmp_path, {"lib/__init__.py": "", "lib/dialects/__init__.py": "", "lib/dialects/mysql/__init__.py": "",
                           "lib/dialects/mysql/conn.py": "def f():\n    from mysql.connector import constants\n"})
    assert rep.findings == [], rep.findings


def test_a_package_named_like_a_library_is_not_what_it_imports(tmp_path):
    # hypothesis/extra/django/_fields.py: `from django import forms` is the installed django, not its own
    # package; nor is a package that lacks the names a from-import takes
    rep = _scan(tmp_path, {"lib/__init__.py": "", "lib/extra/__init__.py": "", "lib/extra/django/__init__.py": "",
                           "lib/extra/django/_fields.py": "from django import forms\nimport django\n",
                           "lib/other/__init__.py": "", "lib/other/use.py": "from django import db\n"})
    assert rep.findings == [], rep.findings


# --- 2. cls() in a classmethod ------------------------------------------------------------------------------
@pytest.mark.parametrize("svc, main", [
    ("    @classmethod\n    def mk(cls):\n        return cls()\n", "import svc\nsvc.K.mk()\n"),
    ("    @classmethod\n    def mk(cls):\n        x = cls()\n        return x\n", "import svc\nsvc.K.mk()\n"),
    ("    @classmethod\n    def mk(klass):\n        return klass()\n", "import svc\nsvc.K.mk()\n"),
    ("    @classmethod\n    def mk(cls):\n        return cls()\nclass L(K):\n    pass\n", "import svc\nsvc.L.mk()\n"),
])
def test_cls_called_in_a_classmethod_builds_the_class(tmp_path, svc, main):
    src = K.replace("def door():", svc + "def door():")
    rep = _scan(tmp_path, {"svc.py": src, "main.py": main}, K_RULE)
    assert ["svc.K.mk", "svc.K.__init__"] == [p for f in rep.findings for p in f.path][1:3], rep.findings


def test_a_subclass_constructor_is_what_cls_may_build(tmp_path):
    src = ("import payments\nclass K:\n    @classmethod\n    def mk(cls):\n        return cls()\n"
           "class L(K):\n    def __init__(self):\n        payments.send_to_gateway(1, 2)\ndef door():\n    L()\n")
    rep = _scan(tmp_path, {"svc.py": src, "main.py": "import svc\nsvc.K.mk()\n"}, K_RULE)
    assert [f.path for f in rep.findings] == [["main.<module>", "svc.K.mk", "svc.L.__init__", "payments.send_to_gateway"]]


# --- 3. annotations -----------------------------------------------------------------------------------------
PAYH = PAY.replace("def refund(c, a):\n    send_to_gateway(c, a)",
                   "def _send(*a):\n    send_to_gateway(1, 2)\n    return int\n\ndef refund(c, a):\n    _send(c, a)")


@pytest.mark.parametrize("main", [
    "import payments\ndef evil(x: payments._send()):\n    pass\n",
    "import payments\ndef evil() -> payments._send():\n    pass\n",
    "import payments\nx: payments._send() = 1\n",
    "import payments\nx: payments._send()\n",
    "import payments\nclass K:\n    x: payments._send() = 1\n",
    "import payments\nclass K:\n    def m(self) -> payments._send():\n        pass\n",
    "import payments\ndef evil(x: 'payments._send()'):\n    pass\n",
    "import payments\nfrom typing import Annotated\nimport pydantic\nclass M(pydantic.BaseModel):\n"
    "    x: Annotated[int, pydantic.AfterValidator(payments._send)]\n",
    "import payments\nfrom typing import Annotated\nfrom fastapi import Depends, FastAPI\napp = FastAPI()\n"
    "@app.get('/')\ndef r(x: Annotated[int, Depends(payments._send)]):\n    pass\n",
    "from __future__ import annotations\nimport payments\nfrom typing import Annotated\n"
    "from fastapi import Depends, FastAPI\napp = FastAPI()\n@app.get('/')\n"
    "def r(x: Annotated[int, Depends(payments._send)]):\n    pass\n",
    "from __future__ import annotations\nimport payments\nfrom typing import Annotated\n"
    "def r(x: 'Annotated[int, payments.send_to_gateway]'):\n    pass\n",
])
def test_an_annotation_is_walked_as_the_expression_it_is(tmp_path, main):
    rep = _scan(tmp_path, {"payments.py": PAYH, "main.py": main})
    assert rep.rule_result("R") != "pass", rep.findings


@pytest.mark.parametrize("main", [
    "from fastapi import Depends, FastAPI\nimport svc\napp = FastAPI()\n@app.get('/')\n"
    "def r(k: svc.K = Depends()):\n    pass\n",
    "from typing import Annotated\nfrom fastapi import Depends, FastAPI\nimport svc\napp = FastAPI()\n"
    "@app.get('/')\ndef r(k: Annotated[svc.K, Depends()]):\n    pass\n",
    "from typing import Annotated\nfrom fastapi import Depends\nimport svc\nasync def r(k: 'Annotated[svc.K, Depends()]'):\n    pass\n",
])
def test_an_empty_depends_builds_the_annotated_class(tmp_path, main):
    rep = _scan(tmp_path, {"svc.py": K, "main.py": main}, K_RULE)
    assert rep.rule_result("R") != "pass", rep.findings


@pytest.mark.parametrize("main", [
    "import svc, typing\ndef f(k: svc.K, j: typing.Optional[svc.K] = None) -> list[svc.K]:\n    pass\n",
    "import svc\nfrom typing import Literal\ndef f(k: Literal['svc.K()']):\n    pass\n",
    "import svc\nk: svc.K\nclass M:\n    k: 'svc.K | None' = None\n",
    "import svc\ndef outer():\n    x: svc.K()\n",                # a local annotation is never evaluated
])
def test_a_type_in_an_annotation_runs_nothing(tmp_path, main):
    rep = _scan(tmp_path, {"svc.py": K, "main.py": main}, K_RULE)
    assert rep.findings == [], rep.findings


# --- 4. literal lookups of a class or module -----------------------------------------------------------------
@pytest.mark.parametrize("main", [
    "import svc\ngetattr(svc, 'K')()\n",
    "import svc\nsvc.__dict__['K']()\n",
    "import pydoc\npydoc.locate('svc.K')()\n",
    "import pkgutil\npkgutil.resolve_name('svc:K')()\n",
    "import inspect, svc\ninspect.getattr_static(svc, 'K')()\n",
    "import svc\nsvc.door.__globals__['K']()\n",
    "import svc\nC = getattr(svc, 'K')\n",
])
def test_a_literal_lookup_of_a_class_builds_it(tmp_path, main):
    rep = _scan(tmp_path, {"svc.py": K, "main.py": main}, K_RULE)
    assert rep.rule_result("R") != "pass", rep.findings


def test_a_called_literal_lookup_of_a_class_is_a_call_edge(tmp_path):
    rep = _scan(tmp_path, {"svc.py": K, "main.py": "import svc\ndef evil():\n    getattr(svc, 'K')()\n"}, K_RULE)
    assert [(f.code, f.path[:2]) for f in rep.findings] == [("bypass", ["main.evil", "svc.K.__init__"])], rep.findings


def test_a_literal_lookup_of_a_nested_class_or_a_submodule(tmp_path):
    nested = ("import payments\nclass O:\n    class K:\n        def __init__(self):\n"
              "            payments.send_to_gateway(1, 2)\ndef door():\n    O.K()\n")
    rep = _scan(tmp_path / "a", {"svc.py": nested, "main.py": "import svc\ngetattr(svc.O, 'K')()\n"}, K_RULE)
    assert rep.rule_result("R") != "pass", rep.findings
    rep = _scan(tmp_path / "b", {"pkg/__init__.py": "", "pkg/sub.py": "import payments\ndef h():\n    payments.send_to_gateway(1, 2)\n",
                                 "svc.py": "import pkg.sub\ndef door():\n    pkg.sub.h()\n",
                                 "main.py": "import pkg.sub, threading\nm = getattr(pkg, 'sub')\nthreading.Thread(target=m.h)\n"},
                _rule("py:payments.send_to_gateway", "py:svc.door"))
    assert rep.rule_result("R") != "pass", rep.findings


# --- 5. an allowed module body -------------------------------------------------------------------------------
INIT_APP = {"app/__init__.py": "", "app/repo.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n",
            "app/service.py": "from app.repo import OrderRepo\ndef place(o):\n    OrderRepo().save_order(o)\n",
            "app/cli.py": "from app.repo import OrderRepo\ndef main():\n    OrderRepo().save_order(1)\nmain()\n"}
INIT_RULE = _rule("py:app.repo.OrderRepo.save_order", "py:app.service.place",
                  allow=["py:app.cli.<module>", "py:app.cli.main"])


def test_an_allowed_module_body_still_lets_its_frozen_calls_through(tmp_path):
    assert _scan(tmp_path, INIT_APP, INIT_RULE, pay=False).findings == []


@pytest.mark.parametrize("extra, code", [
    ("OrderRepo().save_order(2)\n", "bypass"),
    ("import threading\nthreading.Thread(target=OrderRepo().save_order)\n", "unresolved/sink-escapes"),
])
def test_an_allowed_module_body_does_not_excuse_its_own_sink_call(tmp_path, extra, code):
    files = {**INIT_APP, "app/cli.py": INIT_APP["app/cli.py"] + extra}
    rep = _scan(tmp_path, files, INIT_RULE, pay=False)
    assert code in _codes(rep), rep.findings


# --- A. lazy shas ---------------------------------------------------------------------------------------------
UNRES = {"plug.py": "import importlib\ndef load(n):\n    importlib.import_module(n)\n"}


def test_a_text_scan_of_a_rule_without_allow_unresolved_hashes_nothing(tmp_path, monkeypatch):
    calls = []
    real = accept.site_sha
    monkeypatch.setattr(accept, "site_sha", lambda *a: calls.append(a) or real(*a))
    rep = _scan(tmp_path, UNRES)
    scan.to_text(rep)
    assert calls == [] and rep.findings
    d = rep.as_dict()                                  # JSON (and the API) still give every sha
    assert all(len(f["sha"]) == 16 for f in d["findings"] if f["kind"] == "unresolved") and calls
    assert rep.findings[0].sha == d["findings"][0]["sha"]


def test_a_lazy_sha_is_the_eager_one(tmp_path):
    lazy = _scan(tmp_path / "a", UNRES)
    eager = _scan(tmp_path / "b", UNRES, {"rules": [{"id": "R", "mediation": {
        "sink": "py:payments.send_to_gateway", "door": ["py:payments.refund"], "allow_unresolved": []}}]})
    assert [f.sha for f in lazy.findings] == [f.sha for f in eager.findings] and lazy.findings[0].sha
    assert scan.to_sarif(lazy, "X")["runs"][0]["results"][0]["partialFingerprints"]["invSite/v3"] == lazy.findings[0].sha


# --- B. descriptive dunders ------------------------------------------------------------------------------------
@pytest.mark.parametrize("line, code", [
    ("payments.refund.__doc__ = 'changed'", None),
    ("payments.refund.__name__ = 'r'", None),
    ("setattr(payments.refund, '__qualname__', 'r')", None),
    ("payments.refund.__code__ = (lambda c, a: None).__code__", "forbidden/monkeypatch"),
    ("setattr(payments.refund, '__defaults__', (1,))", "forbidden/monkeypatch"),
])
def test_writing_a_doors_description_is_no_monkeypatch(tmp_path, line, code):
    rep = _scan(tmp_path, {"main.py": f"import payments\n{line}\n"})
    assert _codes(rep) == ([code] if code else []), rep.findings


# --- C. a moved entry keeps its column ---------------------------------------------------------------------------
def test_a_moved_entry_with_a_column_is_told_its_column(tmp_path):
    root = write_project(tmp_path, {"payments.py": PAY, **UNRES})
    f = scan.scan(PAY_RULE, [root]).findings[0]
    doc = _rule("py:payments.send_to_gateway", "py:payments.refund", allow_unresolved=[
        {"at": f"{f.file}:{f.line}:{f.col}", "code": f.code, "sha": f.sha, "why": "plugins never import payments"}])
    (root / "plug.py").write_text("# moved\n" + UNRES["plug.py"])
    rep = scan.scan(doc, [root])
    assert rep.findings == [] and rep.accepted[0].moved_col == f.col
    assert f"moved from {f.line}:{f.col}: update its at to plug.py:{f.line + 1}:{f.col}" in scan.to_text(rep)


# --- D. black's magic trailing comma ------------------------------------------------------------------------------
@pytest.mark.parametrize("one, two, same", [
    ("mod = __import__(name, fromlist=[obj])", "mod = __import__(\n    name,\n    fromlist=[obj],\n)", True),
    ("f(a, b)", "f(\n    a,\n    b,\n)", True),
    ("x = [a, b]", "x = [\n    a,\n    b,\n]", True),
    ("d = {a: b}", "d = {\n    a: b,\n}", True),
    ("from m import (a, b)", "from m import (\n    a,\n    b,\n)", True),
    ("x = (a)", "x = (a,)", False),
    ("y = t[a]", "y = t[a,]", False),
    ("y = t[a, b]", "y = t[a, b,]", True),
])
def test_a_magic_trailing_comma_keeps_the_sha(one, two, same):
    assert (accept._normalized(one) == accept._normalized(two)) == same


# --- E. computed setattr narrowed ---------------------------------------------------------------------------------
REC = ("from dataclasses import dataclass, fields\n@dataclass\nclass Rec:\n    a: int = 0\n"
       "    def update(self, other: 'Rec') -> None:\n        for f in fields(self):\n"
       "            setattr(self, f.name, getattr(other, f.name))\n")


@pytest.mark.parametrize("extra, reported", [
    ("", False),
    ("    def __setattr__(self, k, v):\n        object.__setattr__(self, k, v)\n", True),
    ("    @property\n    def b(self):\n        return 1\n", True),
])
def test_a_computed_setattr_on_a_plain_class_is_narrowed(tmp_path, extra, reported):
    rep = _scan(tmp_path, {"rec.py": REC + extra})
    assert ("unresolved/computed-attribute" in _codes(rep)) == reported, rep.findings


# --- F. unwrap of an unknown object ---------------------------------------------------------------------------------
def test_an_unknown_unwrap_with_no_decorated_function_reaching_the_sink_is_quiet(tmp_path):
    rep = _scan(tmp_path, {"ops.py": "import functools\n@functools.lru_cache\ndef g():\n    return 1\n"
                                     "def f(fn):\n    return fn.__wrapped__()\n"})
    assert rep.findings == [], rep.findings


def test_an_unknown_unwrap_call_may_be_a_method_named_wrapped(tmp_path):
    rep = _scan(tmp_path, {"ops.py": "import operator, payments\nclass W:\n    def __wrapped__(self):\n"
                                     "        payments.send_to_gateway(1, 2)\n"
                                     "def f(x):\n    operator.methodcaller('__wrapped__')(x)\n"})
    assert "unresolved/unwrap" in _codes(rep), rep.findings


def test_an_unknown_closure_read_is_unresolved_not_forbidden(tmp_path):
    guard = ("import functools, payments\ndef deco(f):\n    @functools.wraps(f)\n    def w(*a):\n"
             "        return f(*a)\n    return w\n@deco\ndef door(c, a):\n    payments.send_to_gateway(c, a)\n")
    rep = _scan(tmp_path, {"svc.py": guard, "ops.py": "def f(fn):\n    return fn.__closure__\n"},
                _rule("py:payments.send_to_gateway", "py:svc.door"))
    assert "forbidden/unwrap" not in _codes(rep) and "unresolved/unwrap" in _codes(rep), rep.findings
    rep = _scan(tmp_path / "b", {"svc.py": guard, "ops.py": "def f(fn):\n    return fn.__wrapped__\n"},
                _rule("py:payments.send_to_gateway", "py:svc.door"))
    assert "forbidden/unwrap" in _codes(rep), rep.findings


# --- G. inv init: back doors and unresolved sites apart --------------------------------------------------------------
def test_init_lists_unresolved_sites_apart_and_drafts_them_for_review(tmp_path):
    root = write_project(tmp_path, {
        "app/__init__.py": "", "app/repo.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n",
        "app/order_service.py": "from app.repo import OrderRepo\ndef place(o):\n    OrderRepo().save_order(o)\n"
                                "def place2(o):\n    OrderRepo().save_order(o)\n",
        "app/api.py": "from app.order_service import place, place2\ndef a(o):\n    place(o)\ndef b(o):\n    place2(o)\n",
        "app/plug.py": "import importlib\nfrom . import missing\ndef load(n):\n    importlib.import_module(n)\n"})
    found = doors.discover([root])
    doors.apply_strict(found)            # drafting allow_unresolved is `inv init --strict-scan`'s
    d = found.doors[0]
    assert d.back_doors == [] and len(d.unresolved) == 2 and not d.active, (d.back_doors, d.unresolved)
    text = "\n".join(doors.render(found))
    assert "2 unresolved site(s) to review" in text and 'why: ""' in text and "back door" not in text.split("- id:")[0]
    assert any("2 unresolved site(s) to review" in line for line in doors.summary(found))


# --- H. relative-import message names the indexed package ------------------------------------------------------------
def test_a_relative_import_problem_names_the_indexed_package(tmp_path):
    root = write_project(tmp_path, {"src/pkg/__init__.py": "", "src/pkg/sub/__init__.py": "",
                                    "src/pkg/sub/mod.py": "from .missing import x\n"})
    rep = scan.scan(PAY_RULE, [root, root / "src"])
    msgs = [f.message for f in rep.findings if f.code == "unresolved/relative-import"]
    assert msgs and "(pkg.sub.missing)" in msgs[0] and "src.pkg" not in msgs[0], rep.findings


def test_module_tree_reuse_gives_the_parsed_statement(tmp_path):
    p = write_project(tmp_path, {"a.py": "x = 1\ny = f(\n    1,\n)\n"})
    tree = ast.parse((p / "a.py").read_text())
    assert accept._Lines({p / "a.py": tree}).statement(str(p), "a.py", 3, 5) == \
        accept._Lines().statement(str(p), "a.py", 3, 5)
