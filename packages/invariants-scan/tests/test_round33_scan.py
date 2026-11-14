"""Round 33 (scan): nested callables, replaced methods, positional-only self, and scoping.

H1. A nested function, lambda or class defined in a door (or an allowed function) was its owner's code, so
    `def refund(): def inner(): sink(); return inner` let `refund()()` reach the sink with no finding.
H2. A method replaced by a scanned function (`Helper.run = _send`) stayed typed to the original, and
    `type(x).refund = v` / `x.__class__.refund = v` replaced a door with no finding.
M1. A positional-only first parameter (`def m(self, /)`) was not the method's self.
M2. A nested lambda's or def's own `self` was the method's; a class-body `r = Repo()` and a name the method
    binds itself (`Safe = Repo`) did not count for self.r's type; a lambda's defaults and a comprehension's
    first iterable were read with the lambda's or comprehension's own names bound.
"""
from __future__ import annotations

import pathlib
import textwrap

import pytest

import invariants_scan as scan

SINK = "def send_to_gateway(c, a):\n    pass\n"
RULE = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway", "door": ["py:payments.refund"]}}]}
REPO = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.Repo.save", "door": ["py:payments.refund"]}}]}
CLASSES = ("class Repo:\n    def save(self):\n        pass\nclass Safe:\n    def save(self):\n        pass\n"
           "def refund():\n    Repo().save()\n")


def run(tmp_path: pathlib.Path, files: dict[str, str], rule: dict = RULE) -> tuple[str, set[str]]:
    rep = report(tmp_path, files, rule)
    return rep.rule_result("R"), {f.code for f in rep.findings}


def report(tmp_path: pathlib.Path, files: dict[str, str], rule: dict = RULE) -> scan.Report:
    for k, v in files.items():
        p = tmp_path / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(v))
    return scan.scan(rule, [tmp_path])


# ---- H1: a nested callable that leaves the door is a caller of its own ----

@pytest.mark.parametrize("door,use", [
    ("def refund():\n    def inner():\n        send_to_gateway(1, 2)\n    return inner\n", "refund()()"),
    ("def refund():\n    return lambda: send_to_gateway(1, 2)\n", "refund()()"),
    ("CB = []\ndef refund():\n    def inner():\n        send_to_gateway(1, 2)\n    CB.append(inner)\n", "refund()"),
    ("def refund():\n    class K:\n        def m(self):\n            send_to_gateway(1, 2)\n    return K\n", "refund()().m()"),
    ("import threading\ndef refund():\n    def inner():\n        send_to_gateway(1, 2)\n"
     "    threading.Thread(target=inner).start()\n", "refund()"),
    ("import atexit\ndef refund():\n    @atexit.register\n    def inner():\n        send_to_gateway(1, 2)\n", "refund()"),
    ("def refund():\n    def a():\n        def b():\n            send_to_gateway(1, 2)\n        return b\n    return a()\n",
     "refund()()"),
    ("def refund():\n    return [lambda: send_to_gateway(1, 2) for _ in 'ab']\n", "refund()[0]()"),
    ("def refund():\n    def inner():\n        send_to_gateway(1, 2)\n    inner()\n    return inner\n", "refund()()"),
])
def test_a_nested_callable_leaving_the_door_is_not_excused(tmp_path, door, use):
    files = {"payments.py": SINK + door, "ops.py": f"from payments import refund\ndef go():\n    {use}\n"}
    assert run(tmp_path, files) == ("fail", {"bypass"})


def test_the_finding_names_the_nested_callable(tmp_path):
    rep = report(tmp_path, {"payments.py": SINK + "def refund():\n    def inner():\n        send_to_gateway(1, 2)\n"
                                                  "    return inner\n"})
    (f,) = rep.findings
    assert f.code == "bypass" and f.path == ["payments.refund.<inner>", "payments.send_to_gateway"]
    assert (f.file, f.line) == ("payments.py", 5)


def test_a_nested_class_is_one_caller_with_its_methods(tmp_path):
    # a method, and a lambda bound in the class body, are the nested class's own code
    rep = report(tmp_path, {"payments.py": SINK + "def refund():\n    class K:\n        def m(self):\n"
                                                  "            send_to_gateway(1, 2)\n"
                                                  "        f = lambda self: send_to_gateway(3, 4)\n    return K\n"})
    assert [(f.code, f.path) for f in rep.findings] == [
        ("bypass", ["payments.refund.<K>", "payments.send_to_gateway"])] * 2


def test_a_nested_callable_in_a_method_door_and_in_an_allowed_function(tmp_path):
    door = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway",
                                                "door": ["py:payments.Svc.refund"]}}]}
    pay = SINK + "class Svc:\n    def refund(self):\n        def inner():\n            send_to_gateway(1, 2)\n" \
                 "        return inner\n"
    assert run(tmp_path / "a", {"payments.py": pay}, door) == ("fail", {"bypass"})
    allow = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway", "door": ["py:payments.refund"],
                                                 "allow": ["py:payments.legacy"]}}]}
    pay = SINK + "def refund():\n    send_to_gateway(1, 2)\ndef legacy():\n    send_to_gateway(1, 2)\n" \
                 "    return lambda: send_to_gateway(3, 4)\n"
    assert run(tmp_path / "b", {"payments.py": pay, "ops.py": "from payments import legacy\ndef go():\n    legacy()\n"},
               allow) == ("fail", {"bypass"})


@pytest.mark.parametrize("door", [
    "def refund():\n    def inner():\n        send_to_gateway(1, 2)\n    inner()\n",
    "def refund():\n    (lambda: send_to_gateway(1, 2))()\n",
    "def refund():\n    def a():\n        def b():\n            send_to_gateway(1, 2)\n        b()\n    a()\n",
    "import functools\ndef refund():\n    @functools.lru_cache\n    def inner():\n        send_to_gateway(1, 2)\n"
    "    inner()\n",
])
def test_a_nested_helper_only_called_in_the_door_stays_quiet(tmp_path, door):
    files = {"payments.py": SINK + door, "ops.py": "from payments import refund\ndef go():\n    refund()\n"}
    assert run(tmp_path, files) == ("pass", set())


def test_a_nested_wrapper_can_be_named_as_a_door(tmp_path):
    pay = SINK + ("import functools\ndef refund(f):\n    @functools.wraps(f)\n    def w(*a):\n        send_to_gateway(1, 2)\n"
                  "        return f(*a)\n    return w\n")
    files = {"payments.py": pay, "ops.py": "from payments import refund\n@refund\ndef go():\n    pass\n"}
    assert run(tmp_path / "a", files)[0] == "fail"
    rule = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway",
                                                "door": ["py:payments.refund", "py:payments.refund.<w>"]}}]}
    assert run(tmp_path / "b", files, rule) == ("pass", set())


# ---- H2: a method replaced by a scanned function; a door replaced through type(x) ----

HELP = SINK + "def _send(*a):\n    send_to_gateway(1, 2)\nclass Helper:\n    def run(self):\n        pass\n"


@pytest.mark.parametrize("install", [
    "def refund():\n    _send()\n    Helper.run = _send\n",
    "def refund():\n    _send()\n    setattr(Helper, 'run', _send)\n",
    "def refund():\n    _send()\n    Helper().arm()\n",
])
def test_a_call_of_a_replaced_method_may_run_the_replacement(tmp_path, install):
    # (installed inside the door, the value _send is not itself reported: it is made where the door has passed)
    pay = HELP + ("    def arm(self):\n        self.run = _send\n" if "arm" in install else "") + install
    files = {"payments.py": pay, "ops.py": "from payments import Helper\ndef go():\n    Helper().run()\n"}
    assert run(tmp_path, files) == ("unresolved", {"unresolved/inferred-path"})


@pytest.mark.parametrize("write", [
    "type(Svc()).refund = noop", "Svc().__class__.refund = len", "x = Svc()\n    type(x).refund = lambda s: None",
])
def test_a_door_replaced_through_its_instance_class_is_a_monkeypatch(tmp_path, write):
    rule = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway",
                                                "door": ["py:payments.Svc.refund"]}}]}
    pay = SINK + "class Svc:\n    def refund(self):\n        send_to_gateway(1, 2)\n"
    ops = f"from payments import Svc\ndef noop(*a):\n    pass\ndef setup():\n    {write}\n"
    assert run(tmp_path, {"payments.py": pay, "ops.py": ops}, rule) == ("fail", {"forbidden/monkeypatch"})


@pytest.mark.parametrize("write", ["type(self).refund = noop", "self.__class__.refund = len"])
def test_a_door_replaced_through_type_of_self_is_a_monkeypatch(tmp_path, write):
    rule = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway",
                                                "door": ["py:payments.Svc.refund"]}}]}
    pay = SINK + f"def noop(s):\n    pass\nclass Svc:\n    def refund(self):\n        send_to_gateway(1, 2)\n" \
                 f"    def setup(self):\n        {write}\n"
    assert run(tmp_path, {"payments.py": pay}, rule) == ("fail", {"forbidden/monkeypatch"})


def test_a_method_replaced_through_type_of_self_is_not_typed(tmp_path):
    pay = CLASSES + "class H:\n    def save(self):\n        pass\n    def arm(self):\n        type(self).save = len\n" \
                    "def go():\n    H().save()\n"
    assert run(tmp_path, {"payments.py": pay}, REPO) == ("unresolved", {"unresolved/inferred-path"})


# ---- M1: a positional-only self ----

def test_a_positional_only_self_is_self(tmp_path):
    pay = CLASSES + "class Morph(Safe):\n    def morph(self, /):\n        self.__class__ = Repo\n" \
                    "def go():\n    m = Morph()\n    m.morph()\n    m.save()\n"
    assert run(tmp_path / "a", {"payments.py": pay}, REPO) == ("unresolved", {"unresolved/inferred-path"})
    pay = CLASSES + "class K(Repo):\n    @classmethod\n    def make(cls, /):\n        cls().save()\n"
    assert run(tmp_path / "b", {"payments.py": pay}, REPO) == ("fail", {"bypass"})


# ---- M2: scoping ----

@pytest.mark.parametrize("body", [
    "class H(Safe):\n    def go(self):\n        f = lambda self: self.save()\n        f(Repo())\n",
    "class H(Safe):\n    def go(self):\n        def f(self):\n            self.save()\n        f(Repo())\n",
    "class H:\n    r = Repo()\n    def setup(self):\n        self.r = Safe()\n    def go(self):\n        self.r.save()\n",
    "class H:\n    def __init__(self):\n        Safe = Repo\n        self.r = Safe()\n    def go(self):\n        self.r.save()\n",
    "class H:\n    def __init__(self, Safe=Repo):\n        self.r = Safe()\n    def go(self):\n        self.r.save()\n",
])
def test_names_are_looked_up_in_their_own_scope(tmp_path, body):
    assert run(tmp_path, {"payments.py": CLASSES + body}, REPO) == ("unresolved", {"unresolved/inferred-path"})


def test_constant_class_defaults_and_local_imports_keep_the_type(tmp_path):
    ops = "from payments import Repo\nclass H:\n    r = None\n    def __init__(self):\n        self.r = Repo()\n" \
          "    def go(self):\n        self.r.save()\n"
    assert run(tmp_path / "a", {"payments.py": CLASSES, "ops.py": ops}, REPO) == ("fail", {"bypass"})
    ops = "class H:\n    def __init__(self):\n        from payments import Repo\n        self.r = Repo()\n" \
          "    def go(self):\n        self.r.save()\n"
    assert run(tmp_path / "b", {"payments.py": CLASSES, "ops.py": ops}, REPO) == ("fail", {"bypass"})


SEND = SINK + "def _send():\n    send_to_gateway(1, 2)\ndef refund():\n    _send()\n"


@pytest.mark.parametrize("ops,want", [
    ("def go():\n    return lambda _send=_send: _send()\n", ("unresolved", {"unresolved/sink-escapes"})),
    ("def go():\n    return lambda _send=_send(): 1\n", ("fail", {"bypass"})),
    ("class K:\n    hs = [_send for _send in [_send]]\n", ("unresolved", {"unresolved/sink-escapes"})),
    ("class K:\n    hs = [1 for _send in _send()]\n", ("fail", {"bypass"})),
    ("def go():\n    return [1 for _send in _send()]\n", ("fail", {"bypass"})),
    ("def go():\n    return [1 for _send in [_send]]\n", ("unresolved", {"unresolved/sink-escapes"})),
    ("def go():\n    return [1 for _send in [1] for x in _send()]\n", ("pass", set())),
])
def test_defaults_and_first_iterables_are_read_in_the_enclosing_scope(tmp_path, ops, want):
    assert run(tmp_path, {"payments.py": SEND, "ops.py": "from payments import _send\n" + ops}) == want
