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
"""
from __future__ import annotations

import textwrap

import pytest

from invariants import lockfile, scan
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
    return scan.scan(_doc(allow, sink), [write_project(tmp_path, files)], lockfile.DEFAULT_SCAN_EXCLUDE)


def _codes(rep):
    return [f.code for f in rep.findings]


UNKNOWN = ["unresolved/inferred-path", "unresolved/unknown-receiver"]    # a name-matched .save()


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
    rep = scan.scan(_doc(), [write_project(tmp_path, files)], lockfile.DEFAULT_SCAN_EXCLUDE)
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
                    lockfile.DEFAULT_SCAN_EXCLUDE)
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

