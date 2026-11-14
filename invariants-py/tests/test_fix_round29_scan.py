"""Fix round 29: false passes of the static scan (an independent review). In each, payments.Repo.pay reaches
the sink and only the door calls it.

1. `.m` taken as a value on a receiver the scan cannot type (`Thread(target=r.pay)`, `ex.submit(e.pay)`,
   `f = e.pay`, `run(e.pay)`) is a name-matched value of every scanned function named m: one that reaches
   the sink without a door is unresolved/sink-escapes.
2. A computed setattr may set `__class__`: on a method's self (`setattr(self, k, v)`,
   `object.__setattr__(self, k, v)`) it distrusts the class as `self.__class__ = ...` does, on `obj.attr`
   that attribute, on a module-level singleton that name. A dataclass field's name, and a `__setattr__`
   passing its own name on, are not `__class__`.
3. `self.x` bound by a for / async for / with-as / comprehension target is untyped.
4. A module-level singleton rebound by a match capture or `except E as x` is untyped.
5. A module the importing file's own directory holds that is also a top-level module or a standard-library
   module (not a builtin or frozen one) is unresolved/unknown-module: shadowed.
6. 7. A method that rebinds its first parameter (`self = Repo()`, `for self in`, `except E as self`,
   `self += x`, `cls = Repo`) does not type it as the class.
"""
from __future__ import annotations

import textwrap

import pytest
from cli_support import write_project

from invariants import scan

PAY = ("def send_to_gateway(c, a):\n    pass\nclass Repo(Exception):\n    def pay(self):\n"
       "        send_to_gateway(1, 2)\n    def __iadd__(self, o):\n        return o\n"
       "def refund(c, a):\n    Repo().pay()\n")
RULE = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway", "door": ["py:payments.refund"]}}]}


def _scan(tmp_path, files, pay=PAY):
    files = {"payments.py": pay, **{k: textwrap.dedent(v) for k, v in files.items()}}
    return scan.scan(RULE, [write_project(tmp_path, files)])


def _codes(rep):
    return sorted({f.code for f in rep.findings})


# --- 1. a method taken as a value on an untyped receiver ------------------------------------------------------
@pytest.mark.parametrize("src", [
    "import threading\ndef go(r):\n    threading.Thread(target=r.pay).start()\n",
    "def go(e, ex):\n    ex.submit(e.pay)\n",
    "def go(e):\n    f = e.pay\n    f()\n",
    "def go(e):\n    run(e.pay)\ndef run(f):\n    f()\n",
    "def pay(): pass\ndef go(e):\n    pay = e.pay\n    pay()\n",
    "import payments\ndef go():\n    try:\n        raise payments.Repo()\n    except Exception as e:\n"
    "        f = e.pay\n        f()\n",
])
def test_a_method_value_on_an_untyped_receiver_may_be_any_of_that_name(tmp_path, src):
    rep = _scan(tmp_path, {"ops.py": src})
    assert "unresolved/sink-escapes" in _codes(rep), rep.findings
    f = next(f for f in rep.findings if f.code == "unresolved/sink-escapes")
    assert f.file == "ops.py" and "payments.Repo.pay" in f.message, f


def test_a_method_value_whose_namesakes_cannot_reach_the_sink_is_quiet(tmp_path):
    rep = _scan(tmp_path, {"ops.py": "import threading\ndef go(r):\n    threading.Thread(target=r.close).start()\n"
                                     "class W:\n    def close(self): pass\n"})
    assert rep.findings == [], rep.findings


def test_a_method_value_inside_the_door_is_quiet(tmp_path):
    rep = _scan(tmp_path, {}, pay=PAY.replace("    Repo().pay()\n", "    Repo().pay()\n    cb = c.pay\n"))
    assert rep.findings == [], rep.findings


# --- 2. a computed setattr may set __class__ ------------------------------------------------------------------
SAFE = "import payments\nclass Safe:\n    def pay(self):\n        pass\n"


@pytest.mark.parametrize("src", [
    SAFE + "    def load(self, d):\n        for k, v in d.items():\n            setattr(self, k, v)\n        self.pay()\n"
    "def main():\n    Safe().load({'__class__': payments.Repo})\n",
    SAFE + "    def load(self, d):\n        for k, v in d.items():\n            object.__setattr__(self, k, v)\n"
    "        self.pay()\ndef main():\n    Safe().load({'__class__': payments.Repo})\n",
    SAFE + "class H:\n    def __init__(self):\n        self.r = Safe()\n    def load(self, d):\n"
    "        for k, v in d.items():\n            setattr(self.r, k, v)\n        self.r.pay()\n",
    SAFE + "settings = Safe()\ndef use():\n    settings.pay()\ndef f():\n    settings.__class__ = payments.Repo\n",
])
def test_a_computed_setattr_retypes_its_receiver(tmp_path, src):
    rep = _scan(tmp_path, {"ops.py": src})
    assert "unresolved/inferred-path" in _codes(rep), rep.findings


def test_a_computed_setattr_on_a_local_is_reported_for_its_aliases(tmp_path):
    src = SAFE + "def go(k, v):\n    s = Safe()\n    t = s\n    setattr(s, k, v)\n    t.pay()\n"
    rep = _scan(tmp_path, {"ops.py": src})
    assert "unresolved/computed-attribute" in _codes(rep), rep.findings


def test_a_computed_setattr_on_a_returned_object_is_reported(tmp_path):
    # get() hands back what self.r holds: retyping it retypes self.r, which the scan types as Safe
    src = SAFE + ("class H:\n    def __init__(self):\n        self.r = Safe()\n    def get(self) -> Safe:\n"
                  "        return self.r\n    def load(self, d):\n        for k, v in d.items():\n"
                  "            setattr(self.get(), k, v)\n        self.r.pay()\n")
    rep = _scan(tmp_path, {"ops.py": src})
    assert "unresolved/computed-attribute" in _codes(rep), rep.findings


def test_a_dataclass_field_setattr_is_still_narrowed(tmp_path):
    src = ("import dataclasses\n" + chr(64) + "dataclasses.dataclass\nclass Rec:\n    a: int = 0\n    def update(self, o: 'Rec'):\n"
           "        for f in dataclasses.fields(self):\n            setattr(self, f.name, getattr(o, f.name))\n"
           "        self.pay()\n    def pay(self):\n        pass\n")
    assert _scan(tmp_path / "a", {"rec.py": src}).findings == []
    # a scanned class with a dunder field: a field name may then be __class__
    rep = _scan(tmp_path / "b", {"rec.py": src + "class D:\n    __class__: type = int\n"})
    assert "unresolved/computed-attribute" in _codes(rep), rep.findings


def test_a_setattr_forwarding_its_own_name_does_not_distrust_the_class(tmp_path):
    src = SAFE + ("    def __setattr__(self, k, v):\n        object.__setattr__(self, k, v)\n"
                  "def main():\n    Safe().pay()\n")
    assert _scan(tmp_path, {"ops.py": src}).findings == []


SETSTATE = SAFE + ("    def __setstate__(self, state):\n        for k, v in state.items():\n            setattr(self, k, v)\n"
                   "def main():\n    Safe().pay()\n")


def test_a_setstate_restoring_its_state_does_not_distrust_the_class(tmp_path):
    assert _scan(tmp_path / "a", {"ops.py": SETSTATE}).findings == []
    # unless scanned code calls __setstate__ itself, with a state that may hold __class__
    rep = _scan(tmp_path / "b", {"ops.py": SETSTATE + "def f(d):\n    Safe().__setstate__(d)\n"})
    assert "unresolved/inferred-path" in _codes(rep), rep.findings


# --- 3. self.x bound by a loop, with or comprehension target ---------------------------------------------------
@pytest.mark.parametrize("bind", [
    "for self.r in [payments.Repo()]:\n            pass",
    "with contextlib.nullcontext(payments.Repo()) as self.r:\n            pass",
    "[0 for self.r in [payments.Repo()]]",
])
def test_an_attribute_bound_by_a_target_is_untyped(tmp_path, bind):
    src = (f"import payments, contextlib\nclass Safe:\n    def pay(self):\n        pass\nclass H:\n"
           f"    def __init__(self):\n        self.r = Safe()\n    def load(self):\n        {bind}\n"
           f"    def use(self):\n        self.r.pay()\n")
    rep = _scan(tmp_path, {"ops.py": src})
    assert "unresolved/inferred-path" in _codes(rep), rep.findings


def test_an_attribute_bound_by_async_for_is_untyped(tmp_path):
    src = ("import payments\nclass Safe:\n    def pay(self):\n        pass\nclass H:\n    def __init__(self):\n"
           "        self.r = Safe()\n    async def load(self, it):\n        async for self.r in it:\n            pass\n"
           "    def use(self):\n        self.r.pay()\n")
    assert "unresolved/inferred-path" in _codes(_scan(tmp_path, {"ops.py": src}))


# --- 4. a module-level singleton rebound by a match capture or except-as --------------------------------------
@pytest.mark.parametrize("rebind", [
    "match payments.Repo():\n    case settings: pass\n",
    "match payments.Repo():\n    case payments.Repo() as settings: pass\n",
    "match [payments.Repo()]:\n    case [settings]: pass\n",
    "try:\n    raise payments.Repo()\nexcept Exception as settings:\n    pass\n",
])
def test_a_singleton_rebound_by_a_capture_is_untyped(tmp_path, rebind):
    src = SAFE + "settings = Safe()\ndef use():\n    settings.pay()\n" + rebind
    rep = _scan(tmp_path, {"ops.py": src, "o2.py": "import ops\ndef g():\n    ops.settings.pay()\n"})
    assert "unresolved/inferred-path" in _codes(rep), rep.findings


# --- 5. a module of the importing file's own directory shadowing another ------------------------------------
STORE = "import payments\ndef helper():\n    payments.send_to_gateway(1, 2)\n"


def test_a_top_level_module_shadowed_by_the_own_directory_is_unknown(tmp_path):
    rep = _scan(tmp_path, {"cli/__init__.py": "", "cli/fleet/__init__.py": "", "cli/fleet/store.py": STORE,
                           "fleet/__init__.py": "", "fleet/store.py": "def helper():\n    pass\n",
                           "cli/main.py": "import fleet.store\ndef run():\n    fleet.store.helper()\n"})
    assert [(f.code, f.file) for f in rep.findings if f.code == "unresolved/unknown-module"] == \
        [("unresolved/unknown-module", "cli/main.py")], rep.findings


def test_a_stdlib_module_shadowed_by_the_own_directory_is_unknown(tmp_path):
    rep = _scan(tmp_path, {"cli/__init__.py": "", "cli/json.py": STORE.replace("helper", "dumps"),
                           "cli/main.py": "import json\ndef run():\n    json.dumps({})\n"})
    assert "unresolved/unknown-module" in _codes(rep), rep.findings


def test_a_harmless_stdlib_shadow_is_quiet(tmp_path):
    # a package's own types.py beside `import types`: only reported when what it holds bears on the rule
    rep = _scan(tmp_path, {"cli/__init__.py": "", "cli/types.py": "def x():\n    pass\n",
                           "cli/main.py": "import types\ndef run():\n    types.x()\n"})
    assert rep.findings == [], rep.findings


@pytest.mark.parametrize("name", ["sys", "abc"])          # builtin, frozen: found before sys.path
def test_a_builtin_or_frozen_module_is_never_shadowed(tmp_path, name):
    rep = _scan(tmp_path, {"cli/__init__.py": "", f"cli/{name}.py": STORE, "cli/main.py": f"import {name}\n"})
    assert "unresolved/unknown-module" not in _codes(rep), rep.findings


# --- 6, 7. a method rebinding its first parameter ----------------------------------------------------------------
@pytest.mark.parametrize("body", [
    "self = payments.Repo()\n        self.pay()",
    "for self in [payments.Repo()]:\n            self.pay()",
    "if (self := payments.Repo()):\n            self.pay()",
    "with contextlib.nullcontext(payments.Repo()) as self:\n            self.pay()",
    "match x:\n            case self:\n                self.pay()",
    "self, y = payments.Repo(), 1\n        self.pay()",
    "self = x\n        self.pay()",
    "try:\n            raise payments.Repo()\n        except Exception as self:\n            self.pay()",
    "self += payments.Repo()\n        self.pay()",
])
def test_a_rebound_self_is_not_the_class(tmp_path, body):
    src = f"import payments, contextlib\nclass Safe:\n    def pay(self):\n        pass\n    def go(self, x):\n        {body}\n"
    rep = _scan(tmp_path, {"ops.py": src})
    assert rep.findings, body


def test_a_rebound_cls_is_not_the_class(tmp_path):
    src = ("import payments\nclass Safe:\n    @classmethod\n    def pay(cls):\n        pass\n    @classmethod\n"
           "    def go(cls):\n        cls = payments.Repo\n        cls().pay()\n")
    assert _scan(tmp_path, {"ops.py": src}).findings


def test_a_self_left_alone_stays_typed(tmp_path):
    src = "import payments\nclass Safe:\n    def pay(self):\n        pass\n    def go(self):\n        self.pay()\n"
    assert _scan(tmp_path, {"ops.py": src}).findings == []
