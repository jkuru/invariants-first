"""Fix round 18 (scan review): any reference from scanned code into an excluded module is reported, not
only a direct call (S-12); modules in the directories the scan always skips (build/, dist/, venv/, ...)
are excluded modules too when they are importable under the project's package names (S-13); super()
fails closed when a subclass has a base the scan cannot see (S-14)."""
from __future__ import annotations

import textwrap

import pytest

from invariants import lockfile, scan
from cli_support import write_project

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
HELPER = """
    from app.repo import SinkRepo
    def write(x):
        SinkRepo().save(x)
    def deco(f):
        SinkRepo().save(1)
        return f
    class H:
        def go(self, x):
            SinkRepo().save(x)
    """
TESTING = {"app/testing/__init__.py": "", "app/testing/helpers.py": HELPER}


def _doc():
    door = "py:app.svc.door"
    return {"targets": {door: {"effects": "external"}}, "rules": [{
        "id": "T-1", "status": "candidate", "subject": {"target": door}, "bad": "false",
        "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
        "mediation": {"sink": "py:app.repo.SinkRepo.save", "door": door}}]}


def _scan(tmp_path, ops, extra=None, exclude=lockfile.DEFAULT_SCAN_EXCLUDE):
    files = {"app/__init__.py": "", "app/repo.py": REPO, "app/svc.py": SVC,
             "app/ops.py": textwrap.dedent(ops), **{k: textwrap.dedent(v) for k, v in (extra or {}).items()}}
    return scan.scan(_doc(), [write_project(tmp_path, files)], exclude)


def _codes(rep):
    return [f.code for f in rep.findings]


# --- S-12: any reference into an excluded module ----------------------------------------------------------
@pytest.mark.parametrize("ops", [
    "from app.testing.helpers import write\nw = write\ndef handler(x):\n    w(x)\n",
    "import threading\nfrom app.testing.helpers import write\n"
    "def handler(x):\n    threading.Thread(target=write, args=(x,)).start()\n",
    "from app.testing.helpers import write\ndef handler(xs):\n    list(map(write, xs))\n",
    "from app.testing.helpers import deco\n@deco\ndef handler(x):\n    pass\n",
    "from app.testing.helpers import *\ndef handler(x):\n    write(x)\n",
    "from app.testing import helpers\ndef handler(x):\n    getattr(helpers, 'write')(x)\n",
    "from app.testing.helpers import H\nclass K(H):\n    pass\ndef handler(x):\n    K().go(x)\n",
], ids=["alias", "thread-target", "map", "decorator", "star-import", "getattr", "inherited-method"])
def test_any_reference_into_an_excluded_module_is_reported(tmp_path, ops):
    rep = _scan(tmp_path, ops, TESTING)
    assert "unresolved/excluded-callee" in _codes(rep), rep.findings
    assert rep.rule_result("T-1") != "pass"


def test_a_router_in_an_excluded_file_mounted_by_scanned_code_is_reported(tmp_path):
    rep = _scan(tmp_path, """
        from fastapi import FastAPI
        from app import admin_test
        api = FastAPI()
        api.include_router(admin_test.router)
        """, {"app/admin_test.py": """
            from fastapi import APIRouter
            from app.repo import SinkRepo
            router = APIRouter()
            @router.post('/x')
            def route(x: int):
                SinkRepo().save(x)
            """})
    # the import (its module registers a route when imported) and the mounted router
    assert _codes(rep) == ["unresolved/excluded-callee"] * 2, rep.findings
    msgs = sorted(f.message for f in rep.findings)
    assert msgs[0].startswith("imports app.admin_test") and "runs code at import time" in msgs[0]
    assert msgs[1].startswith("refers to app.admin_test.router")


def test_a_reference_inside_a_door_stays_behind_the_door(tmp_path):
    rep = _scan(tmp_path, "", {**TESTING, "app/svc.py": """
        from app.repo import SinkRepo
        from app.testing.helpers import write
        def door(repo: SinkRepo, x):
            cb = write
            repo.save(x)
        """})
    assert rep.findings == [], rep.findings


def test_an_annotation_naming_an_excluded_class_is_not_a_reference(tmp_path):
    rep = _scan(tmp_path, """
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from app.testing.helpers import H
        def handler(x):
            h: H = x
            return h
        """, TESTING)
    assert rep.findings == [], rep.findings


# --- S-13: skipped directories inside the project ----------------------------------------------------------
@pytest.mark.parametrize("ops,extra", [
    ("from app.build.jobs import route\ndef handler(x):\n    route(x)\n",
     {"app/build/jobs.py": "from app.repo import SinkRepo\ndef route(x):\n    SinkRepo().save(x)\n"}),
    ("from app.dist import helpers\ndef handler(x):\n    helpers.write(x)\n",
     {"app/dist/__init__.py": "", "app/dist/helpers.py": HELPER}),
    ("from app.build.jobs import route\nhandlers = [route]\n",
     {"app/build/jobs.py": "def route(x):\n    pass\n"}),
], ids=["build-call", "dist-attr", "build-ref"])
def test_a_reference_into_a_skipped_directory_of_the_project_is_reported(tmp_path, ops, extra):
    rep = _scan(tmp_path, ops, extra)
    assert _codes(rep) == ["unresolved/excluded-callee"], rep.findings
    assert "'build'" in rep.findings[0].message or "'dist'" in rep.findings[0].message


def test_a_skipped_directory_is_reported_with_no_excludes_at_all(tmp_path):
    rep = _scan(tmp_path, "from app.build.jobs import route\ndef handler(x):\n    route(x)\n",
                {"app/build/jobs.py": "def route(x):\n    pass\n"}, exclude=())
    assert _codes(rep) == ["unresolved/excluded-callee"], rep.findings


def test_third_party_code_in_a_virtualenv_inside_the_root_stays_quiet(tmp_path):
    site = "venv/lib/python3.12/site-packages"
    rep = _scan(tmp_path, """
        import requests
        from requests import get
        from fastapi import FastAPI
        def handler(x):
            get(x)
            return requests.post, FastAPI
        """, {f"{site}/requests/__init__.py": "def get(x):\n    pass\ndef post(x):\n    pass\n",
              f"{site}/fastapi/__init__.py": "class FastAPI:\n    pass\n",
              ".venv/lib/python3.12/site-packages/requests/__init__.py": "def get(x):\n    pass\n",
              "build/lib/app/ops.py": "def handler(x):\n    pass\n",
              "node_modules/x/y.py": "def f():\n    pass\n"})
    assert rep.findings == [], rep.findings


def test_iter_sources_still_leaves_skipped_directories_out(tmp_path):
    root = write_project(tmp_path, {"app/__init__.py": "", "app/build/jobs.py": "", "app/x.py": ""})
    assert sorted(n for _, _, n in scan.iter_sources([root])) == ["app", "app.x"]


# --- S-14: super() and bases the scan cannot see ------------------------------------------------------------
MIX = """
from app.repo import Base, SinkRepo
class Mixin(Base):
    def save(self, x):
        super().save(x)
"""


@pytest.mark.parametrize("leaf", [
    "B = SinkRepo\nclass Leaf(Mixin, B):\n    pass\n",
    "def pick():\n    return SinkRepo\nclass Leaf(Mixin, pick()):\n    pass\n",
    "import app.repo as r\nclass Leaf(Mixin, getattr(r, 'SinkRepo')):\n    pass\n",
    "from somelib import Thing\nclass Leaf(Mixin, Thing):\n    pass\n",
], ids=["alias", "call", "getattr", "third-party"])
def test_super_fails_closed_when_a_subclass_has_a_base_the_scan_cannot_see(tmp_path, leaf):
    rep = _scan(tmp_path, MIX + leaf + "def handler(x):\n    Leaf().save(x)\n")
    assert rep.rule_result("T-1") != "pass", rep.findings


def test_a_module_level_alias_base_resolves_to_the_class(tmp_path):
    rep = _scan(tmp_path, MIX + "B = SinkRepo\nclass Leaf(Mixin, B):\n    pass\n"
                "def handler(x):\n    Leaf().save(x)\n")
    assert _codes(rep) == ["bypass"], rep.findings


def test_super_with_only_builtin_and_stdlib_bases_stays_extracted(tmp_path):
    rep = _scan(tmp_path, MIX + "import abc, typing\nclass Leaf(Mixin, abc.ABC, typing.Generic, object):\n"
                "    pass\ndef handler(x):\n    Leaf().save(x)\n")
    assert rep.findings == [], rep.findings


def test_super_fails_closed_when_an_excluded_module_subclasses_the_class(tmp_path):
    rep = _scan(tmp_path, MIX + "def handler(m: Mixin, x):\n    m.save(x)\n", {
        "app/testing/__init__.py": "",
        "app/testing/leaf.py": "from app.ops import Mixin\nfrom app.repo import SinkRepo\n"
                               "class Leaf(Mixin, SinkRepo):\n    pass\n"})
    assert rep.rule_result("T-1") != "pass", rep.findings


def test_an_excluded_subclass_without_super_users_changes_nothing(tmp_path):
    rep = _scan(tmp_path, MIX + "def handler(m: Mixin, x):\n    m.save(x)\n", {
        "app/testing/__init__.py": "",
        "app/testing/leaf.py": "from app.repo import Base\nclass Other(Base):\n    pass\n"})
    assert rep.findings == [], rep.findings


# --- S-15: a module-level import of an excluded module that runs code at import time --------------------
ROUTES = """
    from fastapi import APIRouter
    from app.repo import SinkRepo
    router = APIRouter()
    @router.post('/x')
    def route(x: int):
        SinkRepo().save(x)
    """
QUIET = """
    import dataclasses
    import logging
    import typing
    from app.repo import SinkRepo
    log = logging.getLogger(__name__)
    T = typing.TypeVar("T")
    LIMIT = 10
    @dataclasses.dataclass
    class Row:
        x: int = 0
    def route(x):
        SinkRepo().save(x)
    if __name__ == "__main__":
        route(1)
    """


@pytest.mark.parametrize("ops", ["import app.admin_test\n", "from app import admin_test\n",
                                 "from app.admin_test import route\n"],
                         ids=["import", "from-package", "from-module"])
def test_an_import_of_an_excluded_module_with_import_time_effects_is_reported(tmp_path, ops):
    rep = _scan(tmp_path, ops, {"app/admin_test.py": ROUTES})
    assert _codes(rep) == ["unresolved/excluded-callee"], rep.findings
    assert "imports app.admin_test" in rep.findings[0].message
    assert "runs code at import time" in rep.findings[0].message


def test_a_top_level_call_in_an_excluded_module_is_an_import_time_effect(tmp_path):
    rep = _scan(tmp_path, "import app.admin_test\n", {
        "app/admin_test.py": "from app.repo import SinkRepo\nSinkRepo().save(1)\n"})
    assert _codes(rep) == ["unresolved/excluded-callee"], rep.findings


def test_an_import_of_an_excluded_module_without_import_time_effects_stays_quiet(tmp_path):
    rep = _scan(tmp_path, "import app.admin_test\nfrom app import admin_test as a\n",
                {"app/admin_test.py": QUIET})
    assert rep.findings == [], rep.findings


def test_a_function_level_import_inside_a_door_stays_behind_the_door(tmp_path):
    rep = _scan(tmp_path, "", {"app/admin_test.py": ROUTES, "app/svc.py": """
        from app.repo import SinkRepo
        def door(repo: SinkRepo, x):
            import app.admin_test
            repo.save(x)
        """})
    assert rep.findings == [], rep.findings
