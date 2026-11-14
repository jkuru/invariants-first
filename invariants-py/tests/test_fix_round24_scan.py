"""Scan: functions used as values, class-identity trust, resolvers that run code from strings.

E. An escaped function is an entrypoint. A function that can reach the sink without passing a door (a door's
   own helper below the door counts: called on its own it skips the door) and is used as a value outside
   the doors, the sink and the allowed functions (a thread target, a dict, map(), functools.partial, an
   alias, a default argument, a return value, a class attribute, a bound method) is reported where it
   escapes; an allowed function used as a value outside allow is a new caller: a bypass. A name bound to
   several scanned functions (a conditional import, a star import re-exporting another object under the
   name) calls each of them.
C. Class identity: only known identity-preserving decorators are trusted; a scanned decorator or `__new__`
   that hands back its parameter must never rebind it (and `__new__` must pass its first parameter); a
   class with its own `__getattribute__` / `__getattr__` is not trusted; `self.__dict__.update({...})` and
   an alias of `self.__dict__` set attributes reflectively.
R. Resolvers that run code or look names up from strings and frames: string.Formatter, code.Interactive*,
   frame globals and locals, timeit with string statements, doctest, trace, gc, inspect.getmodule /
   getmembers, and subprocesses that run Python.
M. A return annotation over the result of an unannotated scanned function that visibly returns another
   class is not trusted.
"""
from __future__ import annotations

import copy
import textwrap

import pytest
from cli_support import write_project

from invariants import scan

PAYMENTS = """
    import functools

    def guard(f):
        @functools.wraps(f)
        def w(*a, **k):
            return f(*a, **k)
        return w

    def send_to_gateway(charge_id, amount):
        pass

    def _send(charge_id, amount):
        send_to_gateway(charge_id, amount)

    @guard
    def refund(charge_id, amount):
        _send(charge_id, amount)
    """
STORE = """
    class Repo:
        def save(self, amount):
            pass

    class Safe:
        def save(self, amount):
            pass

    class Service:
        def __init__(self):
            self.repo = Repo()

        def _persist(self, amount):
            self.repo.save(amount)

        def write(self, amount):
            self._persist(amount)
    """
DOC = {"rules": [
    {"id": "PAY-1", "status": "candidate", "subject": {"target": "py:payments.refund"}, "bad": "false",
     "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
     "mediation": {"sink": "py:payments.send_to_gateway", "door": "py:payments.refund"}},
    {"id": "ST-1", "status": "candidate", "subject": {"target": "py:store.Service.write"}, "bad": "false",
     "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
     "mediation": {"sink": "py:store.Repo.save", "door": "py:store.Service.write"}}]}
ADOC = copy.deepcopy(DOC)
ADOC["rules"][0]["mediation"]["allow"] = ["py:legacy.admin_goodwill"]
LEGACY = """
    from payments import send_to_gateway
    def admin_goodwill():
        send_to_gateway(1, 2)
    """


def _scan(tmp_path, ops, extra=None, doc=DOC):
    files = {"payments.py": PAYMENTS, "store.py": STORE, "ops.py": ops, **(extra or {})}
    rep = scan.scan(doc, [write_project(tmp_path, {k: textwrap.dedent(v) for k, v in files.items()})])
    assert not [f for f in rep.findings if f.code == "unresolved/parse-error"], rep.findings
    return rep


def _codes(rep, rule):
    return [f.code for f in rep.for_rule(rule)]


# --- E: an escaped function is an entrypoint ---------------------------------------------------------------
ESCAPES = {
    "thread": "import threading\nfrom payments import _send\nthreading.Thread(target=_send, args=(1, 2)).start()\n",
    "timer": "import threading\nfrom payments import _send\nthreading.Timer(0, _send, args=(1, 2)).start()\n",
    "atexit": "import atexit\nfrom payments import _send\natexit.register(_send, 1, 2)\n",
    "dict": "from payments import _send\nH = {'x': _send}\nH['x'](1, 2)\n",
    "map": "from payments import _send\nlist(map(_send, [1], [2]))\n",
    "partial": "import functools\nfrom payments import _send\nfunctools.partial(_send, 1)(2)\n",
    "alias": "from payments import _send as s\nx = s\nx(1, 2)\n",
    "class-attr": "from payments import _send\nclass Safe:\n    save = staticmethod(_send)\nSafe.save(1, 2)\n",
    "method-replace": "import payments\nclass Safe:\n    def go(self, a, b): pass\n"
                      "Safe.go = staticmethod(payments._send)\nSafe().go(1, 2)\n",
    "method-replace2": "from payments import _send\nclass Safe:\n    @staticmethod\n    def go(a, b): pass\n"
                       "Safe.go = _send\nSafe.go(1, 2)\n",
    "default-arg": "from payments import _send\ndef f(cb=_send):\n    cb(1, 2)\nf()\n",
    "registry": "REG = []\ndef reg(f):\n    REG.append(f); return f\nimport payments\nreg(payments._send)\nREG[0](1, 2)\n",
    "return": "from payments import _send\ndef get():\n    return _send\nget()(1, 2)\n",
    "local-rebind": "from payments import refund, _send\ndef go():\n    refund = _send\n    refund(1, 2)\ngo()\n",
    "walrus": "from payments import refund, _send\nif (refund := _send):\n    refund(1, 2)\n",
    "for": "from payments import refund, _send\nfor refund in [_send]:\n    pass\nrefund(1, 2)\n",
    "with": "import contextlib\nfrom payments import refund, _send\n"
            "with contextlib.nullcontext(_send) as refund:\n    refund(1, 2)\n",
    "match": "from payments import refund, _send\nmatch _send:\n    case refund:\n        refund(1, 2)\n",
    "comprehension": "from payments import refund, _send\n[refund(1, 2) for refund in [_send]]\n",
    "lambda-default": "from payments import refund, _send\n(lambda refund=_send: refund(1, 2))()\n",
    "nonlocal": "from payments import refund, _send\ndef outer():\n    refund = None\n    def inner():\n"
                "        nonlocal refund\n        refund = _send\n    inner()\n    refund(1, 2)\nouter()\n",
    "global": "from payments import refund, _send\ndef setit():\n    global refund\n    refund = _send\n"
              "setit()\nrefund(1, 2)\n",
    "class-body": "from payments import refund, _send\nclass K:\n    refund = _send\n    refund(1, 2)\n",
    "builtins": "import builtins, payments\nbuiltins.len = payments._send\nlen(1, 2)\n",
    "cond-import": "import os\nif os.environ.get('X', '1'):\n    from payments import _send as refund\nelse:\n"
                   "    from payments import refund\nrefund(1, 2)\n",
    "try-import": "try:\n    from payments import _send as refund\nexcept ImportError:\n"
                  "    from payments import refund\nrefund(1, 2)\n",
}


@pytest.mark.parametrize("name", sorted(ESCAPES))
def test_door_helper_used_as_a_value_is_reported(tmp_path, name):
    rep = _scan(tmp_path, ESCAPES[name])
    assert rep.rule_result("PAY-1") != "pass", (name, rep.for_rule("PAY-1"))


def test_escape_finding_is_at_the_escape(tmp_path):
    rep = _scan(tmp_path, ESCAPES["thread"])
    got = [f for f in rep.for_rule("PAY-1") if f.code == "unresolved/sink-escapes"]
    assert got and got[0].file == "ops.py" and got[0].line == 3, rep.for_rule("PAY-1")
    assert got[0].path == ["ops.<module>", "payments._send", "payments.send_to_gateway"]


def test_star_import_reexporting_a_helper_under_the_door_name(tmp_path):
    rep = _scan(tmp_path, "from payments import refund\nfrom shadow import *\nrefund(1, 2)\n",
                {"shadow.py": "from payments import _send as refund\n__all__ = ['refund']\n"})
    assert rep.rule_result("PAY-1") == "fail", rep.for_rule("PAY-1")


@pytest.mark.parametrize("ops", ["from store import Service\nf = Service()._persist\nf(1)\n",
                                 "from store import Service\nf = Service._persist\nf(Service(), 1)\n"])
def test_method_below_the_door_used_as_a_value(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert rep.rule_result("ST-1") != "pass", rep.for_rule("ST-1")


@pytest.mark.parametrize("ops", [
    "import threading\nfrom legacy import admin_goodwill\nthreading.Thread(target=admin_goodwill).start()\n",
    "from legacy import admin_goodwill\ng = admin_goodwill\ng()\n"])
def test_allowed_function_used_as_a_value_outside_allow_is_a_bypass(tmp_path, ops):
    rep = _scan(tmp_path, ops, {"legacy.py": LEGACY}, doc=ADOC)
    assert "bypass" in _codes(rep, "PAY-1"), rep.for_rule("PAY-1")


QUIET = {
    "door-as-value": "import threading\nfrom payments import refund\nthreading.Thread(target=refund).start()\n",
    "unrelated": "def helper(x):\n    return x\nimport threading\nthreading.Thread(target=helper).start()\n",
}


def test_values_that_cannot_skip_the_door_stay_quiet(tmp_path):
    rep = _scan(tmp_path / "a", QUIET["door-as-value"])
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")
    rep = _scan(tmp_path / "b", QUIET["unrelated"])
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")
    inside = PAYMENTS.replace("        _send(charge_id, amount)\n",
                              "        _send(charge_id, 0)\n        import threading\n"
                              "        threading.Thread(target=_send, args=(charge_id, amount)).start()\n")
    files = {"payments.py": inside, "store.py": STORE, "ops.py": "from payments import refund\nrefund(1, 2)\n"}
    rep = scan.scan(DOC, [write_project(tmp_path / "c", {k: textwrap.dedent(v) for k, v in files.items()})])
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")
    legacy = LEGACY + "    import atexit\n    atexit.register(send_to_gateway, 1, 2)\n"
    rep = _scan(tmp_path / "d", "", {"legacy.py": legacy}, doc=ADOC)
    assert rep.rule_result("PAY-1") != "fail", rep.for_rule("PAY-1")


def test_value_taken_below_the_door_or_inside_allow_is_quiet(tmp_path):
    below = PAYMENTS.rstrip(" ").replace("        _send(charge_id, amount)\n",
                             "        _send(charge_id, 0)\n        _dispatch(charge_id, amount)\n") + \
        "    def _dispatch(charge_id, amount):\n        import threading\n" \
        "        threading.Thread(target=_send, args=(charge_id, amount)).start()\n"
    files = {"payments.py": below, "store.py": STORE, "ops.py": "from payments import refund\nrefund(1, 2)\n"}
    rep = scan.scan(DOC, [write_project(tmp_path / "a", {k: textwrap.dedent(v) for k, v in files.items()})])
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")
    doc = copy.deepcopy(DOC)
    doc["rules"][0]["mediation"]["allow"] = ["py:legacy.admin_goodwill", "py:legacy._h"]
    legacy = "from payments import send_to_gateway\ndef _h():\n    send_to_gateway(1, 2)\n" \
             "def admin_goodwill():\n    import atexit\n    atexit.register(_h)\n"
    rep = _scan(tmp_path / "b", "", {"legacy.py": legacy}, doc=doc)
    assert rep.rule_result("PAY-1") == "pass", rep.for_rule("PAY-1")


def test_value_of_a_function_the_path_search_reaches_is_not_reported_twice(tmp_path):
    rep = _scan(tmp_path, "from payments import _send\n_send(1, 2)\nx = _send\n")
    codes = _codes(rep, "PAY-1")
    assert "bypass" in codes and "unresolved/sink-escapes" not in codes, rep.for_rule("PAY-1")


def test_property_read_is_a_call_not_a_value(tmp_path):
    rep = _scan(tmp_path, "from store import Repo\nclass H:\n    def __init__(self):\n        self.repo = Repo()\n"
                          "    @property\n    def total(self):\n        return self.repo.save(1)\nprint(H().total)\n")
    codes = _codes(rep, "ST-1")
    assert "bypass" in codes and "unresolved/sink-escapes" not in codes, rep.for_rule("ST-1")
    store = STORE.replace("        def write(self, amount):\n            self._persist(amount)\n",
                          "        @property\n        def _p(self):\n            return self.repo.save(1)\n\n"
                          "        def write(self, amount):\n            return self._p\n")
    assert store != STORE
    files = {"payments.py": PAYMENTS, "store.py": store, "ops.py": "from store import Service\nService().write(1)\n"}
    rep = scan.scan(DOC, [write_project(tmp_path / "door", {k: textwrap.dedent(v) for k, v in files.items()})])
    assert "store.Service._p" not in [f.owner for f in rep.for_rule("ST-1")], rep.for_rule("ST-1")


# --- C: class identity -------------------------------------------------------------------------------------
CLASSES = {
    "deco-partial": "import functools\nfrom store import Repo\n@functools.partial(lambda r, c: r, Repo)\n"
                    "class Quiet:\n    def save(self, amount): pass\nQuiet().save(1)\n",
    "deco-rebound": "from store import Repo\ndef tidy(c):\n    c = Repo\n    return c\n@tidy\n"
                    "class Quiet:\n    def save(self, amount): pass\nQuiet().save(1)\n",
    "deco-factory-rebound": "from store import Repo\ndef reg(name):\n    def inner(c):\n        c = Repo\n"
                            "        return c\n    return inner\n@reg('x')\n"
                            "class Quiet:\n    def save(self, amount): pass\nQuiet().save(1)\n",
    "third-party-deco": "import attrs_like\nfrom store import Repo\n@attrs_like.swap\n"
                        "class Quiet:\n    def save(self, amount): pass\nQuiet().save(1)\n",
    "new-kw-cls": "from store import Repo\nclass Quiet:\n    def __new__(klass, *a, cls=Repo):\n"
                  "        return super().__new__(cls)\n    def save(self, amount): pass\nQuiet().save(1)\n",
    "new-rebound": "from store import Repo\nclass Quiet:\n    def __new__(cls, *a):\n        cls = Repo\n"
                   "        return object.__new__(cls)\n    def save(self, amount): pass\nQuiet().save(1)\n",
    "getattribute": "from store import Repo\nclass Quiet:\n    def __getattribute__(self, n):\n"
                    "        return object.__getattribute__(Repo(), n)\n    def save(self, amount): pass\n"
                    "Quiet().save(1)\n",
    "getattr-missing": "from store import Repo\nclass Quiet:\n    def __getattr__(self, n):\n"
                       "        return getattr(Repo(), n)\nQuiet().save(1)\n",
    "dict-update-literal": "from store import Repo, Safe\nclass H:\n    def __init__(self):\n        self.r = Safe()\n"
                           "        self.__dict__.update({'r': Repo()})\n    def go(self):\n        self.r.save(1)\n"
                           "H().go()\n",
    "dict-update-mapping": "from store import Repo, Safe\nclass H:\n    def __init__(self, **kw):\n"
                           "        self.r = Safe()\n        self.__dict__.update(kw)\n    def go(self):\n"
                           "        self.r.save(1)\nH(r=Repo()).go()\n",
    "dict-alias": "from store import Repo, Safe\nclass H:\n    def __init__(self):\n        self.r = Safe()\n"
                  "        d = self.__dict__\n        d['r'] = Repo()\n    def go(self):\n        self.r.save(1)\n"
                  "H().go()\n",
    "protocol": "from typing import Protocol\nfrom store import Repo\nclass Saver(Protocol):\n"
                "    def save(self, amount): ...\ndef go(s: Saver):\n    s.save(1)\ngo(Repo())\n",
    "abc-register": "import abc\nfrom store import Repo\nclass Quiet(abc.ABC):\n    def save(self, amount): pass\n"
                    "Quiet.register(Repo)\ndef go(s: Quiet):\n    s.save(1)\ngo(Repo())\n",
    "class-level-annot":"from store import Repo, Safe\nclass H:\n    r: Safe\n    def __init__(self):\n"
                         "        self.__dict__.update({'r': Repo()})\n    def go(self):\n        self.r.save(1)\n"
                         "H().go()\n",
}


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_class_identity_holes(tmp_path, name):
    rep = _scan(tmp_path, CLASSES[name], {"attrs_like.py": "def swap(c):\n    return c\n"}
                if name != "third-party-deco" else None)
    assert rep.rule_result("ST-1") != "pass", (name, rep.for_rule("ST-1"))


def test_dict_literal_update_names_the_attributes_it_sets(tmp_path):
    rep = _scan(tmp_path, "from store import Repo, Safe\nclass H:\n    def __init__(self):\n        self.r = Safe()\n"
                          "    def go(self):\n        self.r.save(1)\nh = H()\nh.__dict__.update({'r': Repo()})\nh.go()\n")
    codes = _codes(rep, "ST-1")
    assert rep.rule_result("ST-1") != "pass" and "unresolved/computed-attribute" not in codes, codes
    rep = _scan(tmp_path / "m", "from store import Repo, Safe\nclass H:\n    def __init__(self):\n        self.r = Safe()\n"
                                "    def go(self):\n        self.r.save(1)\nh = H()\nh.__dict__.update(dict(r=Repo()))\n")
    assert "unresolved/computed-attribute" in _codes(rep, "ST-1"), rep.for_rule("ST-1")


TRUSTED = {
    "dataclass": "import dataclasses\n@dataclasses.dataclass\nclass Quiet:\n    x: int = 0\n"
                 "    def save(self, amount): pass\nQuiet().save(1)\n",
    "dataclass-call": "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass Quiet:\n    x: int = 0\n"
                      "    def save(self, amount): pass\nQuiet().save(1)\n",
    "total-ordering": "import functools\n@functools.total_ordering\nclass Quiet:\n"
                      "    def __lt__(self, o): return True\n    def save(self, amount): pass\nQuiet().save(1)\n",
    "register": "R = []\ndef register(c):\n    R.append(c)\n    return c\n@register\nclass Quiet:\n"
                "    def save(self, amount): pass\nQuiet().save(1)\n",
    "own-getattr": "class Quiet:\n    def __getattr__(self, n):\n        return None\n"
                   "    def save(self, amount): pass\nQuiet().save(1)\n",
    "new-super": "class Quiet:\n    def __new__(cls, *a, **k):\n        return super().__new__(cls)\n"
                 "    def save(self, amount): pass\nQuiet().save(1)\n",
}


@pytest.mark.parametrize("name", sorted(TRUSTED))
def test_identity_preserving_classes_keep_their_type(tmp_path, name):
    rep = _scan(tmp_path, TRUSTED[name])
    assert rep.rule_result("ST-1") == "pass", (name, rep.for_rule("ST-1"))


# --- R: resolvers ------------------------------------------------------------------------------------------
C = "payments.send_to_gateway(1, 2)"
RESOLVERS = {
    "formatter": "import string, payments\nstring.Formatter().get_field('0.send_to_gateway', [payments], {})[0](1, 2)\n",
    "formatter-vformat": "import string, payments\nf = string.Formatter()\nf.vformat('{0.send_to_gateway}', [payments], {})\n",
    "code-interp": f"import code\ncode.InteractiveInterpreter().runsource('import payments; {C}')\n",
    "code-console": f"from code import InteractiveConsole\nInteractiveConsole().push('import payments; {C}')\n",
    "frame-globals": "import sys\nfrom payments import send_to_gateway as s\ndef go():\n"
                     "    sys._getframe(0).f_globals['s'](1, 2)\ngo()\n",
    "currentframe": "import inspect\nfrom payments import send_to_gateway as s\n"
                    "inspect.currentframe().f_globals['s'](1, 2)\n",
    "frame-locals": "import inspect\nfrom payments import send_to_gateway as s\n"
                    "def go(f):\n    return f.f_back.f_locals['s']\n",
    "timeit": f"import timeit\ntimeit.timeit('{C}', setup='import payments', number=1)\n",
    "timer": f"import timeit\ntimeit.Timer(stmt='{C}', setup='import payments').timeit(1)\n",
    "doctest": "import doctest\ndef f():\n    pass\ndoctest.run_docstring_examples(f, {})\n",
    "doctest-testmod": "import doctest\ndoctest.testmod()\n",
    "trace": f"import trace\ntrace.Trace(count=0, trace=0).run('import payments; {C}')\n",
    "gc": "import gc\nfs = [o for o in gc.get_objects() if getattr(o, '__name__', '') == 'send_to_gateway']\nfs[0](1, 2)\n",
    "gc-referrers": "import gc, payments\ngc.get_referents(payments.__dict__)\n",
    "getmodule": "import inspect, payments\nm = inspect.getmodule(payments.refund)\n"
                 "dict(inspect.getmembers(m))['send_to_gateway'](1, 2)\n",
    "subprocess": f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'import payments; {C}'])\n",
    "os-system": f"import os\nos.system('python3 -c \"import payments; {C}\"')\n",
    "popen-script": "import subprocess\nsubprocess.Popen(['python', 'tools/run.py'])\n",
    "execv": "import os, sys\nos.execv(sys.executable, [sys.executable, '-m', 'ops2'])\n",
}


@pytest.mark.parametrize("name", sorted(RESOLVERS))
def test_resolvers_are_reflection(tmp_path, name):
    rep = _scan(tmp_path, RESOLVERS[name])
    assert "unresolved/reflection" in _codes(rep, "PAY-1"), (name, rep.for_rule("PAY-1"))


@pytest.mark.parametrize("ops", ["import subprocess\nsubprocess.run(['git', 'status'])\n",
                                 "import timeit\ndef f():\n    pass\ntimeit.timeit(f, number=1)\n",
                                 "import string\nstring.Template('$x').substitute(x=1)\n"])
def test_harmless_runners_stay_quiet(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert "unresolved/reflection" not in _codes(rep, "PAY-1"), rep.for_rule("PAY-1")


# --- M: annotation over an unannotated factory -------------------------------------------------------------
def test_annotation_over_an_unannotated_factory_returning_another_class(tmp_path):
    rep = _scan(tmp_path, "from store import Repo, Safe\ndef mk():\n    return Repo()\n"
                          "def get() -> Safe:\n    return mk()\nget().save(1)\n")
    assert rep.rule_result("ST-1") != "pass", rep.for_rule("ST-1")
    rep = _scan(tmp_path / "ok", "from store import Safe\ndef mk():\n    return Safe()\n"
                                 "def get() -> Safe:\n    return mk()\nget().save(1)\n")
    assert rep.rule_result("ST-1") == "pass", rep.for_rule("ST-1")


# --- S: a nested scope's bindings type only that scope -----------------------------------------------------
SCOPES = {
    "inner-def": "from store import Repo, Safe\nrepo = Repo()\ndef go():\n    def inner():\n        repo = Safe()\n"
                 "        return repo\n    repo.save(1)\n",
    "inner-class": "from store import Repo, Safe\nrepo = Repo()\ndef go():\n    class K:\n        repo = Safe()\n"
                   "    repo.save(1)\n",
    "inner-lambda-param": "from store import Repo, Safe\nrepo = Repo()\ndef go():\n    f = lambda repo=Safe(): repo\n"
                          "    repo.save(1)\n",
    "outer-into-inner": "from store import Repo, Safe\nrepo = Repo()\ndef go():\n    repo = Safe()\n"
                        "    def inner():\n        global repo\n        repo.save(1)\n    inner()\n",
    "sibling": "from store import Repo, Safe\nrepo = Repo()\ndef go():\n    def a():\n        repo = Safe()\n"
               "        return repo\n    def b():\n        repo.save(1)\n    b()\n",
}


@pytest.mark.parametrize("name", sorted(SCOPES))
def test_nested_scope_bindings_do_not_type_the_outer_name(tmp_path, name):
    rep = _scan(tmp_path, SCOPES[name])
    assert rep.rule_result("ST-1") != "pass", (name, rep.for_rule("ST-1"))


@pytest.mark.parametrize("ops", [
    ("from store import Repo, Safe\ndef go():\n    repo = Repo()\n    def inner():\n        repo = Safe()\n"
     "        return repo\n    repo.save(1)\n"),
    ("from store import Repo, Safe\ndef go():\n    repo = Safe()\n    def inner():\n        repo = Repo()\n"
     "        repo.save(1)\n    inner()\n")])
def test_nested_scope_bindings_keep_each_scope_typed(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert "bypass" in _codes(rep, "ST-1"), rep.for_rule("ST-1")


@pytest.mark.parametrize("ops", [
    "from store import Safe\ndef go():\n    def inner():\n        repo = Safe()\n        repo.save(1)\n    inner()\n",
    "from store import Safe\ndef go():\n    repo = Safe()\n    def inner():\n        repo.save(1)\n    inner()\n"])
def test_nested_scope_safe_binding_stays_typed(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert rep.rule_result("ST-1") == "pass", rep.for_rule("ST-1")
