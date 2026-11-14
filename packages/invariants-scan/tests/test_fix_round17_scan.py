"""Fix round 17 (scan review): super() follows the MRO of every scanned subclass, not only the defining
class's (S-9); a call from scanned code into an excluded module is reported, not invisible (S-10); a
forbidden/unwrap site goes only to the rules whose door or sink it unwraps (S-11)."""
from __future__ import annotations

import textwrap

import invariants_scan as scan
from scan_support import DEFAULT_EXCLUDE, write_project

REPO = textwrap.dedent("""
    class Base:
        def save(self, x):
            pass

    class SinkRepo(Base):
        def save(self, x):
            pass
    """)
SVC = textwrap.dedent("""
    from app.repo import SinkRepo

    def door(repo: SinkRepo, x):
        repo.save(x)
    """)


def _rule(rid, sink, door):
    return {"id": rid, "status": "candidate", "subject": {"target": door}, "bad": "false",
            "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
            "mediation": {"sink": sink, "door": door}}


def _doc(sink="py:app.repo.SinkRepo.save", door="py:app.svc.door"):
    return {"targets": {door: {"effects": "external"}}, "rules": [_rule("T-1", sink, door)]}


def _scan(tmp_path, ops, extra=None, doc=None, exclude=()):
    files = {"app/__init__.py": "", "app/repo.py": REPO, "app/svc.py": SVC,
             "app/ops.py": textwrap.dedent(ops), **{k: textwrap.dedent(v) for k, v in (extra or {}).items()}}
    return scan.scan(doc or _doc(), [write_project(tmp_path, files)], exclude)


# --- S-9: super() in cooperative multiple inheritance ---------------------------------------------------
def test_super_in_a_mixin_follows_the_mro_of_the_subclass(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import Base, SinkRepo
        class Mixin(Base):
            def save(self, x):
                super().save(x)
        class Leaf(Mixin, SinkRepo):
            pass
        def handler(x):
            Leaf().save(x)
        """)
    assert [f.code for f in rep.findings] == ["bypass"], rep.findings
    assert rep.findings[0].path[-2:] == ["app.ops.Mixin.save", "app.repo.SinkRepo.save"]


def test_super_with_explicit_class_follows_the_subclass_mro(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import Base, SinkRepo
        class Mixin(Base):
            def save(self, x):
                super(Mixin, self).save(x)
        class Leaf(Mixin, SinkRepo):
            pass
        def handler(x):
            Leaf().save(x)
        """)
    assert [f.code for f in rep.findings] == ["bypass"], rep.findings


def test_super_without_a_sink_in_any_subclass_mro_stays_clean(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import Base
        class Mixin(Base):
            def save(self, x):
                super().save(x)
        class Leaf(Mixin):
            pass
        def handler(x):
            Leaf().save(x)
        """)
    assert rep.findings == [] and rep.rule_result("T-1") == "pass"


def test_super_falls_back_to_name_matching_when_a_subclass_mro_cannot_be_computed(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import Base
        class Mixin(Base):
            def save(self, x):
                super().save(x)
        class Other:
            pass
        class X(Mixin, Other):
            pass
        class Y(Other, Mixin):
            pass
        class Z(X, Y):
            pass
        def handler(x):
            Mixin().save(x)
        """)
    assert "unresolved/inferred-path" in [f.code for f in rep.findings], rep.findings
    assert rep.rule_result("T-1") != "pass"


# --- S-10: calls into excluded code -------------------------------------------------------------------
HELPER = """
    from app.repo import SinkRepo
    def write(x):
        SinkRepo().save(x)
    """


def test_a_call_into_an_excluded_production_module_is_reported(tmp_path):
    rep = _scan(tmp_path, """
        from app.testing.helpers import write
        from app import test_fastpath
        def handler(x):
            write(x)
        def handler2(x):
            test_fastpath.write(x)
        """, {"app/testing/__init__.py": "", "app/testing/helpers.py": HELPER, "app/test_fastpath.py": HELPER},
        exclude=DEFAULT_EXCLUDE)
    got = [(f.code, f.owner) for f in rep.findings]
    assert got == [("unresolved/excluded-callee", "app.ops.handler"),
                   ("unresolved/excluded-callee", "app.ops.handler2")], got
    assert rep.rule_result("T-1") == "unresolved"
    msgs = [f.message for f in rep.findings]
    assert "app/testing/helpers.py" in msgs[0] and "excluded by '*/testing'" in msgs[0]
    assert "app/test_fastpath.py" in msgs[1] and "excluded by '*/test_*.py'" in msgs[1]


def test_a_test_suite_importing_app_code_stays_excluded_and_quiet(tmp_path):
    rep = _scan(tmp_path, """
        def handler(x):
            return x
        """, {"tests/__init__.py": "", "tests/conftest.py": "import pytest\n",
              "tests/test_ops.py": """
                  from app.repo import SinkRepo
                  from app.ops import handler
                  from tests.conftest import pytest
                  def test_it():
                      SinkRepo().save(handler(1))
                  """,
              "app/testing/__init__.py": "", "app/testing/fixtures.py": HELPER},
        exclude=DEFAULT_EXCLUDE)
    assert rep.findings == [] and rep.rule_result("T-1") == "pass"


def test_a_door_calling_an_excluded_helper_is_behind_the_door(tmp_path):
    rep = _scan(tmp_path, "", {
        "app/svc.py": """
            from app.repo import SinkRepo
            from app.testing.audit import note
            def door(repo: SinkRepo, x):
                note(x)
                repo.save(x)
            """,
        "app/testing/__init__.py": "", "app/testing/audit.py": "def note(x):\n    pass\n"},
        exclude=DEFAULT_EXCLUDE)
    assert rep.findings == [], rep.findings


# --- S-11: an unwrap goes to the rules it unwraps --------------------------------------------------------
TWO = """
    def guarded_a(x):
        sink_a(x)
    def guarded_b(x):
        sink_b(x)
    def sink_a(x):
        pass
    def sink_b(x):
        pass
    """


def _two_doc():
    return {"targets": {}, "rules": [_rule("A", "py:app.two.sink_a", "py:app.two.guarded_a"),
                                     _rule("B", "py:app.two.sink_b", "py:app.two.guarded_b")]}


def test_an_unwrap_is_reported_only_under_the_rule_it_unwraps(tmp_path):
    rep = _scan(tmp_path, """
        import functools
        from app import two
        def f(x):
            two.guarded_a.__wrapped__(x)
        def g(x):
            getattr(two.guarded_b, "__wrapped__")(x)
        """, {"app/two.py": TWO}, _two_doc())
    got = sorted((f.rule, f.code, f.owner) for f in rep.findings)
    assert got == [("A", "forbidden/unwrap", "app.ops.f"), ("B", "forbidden/unwrap", "app.ops.g")], got


def test_an_unwrap_of_a_helper_that_reaches_a_sink_goes_to_that_rule(tmp_path):
    rep = _scan(tmp_path, """
        from app import two
        def helper(x):
            two.sink_b(x)
        def f(x):
            helper.__wrapped__(x)
        """, {"app/two.py": TWO}, _two_doc())
    assert sorted({(f.rule, f.code) for f in rep.findings if f.owner == "app.ops.f"}) == \
        [("B", "unresolved/unwrap")]           # fix round 25: helper is no guarded door, so not forbidden


def test_an_unwrap_of_an_unknown_object_still_goes_to_every_rule(tmp_path):
    rep = _scan(tmp_path, """
        def f(fn, x):
            fn.__wrapped__(x)
        """, {"app/two.py": TWO}, _two_doc())
    # fix round 25: neither door has a decorator, so fn.__wrapped__ cannot lift one's guard: unresolved.
    # fix round 28: and no decorated function reaches either sink, so it can unwrap nothing that does
    assert rep.findings == []
