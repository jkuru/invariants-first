"""Fix round 19 (scan review): each test failed before its fix.

1. A getattr of the sink inside an allowed back door (`getattr(repo, "save")(x)`, or on a module or
   class) is an edge: a new caller that routes through the allowed function is a bypass, as for a
   direct call. A getattr with a computed name there is still reported.
2. Receivers the scan now types: `cls()` in a @classmethod, fluent chains through return annotations
   (`Repo().with_x().save()`, `repo.with_x().save()`, `-> Self`), `Union[Repo, None]`, `with svc.cm()`
   on a local or parameter receiver, attributes and @property getters inherited from a scanned base.
   What it still cannot type is unresolved, and `operator.attrgetter(name)` / `methodcaller(name)`
   with a computed name is `unresolved/computed-attribute` (they passed silently).
"""
from __future__ import annotations

import textwrap

import pytest

import invariants_scan as scan
from scan_support import DEFAULT_EXCLUDE, write_project

REPO = """
    import contextlib
    import typing
    from typing import Iterator, Self

    class Base:
        def save(self, x):
            pass

    class SinkRepo(Base):
        def with_x(self) -> "SinkRepo":
            return self

        def fluent(self) -> Self:
            return self

        def bare(self):
            return self

        def save(self, x):
            pass

    class Other:
        def save(self, x):
            pass

    class Svc:
        @contextlib.asynccontextmanager
        async def cm(self) -> typing.AsyncIterator[SinkRepo]:
            yield SinkRepo()

        @contextlib.contextmanager
        def scm(self) -> Iterator[SinkRepo]:
            yield SinkRepo()

        @contextlib.contextmanager
        def plain(self):
            yield SinkRepo()
    """
SVC = """
    from app.repo import SinkRepo

    def door(repo: SinkRepo, x):
        repo.save(x)
    """


def _doc(allow=None):
    door = "py:app.svc.door"
    med = {"sink": "py:app.repo.SinkRepo.save", "door": door}
    if allow:
        med["allow"] = allow
    return {"targets": {door: {"effects": "external"}}, "rules": [{
        "id": "T-1", "status": "candidate", "subject": {"target": door}, "bad": "false",
        "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
        "mediation": med}]}


def _scan(tmp_path, ops, allow=None):
    files = {"app/__init__.py": "", "app/repo.py": textwrap.dedent(REPO), "app/svc.py": textwrap.dedent(SVC),
             "app/ops.py": textwrap.dedent(ops)}
    return scan.scan(_doc(allow), [write_project(tmp_path, files)], DEFAULT_EXCLUDE)


def _codes(rep):
    return [f.code for f in rep.findings]


# --- 1. getattr of the sink inside an allowed back door --------------------------------------------------
@pytest.mark.parametrize("call", ['getattr(repo, "save")(x)', 'getattr(SinkRepo(), "save")(x)',
                                  'getattr(SinkRepo, "save")(repo, x)'], ids=["param", "constructed", "class"])
def test_a_new_caller_through_an_allowed_getattr_of_the_sink_is_a_bypass(tmp_path, call):
    rep = _scan(tmp_path, f"""
        from app.repo import SinkRepo
        def backfill(repo: SinkRepo, x):
            {call}
        def handler(x):
            backfill(SinkRepo(), x)
        """, ["py:app.ops.backfill"])
    assert _codes(rep) == ["bypass"], rep.findings
    f = rep.findings[0]
    assert f.path == ["app.ops.handler", "app.ops.backfill", "app.repo.SinkRepo.save"] and f.line == 4
    assert rep.rule_result("T-1") == "fail"


def test_an_allowed_getattr_of_the_sink_alone_stays_frozen(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import SinkRepo
        def backfill(repo: SinkRepo, x):
            getattr(repo, "save")(x)
        """, ["py:app.ops.backfill"])
    assert rep.findings == [] and rep.rule_result("T-1") == "pass"


def test_a_new_caller_through_an_allowed_getattr_on_an_unknown_receiver_is_unresolved(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import SinkRepo
        def backfill(repo, x):
            getattr(repo, "save")(x)
        def handler(x):
            backfill(SinkRepo(), x)
        """, ["py:app.ops.backfill"])
    assert _codes(rep) == ["unresolved/inferred-path"], rep.findings
    assert rep.rule_result("T-1") == "unresolved"


def test_a_computed_getattr_inside_an_allowed_back_door_is_still_reported(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import SinkRepo
        def backfill(repo: SinkRepo, name, x):
            repo.save(x)
            getattr(repo, name)(x)
        """, ["py:app.ops.backfill"])
    assert _codes(rep) == ["unresolved/computed-attribute"], rep.findings


def test_a_getattr_of_a_helper_on_a_typed_receiver_is_followed(tmp_path):
    files = {"app/__init__.py": "", "app/repo.py": textwrap.dedent(REPO), "app/ops.py": textwrap.dedent("""
        from app.repo import SinkRepo
        class H:
            def go(self, repo: SinkRepo, x):
                repo.save(x)
        def door(h: H, x):
            h.go(SinkRepo(), x)
        def handler(h: H, x):
            getattr(h, "go")(SinkRepo(), x)
        """)}
    doc = _doc()
    doc["rules"][0]["mediation"]["door"] = "py:app.ops.door"
    rep = scan.scan(doc, [write_project(tmp_path, files)], DEFAULT_EXCLUDE)
    assert _codes(rep) == ["bypass"], rep.findings
    assert rep.findings[0].path == ["app.ops.handler", "app.ops.H.go", "app.repo.SinkRepo.save"]


# --- 2. receivers the scan now types ---------------------------------------------------------------------
TYPED = {
    "classmethod-cls()": """
        from app.repo import SinkRepo
        class R(SinkRepo):
            @classmethod
            def make(cls, x):
                cls().save(x)
        """,
    "classmethod-local": """
        from app.repo import SinkRepo
        class R(SinkRepo):
            @classmethod
            def make(cls, x):
                y = cls()
                y.save(x)
        """,
    "fluent-constructed": "from app.repo import SinkRepo\ndef h(x):\n    SinkRepo().with_x().save(x)\n",
    "fluent-local": "from app.repo import SinkRepo\ndef h(x):\n    repo = SinkRepo()\n    repo.with_x().save(x)\n",
    "fluent-param": "from app.repo import SinkRepo\ndef h(repo: SinkRepo, x):\n    repo.with_x().save(x)\n",
    "fluent-self-type": "from app.repo import SinkRepo\ndef h(repo: SinkRepo, x):\n    repo.fluent().fluent().save(x)\n",
    "union-none": "import typing\nfrom app.repo import SinkRepo\ndef h(repo: typing.Union[SinkRepo, None], x):\n"
                  "    repo.save(x)\n",
    "union-none-first": "from typing import Union\nfrom app.repo import SinkRepo\n"
                        "def h(repo: Union[None, SinkRepo], x):\n    repo.save(x)\n",
    "async-with-local": """
        from app.repo import Svc
        async def h(x):
            svc = Svc()
            async with svc.cm() as r:
                r.save(x)
        """,
    "with-param": """
        from app.repo import Svc
        def h(svc: Svc, x):
            with svc.scm() as r:
                r.save(x)
        """,
    "inherited-property": """
        from app.repo import SinkRepo
        class P:
            @property
            def repo(self) -> SinkRepo:
                return SinkRepo()
        class C(P):
            def go(self, x):
                self.repo.save(x)
        """,
    "inherited-attribute": """
        from app.repo import SinkRepo
        class P:
            def __init__(self):
                self.repo = SinkRepo()
        class C(P):
            pass
        def h(c: C, x):
            c.repo.save(x)
        """,
}


@pytest.mark.parametrize("ops", list(TYPED.values()), ids=list(TYPED))
def test_receivers_the_scan_now_types_are_extracted_edges(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert _codes(rep) == ["bypass"], rep.findings
    assert rep.findings[0].path[-1] == "app.repo.SinkRepo.save"


UNTYPED = {
    "union-of-two": "from typing import Union\nfrom app.repo import SinkRepo, Other\n"
                    "def h(repo: Union[SinkRepo, Other], x):\n    repo.save(x)\n",
    "fluent-unannotated": "from app.repo import SinkRepo\ndef h(x):\n    SinkRepo().bare().save(x)\n",
    "with-unannotated-cm": "from app.repo import Svc\ndef h(svc: Svc, x):\n    with svc.plain() as r:\n"
                           "        r.save(x)\n",
    "with-unknown-receiver": "def h(svc, x):\n    with svc.scm() as r:\n        r.save(x)\n",
    "cls-as-instance-too": """
        from app.repo import SinkRepo
        class R(SinkRepo):
            @classmethod
            def make(cls, x):
                cls().save(x)
            def odd(cls, x):
                cls().save(x)
        """,
    "inherited-attribute-rebound": """
        from app.repo import SinkRepo, Other
        class P:
            def __init__(self):
                self.repo = SinkRepo()
        class C(P):
            def reset(self):
                self.repo = Other()
        def h(c: C, x):
            c.repo.save(x)
        """,
}


@pytest.mark.parametrize("ops", list(UNTYPED.values()), ids=list(UNTYPED))
def test_receivers_the_scan_cannot_type_stay_unresolved(tmp_path, ops):
    rep = _scan(tmp_path, ops)
    assert rep.rule_result("T-1") in ("unresolved", "fail"), rep.findings
    assert any(c.startswith("unresolved/") for c in _codes(rep)), rep.findings


@pytest.mark.parametrize("call", ["operator.attrgetter(name)(SinkRepo())(x)",
                                  "operator.methodcaller(name, x)(SinkRepo())",
                                  'operator.attrgetter("label", name)(SinkRepo())',
                                  "operator.attrgetter(*names)(SinkRepo())"],
                         ids=["attrgetter", "methodcaller", "attrgetter-second", "attrgetter-star"])
def test_a_computed_attrgetter_or_methodcaller_is_unresolved(tmp_path, call):
    rep = _scan(tmp_path, f"""
        import operator
        from app.repo import SinkRepo
        def h(name, names, x):
            {call}
        """)
    assert _codes(rep) == ["unresolved/computed-attribute"], rep.findings
    assert "operator." in rep.findings[0].message


@pytest.mark.parametrize("call", ['operator.attrgetter("repo.save")(ctx)(x)',
                                  'operator.attrgetter("label", "save")(SinkRepo())',
                                  'operator.methodcaller("save", x)(SinkRepo())'],
                         ids=["dotted", "second-name", "methodcaller"])
def test_an_attrgetter_naming_the_sink_anywhere_is_forbidden(tmp_path, call):
    rep = _scan(tmp_path, f"""
        import operator
        from app.repo import SinkRepo
        def h(ctx, x):
            {call}
        """)
    assert _codes(rep) == ["forbidden/getattr-sink"], rep.findings


def test_a_literal_attrgetter_of_another_name_stays_quiet(tmp_path):
    rep = _scan(tmp_path, """
        import operator
        def h(rows):
            return sorted(rows, key=operator.attrgetter("created_at", "id"))
        """)
    assert rep.findings == []
