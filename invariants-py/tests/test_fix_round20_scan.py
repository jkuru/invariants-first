"""Fix round 20 (scan review): each test failed before its fix.

a. `getattr(door, "__wrapped__")` inside an allowed back door is an unwrap (forbidden/unwrap), as
   `door.__wrapped__` is there: allow freezes the ways it reaches the sink, not a lifted guard.
b. `ex.f.__call__(x)` on an excluded module is a call into it (unresolved/excluded-callee), as `ex.f(x)` is;
   so is `getattr.__call__(o, "save")` a getattr, and `eval.__call__(...)` reflection.
c. `type(call()).__dict__` and `call().__class__.__dict__`: the calls inside are walked (a sink call
   hidden there was never seen).
d. A name a nested function or lambda takes as a parameter (in its body), an `except E as name` and a
   match capture are bindings the scan cannot type, so another binding of the same name no longer
   types them; a string `-> "Self"` on an inherited `__enter__` is the receiver's class, not the base's.
e. A sink's name read as a value on a receiver the scan cannot type (`cb = repo.save`) is
   unresolved/sink-escapes, matched by name as a call on an unknown receiver is.
f. Assigning or deleting a sink's name on an object: unresolved/unknown-receiver on a receiver the scan
   cannot type (by name), forbidden/monkeypatch on a typed one.
"""
from __future__ import annotations

import textwrap

import pytest

from invariants import lock, scan
from cli_support import write_project

REPO = """
    class Base:
        def __enter__(self) -> "Self":
            return self

        def __exit__(self, *a):
            return None

    class SinkRepo(Base):
        def save(self, x):
            pass

    class Other(Base):
        def save(self, x):
            pass

    class SinkErr(Exception):
        def save(self, x):
            pass
    """
SVC = """
    import functools
    from app.repo import SinkRepo

    def guard(f):
        @functools.wraps(f)
        def w(*a, **k):
            return f(*a, **k)
        return w

    @guard
    def door(repo: SinkRepo, x):
        repo.save(x)
    """
HELPER = """
    def write(x):
        pass
    """


def _doc(allow=None, sink="py:app.repo.SinkRepo.save"):
    door = "py:app.svc.door"
    med = {"sink": sink, "door": door}
    if allow:
        med["allow"] = allow
    return {"targets": {door: {"effects": "external"}}, "rules": [{
        "id": "T-1", "status": "candidate", "subject": {"target": door}, "bad": "false",
        "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
        "mediation": med}]}


def _scan(tmp_path, ops, allow=None, sink="py:app.repo.SinkRepo.save"):
    files = {"app/__init__.py": "", "app/repo.py": textwrap.dedent(REPO), "app/svc.py": textwrap.dedent(SVC),
             "app/ops.py": textwrap.dedent(ops), "app/testing/__init__.py": "",
             "app/testing/helpers.py": textwrap.dedent(HELPER)}
    return scan.scan(_doc(allow, sink), [write_project(tmp_path, files)], lock.DEFAULT_SCAN_EXCLUDE)


def _codes(rep):
    return [f.code for f in rep.findings]


UNKNOWN = ["unresolved/inferred-path"]    # a name-matched .save(): one finding per call site (round 23)


# --- a. unwrapping a door inside an allowed back door ------------------------------------------------------
@pytest.mark.parametrize("unwrap", ['getattr(svc.door, "__wrapped__")(repo, x)',
                                    'getattr(door, "__wrapped__")(repo, x)',
                                    'operator.attrgetter("__wrapped__")(door)(repo, x)',
                                    'svc.door.__wrapped__(repo, x)'],
                         ids=["getattr-module", "getattr-name", "attrgetter", "attribute"])
def test_unwrapping_a_door_inside_an_allowed_back_door_is_reported(tmp_path, unwrap):
    rep = _scan(tmp_path, f"""
        import operator
        from app import svc
        from app.svc import door
        def backfill(repo, x):
            {unwrap}
        """, ["py:app.ops.backfill"])
    assert _codes(rep) == ["forbidden/unwrap"], rep.findings
    assert rep.findings[0].owner == "app.ops.backfill"


def test_a_literal_getattr_inside_an_allowed_back_door_stays_frozen(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import SinkRepo
        def backfill(repo: SinkRepo, x):
            getattr(repo, "save")(x)
        """, ["py:app.ops.backfill"])
    assert rep.findings == [], rep.findings


# --- b. __call__ on an excluded module's function ----------------------------------------------------------
@pytest.mark.parametrize("call", ["helpers.write(x)", "helpers.write.__call__(x)",
                                  "helpers.write.__call__.__call__(x)", "write.__call__(x)"])
def test_calling_an_excluded_function_through_dunder_call_is_reported(tmp_path, call):
    rep = _scan(tmp_path, f"""
        from app.testing import helpers
        from app.testing.helpers import write
        def handler(x):
            {call}
        """)
    assert _codes(rep) == ["unresolved/excluded-callee"], rep.findings
    assert "calls app.testing.helpers.write" in rep.findings[0].message


@pytest.mark.parametrize("call,code", [
    ('getattr.__call__(SinkRepo(), "save")(x)', "forbidden/getattr-sink"),
    ('operator.methodcaller.__call__("save", x)(SinkRepo())', "forbidden/getattr-sink"),
    ("eval.__call__(x)", "unresolved/reflection"),
    ("importlib.import_module.__call__(x)", "unresolved/reflection"),
], ids=["getattr", "methodcaller", "eval", "import_module"])
def test_a_reflective_builtin_called_through_dunder_call_is_reported(tmp_path, call, code):
    rep = _scan(tmp_path, f"""
        import importlib, operator
        from app.repo import SinkRepo
        def handler(x):
            {call}
        """)
    assert _codes(rep) == [code], rep.findings


# --- c. calls inside type(...).__dict__ and ...__class__.__dict__ ------------------------------------------
@pytest.mark.parametrize("expr", ['type(SinkRepo().save(x)).__dict__',
                                  'SinkRepo().save(x).__class__.__dict__',
                                  'type(SinkRepo().save(x)).__dict__["k"]',
                                  'type(SinkRepo().save(x)).__dict__.get("k")',
                                  'SinkRepo().save(x).__class__.__dict__.items()'])
def test_a_sink_call_inside_a_class_dict_lookup_is_walked(tmp_path, expr):
    rep = _scan(tmp_path, f"""
        from app.repo import SinkRepo
        def handler(x):
            return {expr}
        """)
    assert "bypass" in _codes(rep), rep.findings
    assert [f.path for f in rep.findings if f.code == "bypass"] == [["app.ops.handler", "app.repo.SinkRepo.save"]]


# --- d. names an outer binding must not type ---------------------------------------------------------------
@pytest.mark.parametrize("inner", [
    "def inner(repo):\n        repo.save(x)\n    inner(SinkRepo())",
    "async def inner(*, repo):\n        repo.save(x)\n    return inner(repo=SinkRepo())",
    "def inner(*repo):\n        repo.save(x)\n    inner(SinkRepo())",
    "f = lambda repo: repo.save(x)\n    f(SinkRepo())",
], ids=["def", "async-kwonly", "vararg", "lambda"])
def test_a_nested_parameter_is_not_typed_by_an_outer_binding(tmp_path, inner):
    # repo = Other() typed the inner parameter as Other: repo.save resolved to Other.save and the path to
    # the sink (the argument is a SinkRepo) was hidden. The inner repo is unknown (name-matched); the
    # outer one is still an Other
    rep = _scan(tmp_path, "from app.repo import SinkRepo, Other\n"
                          "def handler(x):\n    repo = Other()\n    repo.save(x)\n    " + inner + "\n")
    assert sorted(_codes(rep)) == UNKNOWN, rep.findings
    assert {f.line for f in rep.findings} == {5 if "lambda" in inner else 6}, rep.findings


def test_an_except_name_is_not_typed_by_an_outer_binding(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import Other, SinkErr
        def handler(x):
            err = Other()
            try:
                raise SinkErr()
            except SinkErr as err:
                err.save(x)
        """, sink="py:app.repo.SinkErr.save")
    assert sorted(_codes(rep)) == UNKNOWN, rep.findings


def test_a_match_capture_is_not_typed_by_an_outer_binding(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import SinkRepo, Other
        def handler(x, y):
            repo = Other()
            match y:
                case SinkRepo() as repo:
                    repo.save(x)
        """)
    assert sorted(_codes(rep)) == UNKNOWN, rep.findings


def test_an_outer_binding_still_types_its_own_uses(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import SinkRepo
        def handler(x):
            repo = SinkRepo()
            repo.save(x)
            def inner(other):
                other.save(x)
        """)
    assert sorted(_codes(rep)) == ["bypass", *UNKNOWN], rep.findings


@pytest.mark.parametrize("ann", ['"Self"', "Self", "typing.Self", '"typing.Self"'])
def test_an_inherited_enter_returning_self_is_the_receivers_class(tmp_path, ann):
    files = {"app/__init__.py": "", "app/svc.py": textwrap.dedent(SVC), "app/repo.py": textwrap.dedent(f"""
        import typing
        from typing import Self
        class Base:
            def __enter__(self) -> {ann}:
                return self
            def __exit__(self, *a):
                return None
        class SinkRepo(Base):
            def save(self, x):
                pass
        """), "app/ops.py": textwrap.dedent("""
        from app.repo import SinkRepo
        def handler(x):
            with SinkRepo() as repo:
                repo.save(x)
        """)}
    rep = scan.scan(_doc(), [write_project(tmp_path, files)], lock.DEFAULT_SCAN_EXCLUDE)
    assert _codes(rep) == ["bypass"], rep.findings
    assert rep.findings[0].path == ["app.ops.handler", "app.repo.SinkRepo.save"]


def test_a_class_named_self_is_not_typing_self(tmp_path):
    # `-> Self` where Self is a scanned class: the class it names, not the receiver
    files = {"app/__init__.py": "", "app/svc.py": textwrap.dedent(SVC), "app/repo.py": textwrap.dedent("""
        class Self:
            def save(self, x):
                pass
        class Base:
            def __enter__(self) -> Self:
                return Self()
            def __exit__(self, *a):
                return None
        class SinkRepo(Base):
            def save(self, x):
                pass
        """), "app/ops.py": textwrap.dedent("""
        from app.repo import SinkRepo
        def handler(x):
            with SinkRepo() as repo:
                repo.save(x)
        """)}
    rep = scan.scan(_doc(sink="py:app.repo.Self.save"), [write_project(tmp_path, files)],
                    lock.DEFAULT_SCAN_EXCLUDE)
    assert _codes(rep) == ["bypass"], rep.findings
    assert rep.findings[0].path == ["app.ops.handler", "app.repo.Self.save"]


def test_a_nested_parameter_does_not_untype_the_outer_name(tmp_path):
    # only the nested function's own uses are unknown: the outer `cb = repo.save` still escapes
    rep = _scan(tmp_path, """
        from app.repo import SinkRepo
        def handler(x):
            repo = SinkRepo()
            cb = repo.save
            def inner(repo):
                return repo
            return cb, inner
        """)
    assert _codes(rep) == ["unresolved/sink-escapes"], rep.findings


# --- e. a sink name read as a value on a receiver the scan cannot type -------------------------------------
@pytest.mark.parametrize("body", [
    "def h(repo, x):\n    cb = repo.save\n    cb(x)\n",
    "def h(xs, x):\n    repo = SinkRepo()\n    for repo in xs:\n        pass\n    cb = repo.save\n",
    "def h(x):\n    try:\n        pass\n    except SinkErr as err:\n        cb = err.save\n",
    "def h(y):\n    match y:\n        case SinkRepo() as repo:\n            cb = repo.save\n",
    "def h(repo, x):\n    return [repo.save]\n",
    "def h(repo, x):\n    return functools.partial(repo.save, x)\n",
    "def h(repo, x, o):\n    return repo.save.__get__(o)(x)\n",
    "def h(repo, x):\n    return repo.save.__func__\n",
], ids=["param", "for", "except", "match", "list", "partial", "get", "func"])
def test_a_sink_name_read_as_a_value_on_an_unknown_receiver_is_reported(tmp_path, body):
    rep = _scan(tmp_path, "import functools\nfrom app.repo import SinkRepo, SinkErr\n" + body)
    assert _codes(rep) == ["unresolved/sink-escapes"], rep.findings
    assert ".save on a receiver the scan cannot resolve" in rep.findings[0].message


def test_a_sink_method_called_through_dunder_call_on_an_unknown_receiver_is_name_matched(tmp_path):
    rep = _scan(tmp_path, "def h(repo, x):\n    repo.save.__call__(x)\n")
    assert sorted(_codes(rep)) == UNKNOWN, rep.findings


@pytest.mark.parametrize("body", [
    "def h(repo):\n    return repo.other\n",                     # not a sink name
    "def h(repo, x):\n    repo.save(x)\n",                      # a call: unknown-receiver, as before
    "def h(d):\n    d = {}\n    return d.update\n",              # a builtin container's own method
], ids=["other-name", "call", "container"])
def test_other_reads_on_an_unknown_receiver_are_not_sink_escapes(tmp_path, body):
    rep = _scan(tmp_path, body)
    assert "unresolved/sink-escapes" not in _codes(rep), rep.findings


def test_a_sink_name_read_inside_a_door_or_an_allowed_function_stays_frozen(tmp_path):
    rep = _scan(tmp_path, "def backfill(repo, x):\n    cb = repo.save\n    return cb\n", ["py:app.ops.backfill"])
    assert rep.findings == [], rep.findings


# --- f. a sink's name assigned or deleted on a receiver ----------------------------------------------------
@pytest.mark.parametrize("body", [
    "def h(repo):\n    repo.save = print\n",
    "def h(repo):\n    del repo.save\n",
    "def h(repo):\n    repo.save += 1\n",
    "def h(repo):\n    repo.save: int = 1\n",
    "def h(repo):\n    repo.save, x = print, 1\n",
    "def h(repo):\n    repo.inner.save = print\n",
    "def h(repo):\n    for repo.save in []:\n        pass\n",
    "def h(xs):\n    repo = SinkRepo()\n    for repo in xs:\n        pass\n    repo.save = print\n",
], ids=["assign", "del", "augassign", "annassign", "unpack", "chain", "for-target", "rebound"])
def test_rebinding_a_sink_name_on_an_unknown_receiver_is_reported(tmp_path, body):
    rep = _scan(tmp_path, "from app.repo import SinkRepo\n" + body)
    assert _codes(rep) == ["unresolved/unknown-receiver"], rep.findings
    assert "assigns or deletes .save on a receiver the scan cannot resolve" in rep.findings[0].message


@pytest.mark.parametrize("body", [
    "def h():\n    repo = SinkRepo()\n    repo.save = print\n",
    "def h():\n    repo = SinkRepo()\n    del repo.save\n",
    "def h(repo: SinkRepo):\n    repo.save = print\n",
    "import app.repo as r\ndef h():\n    del r.SinkRepo.save\n",
], ids=["constructed", "del", "annotated", "del-class-attr"])
def test_rebinding_the_sink_on_a_typed_receiver_is_a_monkeypatch(tmp_path, body):
    rep = _scan(tmp_path, "from app.repo import SinkRepo\n" + body)
    assert _codes(rep) == ["forbidden/monkeypatch"], rep.findings


def test_a_base_init_that_sets_the_name_of_a_subclass_sink_is_a_monkeypatch(tmp_path):
    files = {"app/__init__.py": "", "app/svc.py": textwrap.dedent(SVC), "app/ops.py": "",
             "app/repo.py": "class Base:\n    def __init__(self):\n        self.save = print\n"
                            "class SinkRepo(Base):\n    def save(self, x):\n        pass\n"}
    rep = scan.scan(_doc(), [write_project(tmp_path, files)], lock.DEFAULT_SCAN_EXCLUDE)
    assert _codes(rep) == ["forbidden/monkeypatch"], rep.findings


@pytest.mark.parametrize("body", [
    "def h(repo):\n    repo.other = print\n",                           # not a sink name
    "def h():\n    repo = Other()\n    repo.save = print\n",              # a typed receiver that is not the sink
    "def h():\n    repo = Other()\n    del repo.save\n",
    "def h(d: dict):\n    d.save = 1\n",                                   # a builtin container
    "def h():\n    repo = Other()\n    repo.flag = 1\n",
], ids=["other-name", "typed-other", "typed-other-del", "container", "typed-other-name"])
def test_other_rebindings_are_not_reported(tmp_path, body):
    rep = _scan(tmp_path, "from app.repo import SinkRepo, Other\n" + body)
    assert rep.findings == [], rep.findings


@pytest.mark.parametrize("body,codes", [
    ("        self.save = print\n", ["forbidden/monkeypatch"]),             # self is typed: the sink itself
    ("        self.flag = 1\n", []),
], ids=["sink", "other"])
def test_assignments_in_the_sink_class_init_keep_their_typed_result(tmp_path, body, codes):
    files = {"app/__init__.py": "", "app/svc.py": textwrap.dedent(SVC), "app/ops.py": "",
             "app/repo.py": "class SinkRepo:\n    def __init__(self):\n" + body + "    def save(self, x):\n        pass\n"}
    rep = scan.scan(_doc(), [write_project(tmp_path, files)], lock.DEFAULT_SCAN_EXCLUDE)
    assert _codes(rep) == codes, rep.findings

