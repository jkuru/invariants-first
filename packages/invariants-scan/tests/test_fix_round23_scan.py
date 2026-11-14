"""Fix round 23 (scan: reflection and typing): each test failed before its fix.

H5. Typing the code subverts: when a class's construction or identity can be altered (a custom `__new__`,
    a metaclass hook, a class decorator that may return another class, a class name bound again,
    `__class__` / `__bases__` / method writes on it, reflective writes of a typed attribute), its
    inferred type is not trusted: calls on it are name-matched (unresolved), never a silent pass. A
    function annotated `-> Safe` that visibly returns another scanned class gives no type. A data
    attribute of a scanned module or class (`other.r.save()`, `Holder.r.save()`) is a value whose type
    the scan does not know, so its calls are name-matched too.
H6. Names resolved from strings: pickle, runpy, logging.config, pydoc.locate, pkgutil.resolve_name,
    inspect.getattr_static, `C.__mro__[i].__dict__[k]`, `f.__globals__[k]`, `builtins.__dict__[k]`.
H7. Unwrapping a guard: inspect.unwrap(door), door.__dict__["__wrapped__"] (or .get), door.__closure__.
S.  One finding per call site: a name-matched call is one finding, not two;
    `getattr(store.sink, "__call__")(c)` is a call of the sink, not also the sink as a value.
"""
from __future__ import annotations

import textwrap

import pytest

import invariants_scan as scan
from scan_support import write_project

PAYMENTS = """
    import functools

    def guard(f):
        @functools.wraps(f)
        def w(*a, **k):
            return f(*a, **k)
        return w

    def send_to_gateway(charge_id, amount):
        pass

    @guard
    def refund(charge_id, amount):
        send_to_gateway(charge_id, amount)
    """
STORE = """
    class Repo:
        def save(self, amount):
            pass

    class Service:
        def __init__(self):
            self.repo = Repo()

        def write(self, amount):
            self.repo.save(amount)
    """
DOC = {"rules": [
    {"id": "PAY-1", "status": "candidate", "subject": {"target": "py:payments.refund"}, "bad": "false",
     "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
     "mediation": {"sink": "py:payments.send_to_gateway", "door": "py:payments.refund"}},
    {"id": "ST-1", "status": "candidate", "subject": {"target": "py:store.Service.write"}, "bad": "false",
     "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
     "mediation": {"sink": "py:store.Repo.save", "door": "py:store.Service.write"}}]}


def _scan(tmp_path, ops, extra=None):
    files = {"payments.py": textwrap.dedent(PAYMENTS), "store.py": textwrap.dedent(STORE),
             "ops.py": textwrap.dedent(ops), **{k: textwrap.dedent(v) for k, v in (extra or {}).items()}}
    rep = scan.scan(DOC, [write_project(tmp_path, files)])
    assert not [f for f in rep.findings if f.code == "unresolved/parse-error"], rep.findings
    return rep


def _codes(rep, rule):
    return [f.code for f in rep.for_rule(rule)]


SAFE = textwrap.dedent("""
    from store import Repo
    class Safe:
        def save(self, amount):
            pass
    """)


def _safe(more):
    return SAFE + textwrap.dedent(more)

# --- H5: typing the code subverts --------------------------------------------------------------------------
SUBVERTED = {
    "new": """
        from store import Repo
        class Safe:
            def __new__(cls):
                return Repo()
            def save(self, amount):
                pass
        def g():
            Safe().save(5)
        """,
    "metaclass-call": """
        from store import Repo
        class M(type):
            def __call__(cls, *a, **k):
                return Repo()
        class Safe(metaclass=M):
            def save(self, amount):
                pass
        def g():
            Safe().save(5)
        """,
    "class-decorator": """
        from store import Repo
        def swap(cls):
            return Repo
        @swap
        class Safe:
            def save(self, amount):
                pass
        def g():
            Safe().save(5)
        """,
    "rebind-class": _safe("""
    Safe = Repo
    def g():
        Safe().save(5)
    """),
    "rebind-imported": """
        from store import Repo
        from other import Safe
        Safe = Repo
        def g():
            Safe().save(5)
        """,
    "global-rebind": _safe("""
    def setup():
        global Safe
        Safe = Repo
    def g():
        Safe().save(5)
    """),
    "class-assign": _safe("""
    def g():
        r = Safe()
        r.__class__ = Repo
        r.save(5)
    """),
    "self-class-assign": _safe("""
        def swap(self):
            self.__class__ = Repo
    def g():
        Safe().save(5)
    """),
    "object-setattr": _safe("""
    class Holder:
        def __init__(self):
            self.r = Safe()
            object.__setattr__(self, "r", Repo())
        def go(self):
            self.r.save(5)
    """),
    "self-dict": _safe("""
    class Holder:
        def __init__(self):
            self.r = Safe()
            self.__dict__["r"] = Repo()
        def go(self):
            self.r.save(5)
    """),
    "class-attr-from-module": _safe("""
    class Holder:
        r: Safe
    Holder.r = Repo()
    def g():
        Holder.r.save(5)
    """),
    "bases-swap": _safe("""
    class Leaf(Safe):
        pass
    Leaf.__bases__ = (Repo,)
    def g():
        Leaf().save(5)
    """),
    "del-method": """
        from store import Repo
        class Safe(Repo):
            def save(self, amount):
                pass
        del Safe.save
        def g():
            Safe().save(5)
        """,
    "method-over-class": _safe("""
    Safe.save = Repo.save
    def g():
        Safe.save(Repo(), 5)
    """),
    "return-lie": _safe("""
    def get() -> Safe:
        return Repo()
    def g():
        get().save(5)
    """),
    "module-data-attr": """
        import other
        def g():
            other.r.save(5)
        """,
}
OTHER = {"other.py": "from store import Repo\nclass Safe:\n    def save(self, amount):\n        pass\nr = Repo()\n"}


@pytest.mark.parametrize("ops", list(SUBVERTED.values()), ids=list(SUBVERTED))
def test_a_class_the_code_subverts_is_not_trusted(tmp_path, ops):
    rep = _scan(tmp_path, ops, OTHER)
    assert rep.rule_result("ST-1") != "pass", rep.findings
    assert any(f.file == "ops.py" for f in rep.for_rule("ST-1")), rep.findings


def test_a_parameter_annotation_is_still_trusted(tmp_path):
    # documented: the scan trusts a parameter annotation (it cannot see every caller's argument)
    rep = _scan(tmp_path, SAFE + "def h(r: Safe):\n    r.save(5)\n")
    assert rep.rule_result("ST-1") == "pass", rep.findings


TRUSTED = {
    "plain-new": """
        class Safe:
            def __new__(cls, *a):
                return super().__new__(cls)
            def save(self, amount):
                pass
        """,
    "object-new": """
        class Safe:
            def __new__(cls):
                return object.__new__(cls)
            def save(self, amount):
                pass
        """,
    "dataclass": """
        import dataclasses
        @dataclasses.dataclass(frozen=True)
        class Safe:
            def save(self, amount):
                pass
        """,
    "registering-decorator": """
        REG = {}
        def register(cls):
            REG[cls.__name__] = cls
            return cls
        @register
        class Safe:
            def save(self, amount):
                pass
        """,
    "decorator-factory": """
        REG = {}
        def register(name):
            def deco(cls):
                REG[name] = cls
                return cls
            return deco
        @register("safe")
        class Safe:
            def save(self, amount):
                pass
        """,
    "abc-metaclass": """
        import abc
        class Safe(metaclass=abc.ABCMeta):
            def save(self, amount):
                pass
        """,
    "data-attr-write": """
        class Safe:
            count = 0
            def save(self, amount):
                Safe.count += 1
        """,
}


@pytest.mark.parametrize("cls", list(TRUSTED.values()), ids=list(TRUSTED))
def test_a_class_built_the_ordinary_way_stays_typed(tmp_path, cls):
    rep = _scan(tmp_path, textwrap.dedent(cls) + "\ndef g():\n    Safe().save(5)\n")
    assert rep.rule_result("ST-1") == "pass", rep.findings


def test_replacing_one_method_distrusts_that_method_only(tmp_path):
    # `Repo.save = len` replaces save: Repo().save is name-matched; Repo().other stays typed
    store = STORE.replace("class Service:", "    def other(self):\n            pass\n\n    class Service:")
    rep = _scan(tmp_path, "from store import Repo\nRepo.save = len\ndef g():\n    Repo().other()\n"
                          "def h():\n    Repo().save(5)\n", {"store.py": store})
    found = [f for f in rep.for_rule("ST-1") if f.file == "ops.py"]
    assert [(f.code, f.owner) for f in found if f.code != "forbidden/monkeypatch"] == \
        [("unresolved/inferred-path", "ops.h")], found


def test_a_typed_call_through_a_subverted_class_is_name_matched_not_a_bypass(tmp_path):
    # the sink class itself rebound elsewhere: its own calls stay found (name-matched)
    rep = _scan(tmp_path, "import store\nstore.Repo = object\ndef g():\n    store.Repo().save(5)\n")
    assert rep.rule_result("ST-1") == "fail", rep.findings       # the rebinding is a monkeypatch


# --- H6: names resolved from strings ---------------------------------------------------------------------
@pytest.mark.parametrize("ops,code", [
    ("import pickle\ndef g():\n    pickle.loads(b'')\n", "unresolved/reflection"),
    ("import pickle\ndef g(f):\n    pickle.load(f)\n", "unresolved/reflection"),
    ("import pydoc\ndef g():\n    pydoc.locate('payments.send_to_gateway')('c', 5)\n", "forbidden/getattr-sink"),
    ("import pydoc\ndef g(n):\n    pydoc.locate(n)('c', 5)\n", "unresolved/reflection"),
    ("import pkgutil\ndef g():\n    pkgutil.resolve_name('payments:send_to_gateway')('c', 5)\n",
     "forbidden/getattr-sink"),
    (("import logging.config\ndef g():\n    logging.config.dictConfig({'version': 1, 'handlers': {'h': "
      "{'()': 'payments.send_to_gateway', 'charge_id': 'c', 'amount': 5}}})\n"), "bypass"),
    ("import logging.config\ndef g(cfg):\n    logging.config.dictConfig(cfg)\n", "unresolved/reflection"),
    ("import logging.config\ndef g():\n    logging.config.fileConfig('log.ini')\n", "unresolved/reflection"),
    ("import runpy\ndef g():\n    runpy.run_path('x.py')\n", "unresolved/reflection"),
    ("import runpy\ndef g():\n    runpy.run_module('x')\n", "unresolved/reflection"),
    ("import inspect, payments\ndef g():\n    inspect.getattr_static(payments, 'send_to_gateway')('c', 5)\n",
     "forbidden/getattr-sink"),
    (("import copy\nclass X:\n    def __reduce__(self):\n        import pickle\n"
      "        return (pickle.loads, (b'',))\ndef g():\n    copy.deepcopy(X())\n"), "unresolved/reflection"),
    ("import builtins, payments\ndef g():\n    builtins.__dict__['get' + 'attr'](payments, 'send_to_gateway')('c', 5)\n",
     "unresolved/reflection"),
    ("import builtins, payments\ndef g():\n    builtins.__dict__['getattr'](payments, 'send_to_gateway')('c', 5)\n",
     "unresolved/reflection"),
    ("import builtins, payments\ndef g():\n    getattr(builtins, 'getattr')(payments, 'send_to_gateway')('c', 5)\n",
     "unresolved/reflection"),
    ("import payments\ndef g():\n    payments.refund.__globals__['send_to_gateway']('c', 5)\n",
     "forbidden/getattr-sink"),
    ("import payments\ndef g():\n    f = payments.refund\n    f.__globals__.get('send_to_gateway')('c', 5)\n",
     "unresolved/reflection"),
    ("def g():\n    __builtins__['getattr']\n", "unresolved/reflection"),
], ids=["pickle-loads", "pickle-load", "pydoc", "pydoc-computed", "pkgutil", "dictconfig-factory",
        "dictconfig-unknown", "fileconfig", "runpy-path", "runpy-module", "getattr-static", "reduce",
        "builtins-dict-computed", "builtins-dict-getattr", "getattr-of-getattr", "globals", "globals-unknown",
        "dunder-builtins"])
def test_a_name_resolved_from_a_string_is_reported(tmp_path, ops, code):
    rep = _scan(tmp_path, ops)
    assert code in _codes(rep, "PAY-1"), rep.findings


@pytest.mark.parametrize("ops", [
    "from store import Repo\ndef g():\n    Repo.__mro__[0].__dict__['save'](Repo(), 5)\n",
    "import inspect\nfrom store import Repo\ndef g():\n    inspect.getattr_static(Repo, 'save')(Repo(), 5)\n",
    "from store import Repo\ndef g(name):\n    for c in Repo.__mro__:\n        c.__dict__[name](Repo(), 5)\n",
], ids=["mro-dict", "getattr-static", "mro-loop"])
def test_a_class_namespace_reached_through_its_mro_is_a_lookup(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert rep.rule_result("ST-1") != "pass", rep.findings


@pytest.mark.parametrize("ops", [
    ("import logging.config\nLOG = {'version': 1, 'handlers': {'h': {'class': 'logging.StreamHandler'}}}\n"
     "def g():\n    logging.config.dictConfig(LOG)\n"),
    "def g(o):\n    return o.__dict__\n",
    "class A:\n    def __init__(self, **kw):\n        self.__dict__.update(kw)\n",
    "import json\ndef g(o):\n    return json.dumps(o, default=lambda x: x.__dict__)\n",
], ids=["dictconfig-literal", "instance-dict", "dict-update", "dict-as-default"])
def test_ordinary_uses_stay_quiet(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert rep.rule_result("PAY-1") == "pass" and rep.rule_result("ST-1") == "pass", rep.findings


# --- H7: unwrapping a guard ---------------------------------------------------------------------------------
@pytest.mark.parametrize("ops", [
    "import inspect, payments\ndef g():\n    inspect.unwrap(payments.refund)('c', -5)\n",
    "import payments\ndef g():\n    payments.refund.__dict__['__wrapped__']('c', -5)\n",
    "import payments\ndef g():\n    payments.refund.__dict__.get('__wrapped__')('c', -5)\n",
    "import payments\ndef g():\n    for c in payments.refund.__closure__ or ():\n        c.cell_contents('c', -5)\n",
    "import payments\ndef g():\n    for v in payments.refund.__dict__.values():\n        v('c', -5)\n",
    "def g(f):\n    return f.__dict__['__wrapped__']\n",
], ids=["inspect-unwrap", "fn-dict", "fn-dict-get", "closure", "fn-dict-values", "unknown-fn-dict"])
def test_unwrapping_a_guard_is_forbidden(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert "forbidden/unwrap" in _codes(rep, "PAY-1"), rep.findings


def test_a_functions_other_metadata_stays_harmless(tmp_path):
    rep = _scan(tmp_path, "import payments\ndef g():\n    return payments.refund.__dict__['tag']\n")
    assert rep.rule_result("PAY-1") == "pass", rep.findings


def test_replacing_a_doors_code_is_a_monkeypatch(tmp_path):
    rep = _scan(tmp_path, "import payments\ndef h(c, a):\n    pass\ndef g():\n"
                          "    payments.refund.__code__ = h.__code__\n")
    assert "forbidden/monkeypatch" in _codes(rep, "PAY-1"), rep.findings


# --- S: one finding per call site --------------------------------------------------------------------------
def test_a_name_matched_call_is_one_finding(tmp_path):
    rep = _scan(tmp_path, "def f(repo):\n    repo.save(5)\n")
    found = [f for f in rep.for_rule("ST-1") if f.file == "ops.py"]
    assert [f.code for f in found] == ["unresolved/inferred-path"], found
    assert found[0].path == ["ops.f", "store.Repo.save"] and "name-matched" in found[0].message
    assert rep.counts("ST-1") == {"unresolved": 1}


def test_getattr_call_dunder_on_the_sink_is_one_finding(tmp_path):
    rep = _scan(tmp_path, "import payments\ndef g():\n    getattr(payments.send_to_gateway, '__call__')('c', 5)\n")
    assert _codes(rep, "PAY-1") == ["bypass"], rep.findings


def test_getattr_call_dunder_taken_as_a_value_still_escapes(tmp_path):
    rep = _scan(tmp_path, "import payments\ndef g():\n    return getattr(payments.send_to_gateway, '__call__')\n")
    assert "unresolved/sink-escapes" in _codes(rep, "PAY-1"), rep.findings
