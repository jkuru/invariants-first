"""Fix round 16 (scan review): a sink used as a value through a typed receiver escapes (S-1); generic
bases and super() (S-2); receiver typing for await, with, Annotated and @property (S-3); builtin
containers are not unknown receivers (S-4); reflection through aliases, sys.modules and rebinding
(S-5); one bypass finding per call site (S-6); `inv init` comments out a rule it cannot freeze
(S-7); truthiness preconditions translate by the parameter's type (S-8)."""
from __future__ import annotations

import ast
import textwrap

import pytest

from invariants import bootstrap, doors, scan
from cli_support import write_project

REPO = textwrap.dedent("""
    class Repo:
        def save(self, x):
            pass
        def load(self, x):
            return x
    """)
SVC = textwrap.dedent("""
    from app.repo import Repo

    def door(repo: Repo, x):
        repo.save(x)
    """)


def _doc(sink="py:app.repo.Repo.save", door="py:app.svc.door"):
    return {"targets": {door: {"effects": "external"}},
            "rules": [{"id": "T-1", "status": "candidate", "subject": {"target": door},
                       "bad": "false", "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
                       "mediation": {"sink": sink, "door": door}}]}


def _scan(tmp_path, ops, extra=None, doc=None):
    files = {"app/__init__.py": "", "app/repo.py": REPO, "app/svc.py": SVC,
             "app/ops.py": textwrap.dedent(ops), **(extra or {})}
    return scan.scan(doc or _doc(), [write_project(tmp_path, files)])


def _codes(tmp_path, ops, extra=None, doc=None):
    return sorted(f.code for f in _scan(tmp_path, ops, extra, doc).findings)


# --- S-1: a sink used as a value through a typed receiver ----------------------------------------------
@pytest.mark.parametrize("ops", [
    """
    from app.repo import Repo
    def f(repo: Repo, x):
        cb = repo.save
        cb(x)
    """,
    """
    from app.repo import Repo
    def f(x):
        repo = Repo()
        cb = repo.save
        cb(x)
    """,
    """
    from app.repo import Repo
    class Job:
        def __init__(self):
            self.repo = Repo()
        def run(self, x, add_task):
            add_task(self.repo.save, x)
    """,
    """
    from app.repo import Repo
    def f(repo: Repo, add_task):
        add_task(repo.save)
    """,
])
def test_a_sink_taken_as_a_value_from_a_typed_receiver_escapes(tmp_path, ops):
    assert "unresolved/sink-escapes" in _codes(tmp_path, ops)


def test_a_sink_called_through_a_typed_receiver_stays_an_edge(tmp_path):
    codes = _codes(tmp_path, """
        from app.repo import Repo
        def f(repo: Repo, x):
            repo.save(x)
            Repo().save(x)
        def g(x):
            return Repo().load(x)
        """)
    assert "unresolved/sink-escapes" not in codes and "bypass" in codes


def test_another_method_as_a_value_is_not_the_sink(tmp_path):
    assert _codes(tmp_path, """
        from app.repo import Repo
        def f(repo: Repo, add_task):
            add_task(repo.load)
        """) == []


# --- S-2: generic bases and super() ----------------------------------------------------------------------
GENERIC = textwrap.dedent("""
    from typing import Generic, TypeVar
    T = TypeVar("T")

    class BaseRepo(Generic[T]):
        def save(self, x: T) -> None:
            pass

    class ConcreteRepo(BaseRepo[int]):
        def save(self, x: int) -> None:
            pass
    """)


def test_a_call_through_a_generic_base_reaches_the_override(tmp_path):
    doc = _doc(sink="py:app.generic.ConcreteRepo.save", door="py:app.svc.door")
    rep = _scan(tmp_path, """
        from app.generic import BaseRepo
        def f(repo: BaseRepo[int], x):
            repo.save(x)
        def g(repo: BaseRepo, x):
            repo.save(x)
        """, {"app/generic.py": GENERIC}, doc)
    assert [f.owner for f in rep.findings if f.kind == "bypass"] == ["app.ops.f", "app.ops.g"]


def test_an_inherited_method_on_a_generic_subclass_resolves(tmp_path):
    doc = _doc(sink="py:app.generic.BaseRepo.save", door="py:app.svc.door")
    rep = _scan(tmp_path, """
        from app.generic import Child
        def f(x):
            Child().save(x)
        """, {"app/generic.py": GENERIC + "\nclass Child(BaseRepo[str]):\n    pass\n"}, doc)
    assert [f.code for f in rep.findings] == ["bypass"]


def test_super_resolves_to_the_base_method(tmp_path):
    doc = _doc(sink="py:app.generic.BaseRepo.save", door="py:app.svc.door")
    rep = _scan(tmp_path, """
        from app.generic import BaseRepo
        class Mine(BaseRepo[int]):
            def save(self, x):
                super().save(x)
        def f(x):
            Mine().save(x)
        """, {"app/generic.py": GENERIC}, doc)
    codes = [f.code for f in rep.findings]
    assert codes == ["bypass"], codes
    assert rep.findings[0].path[-2:] == ["app.ops.Mine.save", "app.generic.BaseRepo.save"]


def test_super_follows_the_mro_through_scanned_classes(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import Repo
        class Mid(Repo):
            pass
        class Leaf(Mid):
            def save(self, x):
                super().save(x)
        def f(x):
            Leaf().save(x)
        """)
    assert [f.code for f in rep.findings] == ["bypass"]


# --- S-3: receiver typing --------------------------------------------------------------------------------
@pytest.mark.parametrize("ops", [
    """
    from app.repo import Repo
    async def get_repo() -> Repo:
        return Repo()
    async def f(x):
        repo = await get_repo()
        repo.save(x)
    async def g(x):
        (await get_repo()).save(x)
    """,
    """
    from app.repo import Repo
    class Session:
        def __enter__(self) -> Repo:
            return Repo()
        def __exit__(self, *a):
            pass
    def f(x):
        with Session() as repo:
            repo.save(x)
    """,
    """
    from app.repo import Repo
    class Session:
        async def __aenter__(self) -> Repo:
            return Repo()
        async def __aexit__(self, *a):
            pass
    async def f(x):
        async with Session() as repo:
            repo.save(x)
    """,
    """
    import contextlib
    from typing import Iterator
    from app.repo import Repo
    @contextlib.contextmanager
    def session() -> Iterator[Repo]:
        yield Repo()
    def f(x):
        with session() as repo:
            repo.save(x)
    """,
    """
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator
    from app.repo import Repo
    @asynccontextmanager
    async def session() -> AsyncIterator[Repo]:
        yield Repo()
    async def f(x):
        async with session() as repo:
            repo.save(x)
    """,
    """
    from typing import Annotated
    from app.repo import Repo
    def Depends(f):
        return f
    def get_repo() -> Repo:
        return Repo()
    def f(repo: Annotated[Repo, Depends(get_repo)], x):
        repo.save(x)
    """,
    """
    from typing import Annotated
    from app.repo import Repo
    RepoDep = Annotated[Repo, "dependency"]
    def f(repo: RepoDep, x):
        repo.save(x)
    """,
    """
    from app.deps import RepoDep
    def f(repo: RepoDep, x):
        repo.save(x)
    """,
    """
    from app.repo import Repo
    class Service:
        @property
        def repo(self) -> Repo:
            return Repo()
        def run(self, x):
            self.repo.save(x)
    """,
])
def test_receivers_typed_through_await_with_annotated_and_property(tmp_path, ops):
    extra = {"app/deps.py": "import typing\nfrom app.repo import Repo\nRepoDep = typing.Annotated[Repo, 1]\n"}
    codes = _codes(tmp_path, ops, extra)
    assert "bypass" in codes and "unresolved/unknown-receiver" not in codes, codes


def test_a_with_target_the_scan_cannot_type_stays_unresolved(tmp_path):
    codes = _codes(tmp_path, """
        import contextlib
        @contextlib.contextmanager
        def session():
            yield object()
        def f(x):
            with session() as repo:
                repo.save(x)
        """)
    assert "unresolved/unknown-receiver" in codes and "bypass" not in codes


# --- S-4: builtin containers ---------------------------------------------------------------------------
BUILTIN_SINKS = textwrap.dedent("""
    class Repo:
        def update(self, x):
            pass
        def add(self, x):
            pass
    """)


@pytest.mark.parametrize("ops", [
    """
    def f(x):
        d = {}
        d.update(x)
        s = set()
        s.add(x)
        {}.update(x)
        [1].copy()
        {k: 1 for k in x}.update(x)
        t = dict(x)
        t.update(x)
    """,
    """
    def f(d: dict[str, int], s: set[str], x):
        d.update(x)
        s.add(x)
    """,
    """
    class C:
        def __init__(self):
            self.seen = set()
        def f(self, x):
            self.seen.add(x)
    """,
])
def test_builtin_containers_are_not_unknown_receivers(tmp_path, ops):
    doc = _doc(sink=["py:app.repo.Repo.update", "py:app.repo.Repo.add"], door="py:app.svc.door")
    svc = "from app.repo import Repo\n\ndef door(repo: Repo, x):\n    repo.update(x)\n    repo.add(x)\n"
    assert _codes(tmp_path, ops, {"app/repo.py": BUILTIN_SINKS, "app/svc.py": svc}, doc) == []


def test_an_unknown_receiver_and_a_dict_subclass_stay_fail_closed(tmp_path):
    doc = _doc(sink=["py:app.repo.Repo.update", "py:app.repo.Repo.add"], door="py:app.svc.door")
    svc = "from app.repo import Repo\n\ndef door(repo: Repo, x):\n    repo.update(x)\n    repo.add(x)\n"
    codes = _codes(tmp_path, """
        from app.repo import Repo
        class Mine(dict):
            def update(self, x):
                Repo().update(x)
        def f(d: dict, x, other):
            d.update(x)
            other.add(x)
        """, {"app/repo.py": BUILTIN_SINKS, "app/svc.py": svc}, doc)
    assert codes.count("unresolved/unknown-receiver") == 2


# --- S-5: reflection -----------------------------------------------------------------------------------
@pytest.mark.parametrize("ops,want", [
    ("from app.repo import Repo\ng = getattr\ndef f(r: Repo, n):\n    g(r, n)()\n", "unresolved/reflection"),
    ("from app.repo import Repo\ndef f(r: Repo, n):\n    g = getattr\n    g(r, n)()\n", "unresolved/reflection"),
    ("from builtins import getattr as g\nfrom app.repo import Repo\ndef f(r: Repo, n):\n    g(r, n)()\n",
     "unresolved/computed-attribute"),
    ("from builtins import getattr as g\nfrom app.repo import Repo\ndef f(r: Repo):\n    g(r, 'save')(1)\n",
     "forbidden/getattr-sink"),
    ("import sys\ndef f(x):\n    sys.modules['app.repo'].Repo().save(x)\n", "unresolved/reflection"),
    ("import sys\ndef f(x):\n    sys.modules.get('app.repo').Repo().save(x)\n", "unresolved/reflection"),
    ("import sys\ndef f(fake):\n    sys.modules['app.repo'] = fake\n", "unresolved/reflection"),
    ("from app import repo\ndef f(x):\n    repo.__dict__['Repo']().save(x)\n", "unresolved/unknown-receiver"),
    ("from app.repo import Repo\ndef f(r: Repo, x):\n    type(r).__dict__['save'](r, x)\n", "forbidden/getattr-sink"),
    ("from app.repo import Repo\ndef f(r: Repo, x):\n    r.__class__.__dict__.get('save')(r, x)\n",
     "forbidden/getattr-sink"),
    ("from app.repo import Repo\ndef f(r: Repo, k, x):\n    type(r).__dict__[k](r, x)\n", "unresolved/reflection"),
    ("from app.repo import Repo\ndef f(x):\n    Repo.__dict__['save'](Repo(), x)\n", "forbidden/getattr-sink"),
    ("import app.repo\ndef f(fake):\n    app.repo.Repo = fake\n", "forbidden/monkeypatch"),
    ("from app import repo\ndef f(fake):\n    repo.Repo = fake\n", "forbidden/monkeypatch"),
    ("from app.repo import Repo\ndef f(other):\n    Repo.save = other\n", "forbidden/monkeypatch"),
    ("from app.repo import Repo\ndef f(other):\n    setattr(Repo, 'save', other)\n", "forbidden/monkeypatch"),
    ("from app import repo\ndef f(fake):\n    setattr(repo, 'Repo', fake)\n", "forbidden/monkeypatch"),
    ("import app\ndef f(fake):\n    app.repo = fake\n", "forbidden/monkeypatch"),
])
def test_reflection_aliases_sys_modules_and_rebinding_are_reported(tmp_path, ops, want):
    assert want in _codes(tmp_path, ops)


def test_harmless_uses_stay_quiet(tmp_path):
    assert _codes(tmp_path, """
        import sys
        from app.repo import Repo
        def f(x):
            if "app.repo" in sys.modules:
                return getattr(x, "name", None)
            Repo.label = "repo"
        """) == []


# --- S-6: one finding per call site --------------------------------------------------------------------
def test_each_bypass_site_is_its_own_finding(tmp_path):
    rep = _scan(tmp_path, """
        from app.repo import Repo
        def f(repo: Repo, x):
            repo.save(x)
            if x:
                repo.save(x + 1)
        def helper(repo: Repo, x):
            repo.save(x)
        def a(repo: Repo):
            helper(repo, 1)
        def b(repo: Repo):
            helper(repo, 2)
        """)
    lines = [l for l in scan.to_text(rep).splitlines() if l.startswith("bypass")]
    assert [l.split()[2] for l in lines] == ["app/ops.py:4:5", "app/ops.py:6:9", "app/ops.py:8:5"]
    assert lines[0].startswith("bypass     T-1      app/ops.py:4:5  app.ops.f -> app.repo.Repo.save")
    assert rep.rule_result("T-1") == "fail"


def test_allowed_back_doors_on_every_path_stay_in_use(tmp_path):
    doc = _doc()
    doc["rules"][0]["mediation"]["allow"] = ["py:app.ops.a", "py:app.ops.b", "py:app.ops.helper"]
    rep = _scan(tmp_path, """
        from app.repo import Repo
        def helper(repo: Repo, x):
            repo.save(x)
        def a(repo: Repo):
            helper(repo, 1)
        def b(repo: Repo):
            helper(repo, 2)
        """, doc=doc)
    assert rep.findings == [] and rep.rule_result("T-1") == "pass"


# --- S-7: `inv init` cannot freeze a back door owned by the door or sink -------------------------------
def test_a_rule_whose_findings_cannot_be_frozen_is_written_commented_out(tmp_path):
    root = write_project(tmp_path, {
        "app/__init__.py": "",
        "app/repo.py": "class Repo:\n    def save(self, x):\n        pass\n",
        "app/service.py": textwrap.dedent("""
            from app.repo import Repo
            def door(repo: Repo, x):
                repo.save(x)
                getattr(repo, "save")(x)
            """),
        "app/api.py": "from app.service import door\nfrom app.repo import Repo\n\n"
                      "def one(x):\n    door(Repo(), x)\n\ndef two(x):\n    door(Repo(), x)\n",
    })
    g = scan.graph([root])
    d = doors.Door("T-9", "app.repo", ["app.service.door"], ["app.repo.Repo.save"], ["app.api.one", "app.api.two"])
    report = scan.scan({"rules": [{"id": "T-9", "status": "candidate", "subject": {"target": "py:app.service.door"},
                                   "bad": "false", "mediation": {"sink": "py:app.repo.Repo.save",
                                                                 "door": "py:app.service.door"}}]}, [root], g=g)
    assert report.rule_result("T-9") != "pass"           # the door itself hides a sink call
    doors._freeze(d, g, [root], [])
    assert not d.active and d.why_inactive
    text = "\n".join(doors.render(doors.DoorsFound([d], [], {}, 3)))
    assert "# not written as a rule:" in text and "\n  - id: T-9" not in text


def test_init_freezes_a_back_door_reached_from_many_entrypoints(tmp_path):
    callers = "".join(f"def e{i}(x):\n    helper(Repo(), x)\n\n" for i in range(8))
    root = write_project(tmp_path, {
        "app/__init__.py": "",
        "app/repo.py": "class Repo:\n    def save(self, x):\n        pass\n",
        "app/service.py": "from app.repo import Repo\n\ndef door(repo: Repo, x):\n    repo.save(x)\n",
        "app/admin.py": "from app.repo import Repo\n\ndef helper(repo: Repo, x):\n    repo.save(x)\n\n" + callers,
    })
    g = scan.graph([root])
    d = doors.Door("T-9", "app.repo", ["app.service.door"], ["app.repo.Repo.save"], [])
    doors._freeze(d, g, [root], [])
    assert d.active and len(d.back_doors) == 1             # one call site, frozen with all eight callers
    assert d.allow == sorted(["app.admin.helper", *(f"app.admin.e{i}" for i in range(8))])


# --- S-8: truthiness preconditions -----------------------------------------------------------------------
def _guard(src: str):
    tree = ast.parse(textwrap.dedent(src))
    return bootstrap._guards("m", tree)[0]


@pytest.mark.parametrize("sig,cond,cel", [
    ("name: str", "not name", "size(args.name) == 0"),
    ("data: bytes", "not data", "size(args.data) == 0"),
    ("items: list[int]", "not items", "size(args.items) == 0"),
    ("m: dict[str, int]", "not m", "size(args.m) == 0"),
    ("tags: set[str]", "not tags", "size(args.tags) == 0"),
    ("ok: bool", "not ok", "!args.ok"),
    ("name: str", "name", "size(args.name) != 0"),
    ("name: Optional[str]", "not name", "args.name == null || size(args.name) == 0"),
    ("name: str | None", "not name", "args.name == null || size(args.name) == 0"),
    ("ok: Optional[bool]", "not ok", "args.ok == null || !args.ok"),
    ("name: str, n: int", "not name or n > 3", "size(args.name) == 0 || args.n > 3"),
    ("name: str, n: int", "n > 3 and not name", "args.n > 3 && size(args.name) == 0"),
])
def test_truthiness_translates_by_annotation(sig, cond, cel):
    g = _guard(f"from typing import Optional\ndef f({sig}):\n    if {cond}:\n        raise ValueError('x')\n")
    assert g.clauses == [(cel, "x")] and g.skipped == []


@pytest.mark.parametrize("sig,cond", [
    ("name", "not name"),
    ("n: int", "not n"),
    ("x: float", "not x"),
    ("o: Optional[int]", "not o"),
    ("name: str", "not len(name)"),
    ("name: str", "not name.strip()"),
])
def test_truthiness_without_an_exact_form_is_not_translated(sig, cond):
    g = _guard(f"from typing import Optional\ndef f({sig}):\n    if {cond}:\n        raise ValueError('x')\n")
    assert g.clauses == [] and len(g.skipped) == 1


def test_an_assert_on_a_string_translates_by_annotation():
    g = _guard("def f(name: str):\n    assert name, 'name needed'\n")
    assert g.clauses == [("size(args.name) == 0", "name needed")]
