"""Scan, fix round 26: false passes through values the scan did not see as values.

A. A class-body alias of a method (`post = record`) is the method under another name; a dunder (def or
   alias) is called by Python from syntax (`a + b`, `x[k]`, `with`, `len()`), so it is an entrypoint; any other
   class-body use of a method (`{"post": record}`, `property(record)`) is a value.
D. A decorator that may keep the function (`@app.post(...)`, `@atexit.register`, a scanned registry) is
   handed it as a value.
M. A scanned module used as a value hands out its functions; a call on a receiver the scan cannot type may
   be a module's function as well as a method.
K. A class used as a value runs its constructors when called.
G. A computed getattr is never narrowed on an object outside the scan, and on a scanned class only when each
   base outside the scan is a known plain one. A module-level name bound once to a constructor is typed.
"""
from __future__ import annotations

import textwrap

import pytest
from cli_support import write_project

from invariants import scan

PAYMENTS = """
    class Sender:
        def __init__(self, c, a):
            send_to_gateway(c, a)

    def send_to_gateway(charge_id, amount):
        pass

    def _send(charge_id, amount):
        send_to_gateway(charge_id, amount)

    def refund(charge_id, amount):
        _send(charge_id, amount)
        Sender(charge_id, amount)
    """
STORE = """
    class Store:
        def save(self, x):
            return x
    """
DOC = {"rules": [
    {"id": "PAY-1", "status": "candidate", "subject": {"target": "py:payments.refund"}, "bad": "false",
     "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
     "mediation": {"sink": "py:payments.send_to_gateway", "door": "py:payments.refund"}},
    {"id": "SAVE-1", "status": "candidate", "subject": {"target": "py:svc.create"}, "bad": "false",
     "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
     "mediation": {"sink": "py:store.Store.save", "door": "py:svc.create"}}]}


def _scan(tmp_path, files, doc=DOC):
    base = {"payments.py": PAYMENTS, "store.py": STORE, "svc.py": "def create(x):\n    pass\n"}
    files = {**base, **files}
    rep = scan.scan(doc, [write_project(tmp_path, {k: textwrap.dedent(v) for k, v in files.items()})])
    assert not [f for f in rep.findings if f.code == "unresolved/parse-error"], rep.findings
    return rep


def _svc(body: str, door: str = "return Ledger().record(x)") -> dict[str, str]:
    """svc.py: class Ledger with record() reaching the sink, the body lines in the class, the door create()."""
    return {"svc.py": "from store import Store\n\n\nclass Ledger:\n    def record(self, *a, **k):\n"
                      "        return Store().save(a)\n" + textwrap.indent(textwrap.dedent(body), "    ") +
                      f"\n\ndef create(x):\n    {door}\n"}


# --- A: class bodies ---------------------------------------------------------------------------------------
def test_a_class_body_alias_is_the_method_under_another_name(tmp_path):
    files = _svc("post = record\n")
    files["ops.py"] = "from svc import Ledger\ndef evil(x):\n    return Ledger().post(x)\n"
    rep = _scan(tmp_path, files)
    assert [f.code for f in rep.for_rule("SAVE-1")] == ["bypass"], rep.for_rule("SAVE-1")
    assert rep.for_rule("SAVE-1")[0].path == ["ops.evil", "svc.Ledger.record", "store.Store.save"]


@pytest.mark.parametrize("dunder, use", [
    ("__iadd__", "led += x"), ("__add__", "led + x"), ("__getitem__", "led[x]"), ("__call__", "led(x)"),
    ("__enter__", "with led:\n        pass"), ("__len__", "len(led)"), ("__str__", "f'{led}'"),
    ("__contains__", "x in led"), ("__eq__", "led == x"), ("__iter__", "list(led)"),
    ("__getattr__", "led.anything"), ("__setitem__", "led[x] = 1"), ("__bool__", "bool(led)")])
def test_a_dunder_alias_is_an_entrypoint(tmp_path, dunder, use):
    files = _svc(f"{dunder} = record\n")
    files["ops.py"] = f"from svc import Ledger\ndef evil(x):\n    led = Ledger()\n    {use}\n"
    rep = _scan(tmp_path, files)
    got = [f for f in rep.for_rule("SAVE-1") if f.code == "unresolved/sink-escapes"]
    assert got and got[0].file == "svc.py", rep.for_rule("SAVE-1")


@pytest.mark.parametrize("use", ["Box() + x", "Box()[x]", "Box() == x", "f'{Box()}'", "len(Box())",
                                 "[_ for _ in Box()]", "bool(Box())", "x in Box()"])
def test_a_dunder_the_door_also_calls_is_still_an_entrypoint(tmp_path, use):
    methods = "".join(f"    def {d}(self, *a):\n        return Store().save(a)\n" for d in (
        "__add__", "__getitem__", "__eq__", "__str__", "__len__", "__iter__", "__bool__", "__contains__"))
    calls = "".join(f"    b.{d}(x)\n" for d in ("__add__", "__getitem__", "__eq__", "__str__", "__len__",
                                                   "__iter__", "__bool__", "__contains__"))
    files = {"svc.py": f"from store import Store\nclass Box:\n{methods}\ndef create(x):\n    b = Box()\n{calls}",
             "ops.py": f"from svc import Box\ndef evil(x):\n    return {use}\n"}
    rep = _scan(tmp_path, files)
    assert rep.rule_result("SAVE-1") != "pass", rep.for_rule("SAVE-1")


@pytest.mark.parametrize("body, use", [
    ("HANDLERS = {'post': record}\n", "Ledger.HANDLERS['post'](Ledger(), x)"),
    ("total = property(record)\n", "Ledger().total"),
    ("go = staticmethod(record)\n", "Ledger.go(Ledger(), x)"),
    ("table = [record]\n", "Ledger.table[0](Ledger(), x)")])
def test_any_other_class_body_use_of_a_method_is_a_value(tmp_path, body, use):
    files = _svc(body)
    files["ops.py"] = f"from svc import Ledger\ndef evil(x):\n    return {use}\n"
    rep = _scan(tmp_path, files)
    assert "unresolved/sink-escapes" in [f.code for f in rep.for_rule("SAVE-1")], rep.for_rule("SAVE-1")


def test_a_subclass_alias_overrides_the_method(tmp_path):
    # through an annotation a subclass may arrive, and its alias of another function replaces record
    files = {"svc.py": "from store import Store\ndef persist(self, *a):\n    return Store().save(a)\n"
                       "class Ledger:\n    def record(self, *a):\n        return None\n"
                       "class Sub(Ledger):\n    record = persist\n"
                       "def create(x):\n    return persist(None, x)\n",
             "ops.py": "from svc import Ledger\ndef evil(led: Ledger, x):\n    return led.record(x)\n"}
    rep = _scan(tmp_path, files)
    assert [(f.code, f.path[0]) for f in rep.for_rule("SAVE-1")] == [("bypass", "ops.evil")], rep.for_rule("SAVE-1")


def test_a_class_body_alias_of_a_name_bound_twice_is_a_value(tmp_path):
    ops = ("try:\n    from payments import _send as h\nexcept ImportError:\n    from payments import refund as h\n"
           "class K:\n    go = h\nK.go(1, 2)\n")
    rep = _scan(tmp_path, {"ops.py": ops})
    assert rep.rule_result("PAY-1") != "pass", rep.for_rule("PAY-1")


def test_a_class_body_name_is_looked_up_in_the_class_first(tmp_path):
    # `record` in the class body is the method, not the module function of the same name
    files = {"svc.py": "from store import Store\ndef record(*a):\n    return None\n"
                       "class Ledger:\n    def record(self, *a):\n        return Store().save(a)\n"
                       "    H = {'x': record}\n"
                       "def create(x):\n    return Ledger().record(x)\n"}
    rep = _scan(tmp_path, files)
    assert "unresolved/sink-escapes" in [f.code for f in rep.for_rule("SAVE-1")], rep.for_rule("SAVE-1")


def test_a_comprehension_in_a_class_body_does_not_see_the_class_names(tmp_path):
    # only the first iterable sees the class body; `record` inside the comprehension is the module's
    files = {"svc.py": "from store import Store\ndef record(*a):\n    return Store().save(a)\n"
                       "class Ledger:\n    def record(self, *a):\n        return None\n"
                       "    H = [record for _ in range(1)]\n"
                       "def create(x):\n    return record(x)\n"}
    rep = _scan(tmp_path, files)
    assert "unresolved/sink-escapes" in [f.code for f in rep.for_rule("SAVE-1")], rep.for_rule("SAVE-1")


def test_quiet_dunders_and_aliases(tmp_path):
    files = {"svc.py": "from store import Store\nclass Box:\n    def __init__(self):\n        self.s = Store()\n"
                       "    def __repr__(self):\n        return 'Box'\n    def __eq__(self, o):\n        return True\n"
                       "    def write(self, x):\n        return self.s.save(x)\n    put = write\n"
                       "def create(x):\n    return Box().put(x)\n",
             "ops.py": "from svc import Box, create\ndef f(x):\n    b = Box()\n    return repr(b), b == x, create(x)\n"}
    rep = _scan(tmp_path, files)
    assert rep.rule_result("SAVE-1") == "pass", rep.for_rule("SAVE-1")


# --- D: decorators -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("deco, extra", [
    ("@app.post('/internal/issue')", {"web.py": "from fastapi import FastAPI\napp = FastAPI()\n"}),
    ("@hooks.register", {"hooks.py": "HOOKS = []\ndef register(f):\n    HOOKS.append(f)\n    return f\n",
                          "ops.py": "import hooks\ndef go():\n    for h in hooks.HOOKS:\n        h(1, 2)\n"}),
    ("@atexit.register", {}),
    ("@functools.partial(print)", {}),
])
def test_a_decorator_that_may_keep_the_function_is_handed_it_as_a_value(tmp_path, deco, extra):
    payments = PAYMENTS.replace("    def _send(", f"    {deco}\n    def _send(")
    head = "import atexit, functools, hooks\nfrom web import app\n"
    files = {"payments.py": head + textwrap.dedent(payments), "hooks.py": "def register(f):\n    return f\n",
             "web.py": "app = None\n", **extra}
    rep = _scan(tmp_path, files)
    got = [f for f in rep.for_rule("PAY-1") if f.code == "unresolved/sink-escapes"]
    assert got and got[0].file == "payments.py", rep.for_rule("PAY-1")


@pytest.mark.parametrize("deco", [
    "@functools.lru_cache", "@functools.lru_cache(maxsize=2)", "@functools.cache", "@guard", "@guard_with('x')",
    "@contextlib.contextmanager", "@typing.overload", "@inv.guard('PAY-1')"])
def test_a_decorator_that_keeps_nothing_is_quiet(tmp_path, deco):
    payments = PAYMENTS.replace("    def _send(", f"    {deco}\n    def _send(")
    head = """
        import contextlib, functools, typing
        import invariants as inv

        def guard(f):
            @functools.wraps(f)
            def w(*a, **k):
                return f(*a, **k)
            return w

        def guard_with(name):
            def deco(f):
                def w(*a):
                    return f(*a)
                return w
            return deco
        """
    rep = _scan(tmp_path, {"payments.py": textwrap.dedent(head) + textwrap.dedent(payments)})
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")


def test_a_route_that_does_not_reach_the_sink_is_quiet(tmp_path):
    files = {"web.py": "from fastapi import FastAPI\nfrom payments import refund\napp = FastAPI()\n"
                       "@app.get('/health')\ndef health():\n    return 'ok'\n"
                       "@app.post('/refund')\ndef do_refund(c: str, a: int):\n    return refund(c, a)\n"}
    rep = _scan(tmp_path, files)
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")


def test_an_allowed_route_is_not_a_new_caller_of_itself(tmp_path):
    doc = {"rules": [dict(DOC["rules"][0], mediation={**DOC["rules"][0]["mediation"],
                                                      "allow": ["py:web.quick"]})]}
    files = {"web.py": "from fastapi import FastAPI\nfrom payments import send_to_gateway\napp = FastAPI()\n"
                       "@app.post('/quick')\ndef quick(c: str, a: int):\n    send_to_gateway(c, a)\n"}
    rep = _scan(tmp_path, files, doc)
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")


def test_a_property_setter_is_an_entrypoint(tmp_path):
    files = _svc("@property\ndef total(self):\n    return 1\n@total.setter\ndef total(self, v):\n"
                 "    Store().save(v)\n", door="led = Ledger()\n    led.total = x")
    files["ops.py"] = "from svc import Ledger\ndef evil(x):\n    Ledger().total = x\n"
    rep = _scan(tmp_path, files)
    assert rep.rule_result("SAVE-1") != "pass", rep.for_rule("SAVE-1")


def test_a_property_read_on_an_unknown_receiver_may_run_a_getter(tmp_path):
    files = _svc("@property\ndef total(self):\n    return Store().save(1)\n", door="return Ledger().total")
    files["ops.py"] = "def evil(led):\n    return led.total\n"
    rep = _scan(tmp_path, files)
    got = [f for f in rep.for_rule("SAVE-1") if f.code == "unresolved/inferred-path"]
    assert got and got[0].path[0] == "ops.evil", rep.for_rule("SAVE-1")


# --- M: modules as values ----------------------------------------------------------------------------------
MODULE_VALUES = {
    "dict": "import payments\nB = {'p': payments}\ndef go(k):\n    B[k]._send(1, 2)\n",
    "local": "import payments\ndef go():\n    m = payments\n    m._send(1, 2)\n",
    "param": "import payments\ndef call(m):\n    m._send(1, 2)\ndef go():\n    call(payments)\n",
    "self": "import payments\nclass Safe:\n    def __init__(self):\n        self.mod = payments\n"
            "    def go(self):\n        self.mod._send(1, 2)\n",
    "getattr": "import payments\nclass Safe:\n    def __init__(self):\n        self.mod = payments\n"
               "    def go(self, n):\n        getattr(self, n)._send(1, 2)\n",
    "class-body-import": "class Safe:\n    import payments\n    def go(self, n):\n        getattr(self, n)._send(1, 2)\n",
    "class-body-import2": "class Safe:\n    import payments\ndef go():\n    Safe.payments._send(1, 2)\n",
    "tuple": "import payments, json\ndef go(i):\n    m = (json, payments)[i]\n    m._send(1, 2)\n",
    "dunder-dict": "import payments\ndef go(k):\n    m = payments\n    m.__dict__[k](1, 2)\n",
    "unknown": "def go(m):\n    m._send(1, 2)\n",
    "methodcaller": "import operator\ndef go(m):\n    operator.methodcaller('_send', 1, 2)(m)\n",
    "attrgetter": "import operator\ndef go(m):\n    operator.attrgetter('x._send')(m)(1, 2)\n",
}


@pytest.mark.parametrize("name", sorted(MODULE_VALUES))
def test_a_module_used_as_a_value_hands_out_its_functions(tmp_path, name):
    rep = _scan(tmp_path, {"ops.py": MODULE_VALUES[name]})
    assert rep.rule_result("PAY-1") != "pass", rep.for_rule("PAY-1")


def test_a_package_of_backends_in_a_dict(tmp_path):
    files = {"app/__init__.py": "", "app/gateway.py": "def send_to_gateway(c, a):\n    pass\n",
             "app/backends/__init__.py": "",
             "app/backends/stripe.py": "from app.gateway import send_to_gateway\ndef charge(c, a):\n"
                                       "    send_to_gateway(c, a)\n",
             "app/billing.py": "from app.backends import stripe\nBACKENDS = {'stripe': stripe}\n"
                               "def refund(c, a):\n    stripe.charge(c, a)\n",
             "app/admin.py": "from app.billing import BACKENDS\ndef goodwill(k, c, a):\n    BACKENDS[k].charge(c, a)\n"}
    doc = {"rules": [dict(DOC["rules"][0], mediation={"sink": "py:app.gateway.send_to_gateway",
                                                      "door": "py:app.billing.refund"})]}
    rep = scan.scan(doc, [write_project(tmp_path, files)])
    assert rep.rule_result("PAY-1") != "pass", rep.for_rule("PAY-1")
    assert {f.file for f in rep.for_rule("PAY-1")} >= {"app/billing.py"}, rep.for_rule("PAY-1")


def test_a_module_attribute_written_or_read_is_not_the_module_as_a_value(tmp_path):
    rep = _scan(tmp_path, {"ops.py": "import payments\ndef f(c):\n    payments.refund(c, 1)\n"
                                     "    payments.LIMIT = 3\n    return payments.__name__, hasattr(payments, 'x')\n"})
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")


# --- K: classes as values ----------------------------------------------------------------------------------
CLASS_VALUES = {
    "thread": "import payments, threading\ndef go():\n    threading.Thread(target=payments.Sender, args=(1, 2)).start()\n",
    "dict": "import payments\nH = {'x': payments.Sender}\ndef go(k, c, a):\n    H[k](c, a)\n",
    "attr": "import payments\nclass Safe:\n    def __init__(self):\n        self.op = payments.Sender\n"
            "    def run(self):\n        return self.op(1, 2)\n",
    "getattr": "import payments\nclass Safe:\n    def __init__(self):\n        self.op = payments.Sender\n"
               "    def run(self, name):\n        return getattr(self, name)(1, 2)\n",
    "decorated": "import payments\nREG = []\ndef reg(c):\n    REG.append(c)\n    return c\n"
                 "@reg\nclass Leaf(payments.Sender):\n    pass\n",
}


@pytest.mark.parametrize("name", sorted(CLASS_VALUES))
def test_a_class_used_as_a_value_runs_its_constructor(tmp_path, name):
    rep = _scan(tmp_path, {"ops.py": CLASS_VALUES[name]})
    assert rep.rule_result("PAY-1") != "pass", rep.for_rule("PAY-1")


def test_constructors_run_where_the_class_is_called(tmp_path):
    files = {"svc.py": "import dataclasses\nfrom store import Store\n@dataclasses.dataclass\nclass Rec:\n"
                       "    x: int\n    def __post_init__(self):\n        Store().save(self.x)\n"
                       "def create(x):\n    return Rec(x).__post_init__()\n",
             "ops.py": "from svc import Rec\ndef evil(x):\n    return Rec(x)\n"}
    rep = _scan(tmp_path, files)
    assert [f.code for f in rep.for_rule("SAVE-1")] == ["bypass"], rep.for_rule("SAVE-1")


@pytest.mark.parametrize("use", ["type(obj)(1, 2)", "obj.__class__(1, 2)", "copy.copy(obj)",
                                 "dataclasses.replace(obj)"])
def test_another_instance_of_an_objects_class_runs_its_constructors(tmp_path, use):
    rep = _scan(tmp_path, {"ops.py": f"import copy, dataclasses\ndef evil(obj):\n    return {use}\n"})
    got = [f for f in rep.for_rule("PAY-1") if f.code == "unresolved/inferred-path"]
    assert got and got[0].path[:2] == ["ops.evil", "payments.Sender.__init__"], rep.for_rule("PAY-1")


def test_another_instance_of_a_typed_objects_class(tmp_path):
    files = {"svc.py": "from store import Store\nclass Rec:\n    def __init__(self, x):\n        Store().save(x)\n"
                       "    def again(self):\n        return type(self)(1)\n"
                       "class Safe:\n    def __init__(self, x):\n        pass\n"
                       "def create(x):\n    return Rec(x)\n",
             "ops.py": "from svc import Rec, Safe\ndef evil(r: Rec):\n    return r.again()\n"
                       "def fine(s: Safe):\n    return type(s)(1)\n"}
    rep = _scan(tmp_path, files)
    assert [(f.code, f.path[0]) for f in rep.for_rule("SAVE-1")] == [("bypass", "ops.evil")], rep.for_rule("SAVE-1")


def test_a_class_only_tested_or_inherited_is_not_a_value(tmp_path):
    ops = ("import payments\nfrom typing import Optional\nclass Leaf(payments.Sender):\n    pass\n"
           "def f(x: 'Optional[payments.Sender]'):\n    if isinstance(x, payments.Sender) or type(x) is payments.Sender:\n"
           "        return 1\n    try:\n        pass\n    except (TypeError, ValueError):\n        pass\n"
           "    match x:\n        case payments.Sender():\n            return 2\n"
           "    return Optional[payments.Sender], issubclass(Leaf, payments.Sender)\n")
    rep = _scan(tmp_path, {"ops.py": ops})
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")


def test_subclasses_are_reflection(tmp_path):
    rep = _scan(tmp_path, {"ops.py": "def f(n):\n    for c in object.__subclasses__():\n        if c.__name__ == n:\n"
                                     "            c(1, 2)\n"})
    assert "unresolved/reflection" in [f.code for f in rep.for_rule("PAY-1")], rep.for_rule("PAY-1")


# --- G: computed getattr narrowing -------------------------------------------------------------------------
REPO_DOC = {"rules": [dict(DOC["rules"][0], mediation={"sink": "py:repo.Repo.send_to_gateway",
                                                       "door": "py:repo.refund"})]}
REPO = "class Repo:\n    def send_to_gateway(self, c, a):\n        pass\ndef refund(c, a):\n    Repo().send_to_gateway(c, a)\n"


@pytest.mark.parametrize("ops", [
    "import builtins, repo\ndef f(c, a):\n    fn = 'get' + 'attr'\n    getattr(builtins, fn)(repo.Repo(), 'send_to_gateway')(c, a)\n",
    "import operator, repo\ndef f(fn, m):\n    return getattr(operator, fn)(m, 1, 2)(repo.Repo())\n",
    "import importlib\ndef f(fn, mod):\n    return getattr(importlib, fn)(mod)\n",
    "import sys\ndef f(attr, key):\n    return getattr(sys, attr)[key]\n",
    "import logging\ndef f(name):\n    return getattr(logging, name)\n",
    "import requests\ndef f(name):\n    return getattr(requests, name)\n",
    ("import tempfile, repo\nclass W(tempfile._TemporaryFileWrapper):\n    pass\n"
     "def f(n):\n    return getattr(W(repo.Repo(), 'x', delete=False), n)(1, 2)\n"),
    "import tornado.web\nclass H(tornado.web.RequestHandler):\n    def go(self, n):\n        return getattr(self, n)()\n",
])
def test_a_computed_getattr_that_may_reach_reflection_is_reported(tmp_path, ops):
    rep = _scan(tmp_path, {"repo.py": REPO, "ops.py": ops}, REPO_DOC)
    assert "unresolved/computed-attribute" in [f.code for f in rep.for_rule("PAY-1")], rep.for_rule("PAY-1")


@pytest.mark.parametrize("base", ["", "Exception", "enum.Enum", "pydantic.BaseModel", "abc.ABC", "dict"])
def test_a_computed_getattr_on_a_plain_family_is_narrowed(tmp_path, base):
    ops = (f"import abc, enum, pydantic\nclass H({base}):\n    def ping(self):\n        return 1\n"
           "    def go(self, n):\n        return getattr(self, n)()\n")
    rep = _scan(tmp_path, {"repo.py": REPO, "ops.py": ops}, REPO_DOC)
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")


SETTINGS = ("import pydantic\nclass Settings(pydantic.BaseModel):\n    level: str = 'INFO'\n"
            "settings = Settings()\ndef get(key):\n    return getattr(settings, key)\n")


def test_a_module_level_instance_bound_once_is_typed(tmp_path):
    rep = _scan(tmp_path, {"repo.py": REPO, "ops.py": SETTINGS,
                           "use.py": "from ops import settings\nimport ops\ndef f(k):\n"
                                     "    return getattr(settings, k), getattr(ops.settings, k)\n"}, REPO_DOC)
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")


@pytest.mark.parametrize("other, more", [
    ("import ops, repo\nops.settings = repo.Repo()\n", ""),           # set from outside
    ("", "settings = 1\n"),                                           # bound twice
    ("", "def g():\n    global settings\n    settings = 2\n"),          # rebound through global
    ("", "def g(k, v):\n    globals()[k] = v\n")])                     # names the scan cannot list
def test_a_module_level_instance_rebound_is_not_typed(tmp_path, other, more):
    rep = _scan(tmp_path, {"repo.py": REPO, "ops.py": SETTINGS + more, "other.py": other}, REPO_DOC)
    assert "unresolved/computed-attribute" in [f.code for f in rep.for_rule("PAY-1")], rep.for_rule("PAY-1")
