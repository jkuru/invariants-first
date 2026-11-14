"""Scan, fix round 27: false passes from how names bind and how methods are looked up.

I. An import binds the scope it is in: one in a function or class body is not a module attribute, and a
   name imported differently in a body is not resolved to one of its objects.
L. A function's locals are its own scope's: a nested scope's parameters, bindings and `global` / `nonlocal`
   declarations, and a comprehension's targets, stay in that scope; a match capture binds a local.
C. A method is looked up along the C3 method resolution order, not depth first.
B. A module-level class name rebound by a star import or a `case {**name}` capture is not trusted.
W. A decorator whose wrapper is itself handed to another decorator (`@register`) may keep the function.
P. Writing `__code__` / `__defaults__` / `__kwdefaults__` replaces what a function runs.
T. A project module imported under a deeper sys.path name (`fleet.store` for cli/fleet/store.py) is not an
   object outside the scan.
"""
from __future__ import annotations

import textwrap

import pytest
from scan_support import write_project

import invariants_scan as scan

PAYMENTS = """
    def send_to_gateway(c, a):
        pass

    def refund(c, a):
        send_to_gateway(c, a)
    """
STORE = """
    class Store:
        def save(self, x):
            return x

    class Other:
        def save(self, x):
            return x
    """


def _rule(sink: str, door: str | list[str]) -> dict:
    return {"rules": [{"id": "R", "status": "candidate",
                       "subject": {"target": door if isinstance(door, str) else door[0]}, "bad": "false",
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
                       "mediation": {"sink": sink, "door": door}}]}


PAY = _rule("py:payments.send_to_gateway", "py:payments.refund")
SAVE = _rule("py:store.Store.save", "py:svc.create")


def _scan(tmp_path, files, doc=PAY):
    base = {"payments.py": PAYMENTS, "store.py": STORE, "svc.py": "def create(x):\n    pass\n"}
    files = {**base, **files}
    rep = scan.scan(doc, [write_project(tmp_path, {k: textwrap.dedent(v) for k, v in files.items()})])
    assert not [f for f in rep.findings if f.code == "unresolved/parse-error"], rep.findings
    return [(f.code, f.file.as_posix() if hasattr(f.file, "as_posix") else f.file, f.line, f.path)
            for f in rep.for_rule("R")]


def _codes(found):
    return sorted({c for c, *_ in found})


# --- I: imports bind their own scope ---------------------------------------------------------------------
def test_i_an_import_in_a_function_is_not_the_module_attribute(tmp_path):
    ops = """
        from payments import send_to_gateway as go

        def helper():
            from payments import refund as go
        """
    for main in ("from ops import go\n\ndef evil():\n    go(1, 2)\n", "import ops\n\ndef evil():\n    ops.go(1, 2)\n"):
        found = _scan(tmp_path, {"ops.py": ops, "main.py": main})
        assert [(c, p) for c, _, _, p in found] == [("bypass", ["main.evil", "payments.send_to_gateway"])], found


def test_i_a_class_body_import_is_not_the_module_attribute(tmp_path):
    ops = "from payments import send_to_gateway as go\n\nclass K:\n    from payments import refund as go\n"
    found = _scan(tmp_path, {"ops.py": ops, "main.py": "from ops import go\n\ndef evil():\n    go(1, 2)\n"})
    assert [(c, p) for c, _, _, p in found] == [("bypass", ["main.evil", "payments.send_to_gateway"])], found


def test_i_the_module_binding_types_an_importer_whatever_a_body_imports(tmp_path):
    ops = "from store import Store as S\n\ndef helper():\n    from store import Other as S\n    return S\n"
    found = _scan(tmp_path, {"ops.py": ops, "main.py": "from ops import S\n\ndef evil():\n    S().save(1)\n"}, SAVE)
    assert [(c, p) for c, _, _, p in found] == [("bypass", ["main.evil", "store.Store.save"])], found


@pytest.mark.parametrize("ops", [
    "from store import Other as S\n\ndef evil():\n    from store import Store as S\n    S().save(1)\n",
    "def evil():\n    from store import Store as S\n    S().save(1)\n\ndef fine():\n    from store import Other as S\n"
    "    S().save(1)\n",
    "S = len\n\ndef evil():\n    from store import Store as S\n    S().save(1)\n"])
def test_i_a_name_a_body_imports_as_another_object_is_not_typed(tmp_path, ops):
    found = _scan(tmp_path, {"ops.py": ops}, SAVE)
    assert ("unresolved/inferred-path", "ops.py", ops.splitlines().index("    S().save(1)") + 1,
            ["ops.evil", "store.Store.save"]) in found, found


def test_i_a_name_bound_twice_at_module_scope_reaches_both_through_an_importer(tmp_path):
    ops = """
        import os
        if os.environ.get("X"):
            from payments import send_to_gateway as go
        else:
            from payments import refund as go
        """
    for main in ("from ops import go\n\ndef evil():\n    go(1, 2)\n", "import ops\n\ndef evil():\n    ops.go(1, 2)\n",
                 "from ops import *\n\ndef evil():\n    go(1, 2)\n"):
        found = _scan(tmp_path, {"ops.py": ops, "main.py": main})
        assert [(c, p) for c, _, _, p in found] == [("bypass", ["main.evil", "payments.send_to_gateway"])], found


@pytest.mark.parametrize("ops", [
    """
    import os, threading
    if os.environ.get("X"):
        from payments import Quiet as K
    else:
        from payments import Sender as K

    def evil():
        threading.Thread(target=K)
    """,
    """
    import threading
    from payments import Quiet as K

    def evil():
        from payments import Sender as K
        threading.Thread(target=K)
    """])
def test_i_every_class_a_name_may_be_is_used_as_a_value(tmp_path, ops):
    pay = PAYMENTS + """
    class Sender:
        def __init__(self):
            send_to_gateway(1, 2)

    class Quiet:
        def __init__(self):
            pass

    def door2():
        Sender()
    """
    found = _scan(tmp_path, {"payments.py": pay, "ops.py": ops},
                  _rule("py:payments.send_to_gateway", ["py:payments.refund", "py:payments.door2"]))
    assert _codes(found) == ["unresolved/sink-escapes"], found
    assert found[0][3] == ["ops.evil", "payments.Sender.__init__", "payments.send_to_gateway"], found


# --- L: locals are a scope's own ---------------------------------------------------------------------------
@pytest.mark.parametrize("nested", [
    "f = lambda go: go",
    "def inner(go):\n        return go",
    "def inner():\n        go = 1",
    "ys = [go for go in xs]",
    "class K:\n        go = 1",
    "def inner():\n        global go\n        go = len"])
def test_l_a_nested_scope_binding_does_not_hide_the_owners_call(tmp_path, nested):
    ops = f"from payments import send_to_gateway as go\n\ndef evil(xs):\n    {nested}\n    go(1, 2)\n"
    found = _scan(tmp_path, {"ops.py": ops})
    assert [(c, p) for c, _, _, p in found] == [("bypass", ["ops.evil", "payments.send_to_gateway"])], found


def test_l_a_nested_global_is_the_module_name_there(tmp_path):
    ops = """
        from payments import send_to_gateway as go

        def evil():
            go = 1
            def inner():
                global go
                go(1, 2)
            inner()
        """
    found = _scan(tmp_path, {"ops.py": ops})
    assert [(c, p) for c, _, _, p in found] == [("bypass", ["ops.evil", "payments.send_to_gateway"])], found


@pytest.mark.parametrize("decl", ["global", "nonlocal"])
def test_l_a_nested_declaration_keeps_the_owners_local(tmp_path, decl):
    ops = f"""
        from store import Store, Other as repo

        def evil(repo: Store):
            def inner():
                {decl} repo
            repo.save(1)
        """
    found = _scan(tmp_path, {"ops.py": ops}, SAVE)
    assert [p for *_, p in found] == [["ops.evil", "store.Store.save"]], found


@pytest.mark.parametrize("pat", ["case repo:", "case [*repo]:", "case {**repo}:", "case Store() as repo:"])
def test_l_a_match_capture_is_a_local(tmp_path, pat):
    ops = f"""
        from store import Store, Other as repo

        def evil(x):
            match x:
                {pat}
                    repo.save(1)
        """
    found = _scan(tmp_path, {"ops.py": ops}, SAVE)
    assert [(c, p) for c, _, _, p in found] == [("unresolved/inferred-path", ["ops.evil", "store.Store.save"])], found


# --- C: C3 method lookup -----------------------------------------------------------------------------------
@pytest.mark.parametrize("call", ["def evil():\n    D().m()\n", "def evil(d: D):\n    d.m()\n"])
def test_c_a_diamond_override_is_found_by_the_mro(tmp_path, call):
    ops = """
        from payments import send_to_gateway

        class A:
            def m(self):
                return 1

        class B(A):
            pass

        class C(A):
            def m(self):
                send_to_gateway(1, 2)

        class D(B, C):
            pass

        def door():
            C().m()
        """
    found = _scan(tmp_path, {"ops.py": textwrap.dedent(ops) + call},
                  _rule("py:payments.send_to_gateway", ["py:payments.refund", "py:ops.door"]))
    assert [(c, p) for c, _, _, p in found] == [("bypass", ["ops.evil", "ops.C.m", "payments.send_to_gateway"])], found


# --- B: class names bound again -----------------------------------------------------------------------------
@pytest.mark.parametrize("ops", [
    "class Safe:\n    def save(self, x):\n        return x\n\nfrom shadow import *\n\ndef evil():\n    Safe().save(1)\n",
    "import os\n\nclass Safe:\n    def save(self, x):\n        return x\n\nmatch os.environ:\n    case {**Safe}:\n"
    "        pass\n\ndef evil():\n    Safe().save(1)\n"])
def test_b_a_class_name_bound_again_is_not_trusted(tmp_path, ops):
    found = _scan(tmp_path, {"shadow.py": "from store import Store as Safe\n", "ops.py": ops}, SAVE)
    assert [(c, p) for c, _, _, p in found] == [("unresolved/inferred-path", ["ops.evil", "store.Store.save"])], found


@pytest.mark.parametrize("own", ["from store import Store\n", "import store\nStore = store.Store\n"])
def test_b_a_star_import_of_the_same_class_binds_nothing_new(tmp_path, own):
    ops = f"{own}from shadow import *\n\ndef evil():\n    Store().save(1)\n"
    found = _scan(tmp_path, {"shadow.py": "from store import Store\n", "ops.py": ops}, SAVE)
    assert [(c, p) for c, _, _, p in found] == [("bypass", ["ops.evil", "store.Store.save"])], found


# --- W: a wrapper handed to another decorator ---------------------------------------------------------------
@pytest.mark.parametrize("deco, kept", [("register", True), ("atexit.register", True), ("functools.wraps(f)", False)])
def test_w_a_wrapper_decorated_by_a_keeping_decorator_keeps_the_function(tmp_path, deco, kept):
    ops = f"""
        import functools, atexit
        from payments import send_to_gateway

        HOOKS = []

        def register(w):
            HOOKS.append(w)
            return w

        def deco(f):
            @{deco}
            def wrapper(*a):
                return f(*a)
            return wrapper

        @deco
        def helper():
            send_to_gateway(1, 2)

        def door():
            helper()

        def later():
            for h in HOOKS:
                h()
        """
    found = _scan(tmp_path, {"ops.py": ops}, _rule("py:payments.send_to_gateway", ["py:payments.refund", "py:ops.door"]))
    assert _codes(found) == (["unresolved/sink-escapes"] if kept else []), found


# --- P: code and defaults writes ----------------------------------------------------------------------------
@pytest.mark.parametrize("body, code", [
    ("setattr(payments.refund, '__code__', other.__code__)", "forbidden/monkeypatch"),
    ("setattr(payments.refund, '__defaults__', (1,))", "forbidden/monkeypatch"),
    ("delattr(payments.refund, '__kwdefaults__')", "forbidden/monkeypatch"),
    ("payments.refund.__code__ = other.__code__", "forbidden/monkeypatch"),
    ("f = payments.refund\n    f.__code__ = other.__code__", "unresolved/unknown-receiver"),
    ("f = payments.refund\n    setattr(f, '__code__', other.__code__)", "unresolved/unknown-receiver"),
    ("x.__defaults__ = (1,)", "unresolved/unknown-receiver")])
def test_p_a_code_write_replaces_what_a_door_runs(tmp_path, body, code):
    ops = f"import payments\n\ndef other():\n    pass\n\ndef evil(x):\n    {body}\n"
    assert _codes(_scan(tmp_path, {"ops.py": ops})) == [code]


# --- T: a project module under a deeper sys.path name --------------------------------------------------------
def test_t_an_unwrap_through_a_deeper_module_name_is_reported(tmp_path):
    store = """
        import functools
        import payments

        def deco(f):
            @functools.wraps(f)
            def w(*a):
                return f(*a)
            return w

        @deco
        def save(c, a):
            payments.send_to_gateway(c, a)

        def door():
            save(1, 2)
        """
    main = "import fleet.store\n\ndef run():\n    fleet.store.save.__wrapped__(1, 2)\n"
    found = _scan(tmp_path, {"cli/fleet/store.py": store, "cli/main.py": main},
                  _rule("py:payments.send_to_gateway", ["py:payments.refund", "py:cli.fleet.store.save"]))
    assert [(c, f, ln) for c, f, ln, _ in found] == [("forbidden/unwrap", "cli/main.py", 4)], found
