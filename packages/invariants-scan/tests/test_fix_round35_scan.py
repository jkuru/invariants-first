"""Round 35 (external audit, scan).

H1. InferredBasis cached a name's callees' sites by id() of the callees tuple; a single inferred edge's
    tuple was made fresh on each read and freed, so a later edge could get a freed tuple's id and its
    cached sites: reached_by (and the sha) of an inferred-path finding listed calls that never reach it.
H2. A file in an always-skipped directory (.venv/, build/) named modules for the whole index: pytest's
    `_pytest/pathlib.py` in a virtualenv made `pathlib` a file under the roots, so `pathlib.Path(x)` lost its
    type and its `.write_text()` was name-matched to a scanned method.
H3. door_only (code reached only through a door) counted functions taken as values by name ("ref") as
    escaped, not `.m` taken as a value on an untyped receiver ("attrref"): `f = obj.run` let Helper.run's
    own sites (a call into excluded code) go unreported.
H4. `o.__dict__["save"] = v` (and `del`, `.update(save=v)`, `.update({"save": v})`, `.setdefault`) on an
    instance replaced the sink with no finding, where `o.save = v` is forbidden/monkeypatch; and
    setattr/delattr of a name another module imports the sink or door under (`setattr(helper, "pay_back",
    v)`) was not one, where `helper.pay_back = v` is.
M1. super(Cls, other) in a method was resolved through the method's own class, not other's.
M2. A singleton imported from another module (`from lib import s`) and bound again in the importing module
    (`s = Repo()`, or `global s` in a function) kept the other module's type.
M3. _Subversions resolved names with no locals: a parameter named as a module class (`def patch(Other):
    Other.__bases__ = ...`) was taken for that class, not for any class.
M4. A class-body alias (`save = quiet`) beat a later `def save(self)` in the same class body.
M5. mediations() crashed (AttributeError, TypeError) on a subject that is not a mapping and on rules that
    are not a list; it raises ScanConfigError.
"""
from __future__ import annotations

import pathlib
import textwrap

import pytest

import invariants_scan as scan
from invariants_scan import _index
from invariants_scan.accept import InferredBasis
from invariants_scan.contracts import Finding, _Edge, _Fan

SINK = "def send_to_gateway(c, a):\n    pass\n"
RULE = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway", "door": ["py:payments.refund"]}}]}
SAVE = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.Repo.save", "door": ["py:payments.refund"]}}]}


def write(tmp_path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for k, v in files.items():
        p = tmp_path / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(v))
    return tmp_path


def run(tmp_path: pathlib.Path, files: dict[str, str], rule: dict = RULE, exclude: tuple[str, ...] = ()
        ) -> tuple[str, set[str]]:
    rep = scan.scan(rule, [write(tmp_path, files)], exclude)
    return rep.rule_result("R"), {f.code for f in rep.findings}


def calls_of(tmp_path: pathlib.Path, files: dict[str, str], caller: str) -> list[tuple[str, ...]]:
    g = scan.graph([write(tmp_path, files)], [])
    return [c.callees if isinstance(c, _Fan) else (c.callee,) for c in g.calls if c.caller == caller]


# ---- H1: a single inferred edge's callees are not cached by a freed tuple's id ----

def test_inferred_basis_lists_only_the_calls_that_reach_the_site():
    n = 40
    succ: dict[str, list[_Edge | _Fan]] = {"m.site": [_Edge("m.site", "m.goal", 1, 0, True)], "m.goal": []}
    starts = []
    for i in range(n):
        c = f"m.c{i:03d}"
        starts.append(c)
        to = "m.site" if i % 3 == 0 else f"m.dead{i:03d}"
        succ[c] = [_Edge(c, to, 1, 0, True)]
        succ.setdefault(to, [])
    f = Finding("unresolved", "R", "unresolved/inferred-path", "", path=["m.c000", "m.site", "m.goal"])
    got = InferredBasis(succ, starts, {"m.goal"}, [f])(f)
    calls = sorted(x for x in got if "->" in x)
    want = sorted([f"m.c{i:03d} -> .site" for i in range(0, n, 3)] + ["m.site -> .goal"])
    assert calls == want
    assert sorted(x for x in got if x.startswith("entrypoint")) == sorted(
        f"entrypoint m.c{i:03d}" for i in range(0, n, 3))


def test_inferred_basis_does_not_key_a_single_edge_by_its_id(monkeypatch):
    # what CPython's free list does by chance (a freed 1-tuple's address given to the next one), every time:
    # each fresh 1-tuple has the same id; a fan's callees tuple stays alive, so its id is its own
    import builtins

    from invariants_scan import accept
    monkeypatch.setattr(accept, "id", lambda o: 0 if isinstance(o, tuple) and len(o) == 1 else builtins.id(o),
                        raising=False)
    succ: dict[str, list[_Edge | _Fan]] = {"m.site": [_Edge("m.site", "m.goal", 1, 0, True)], "m.goal": [],
                                           "m.dead": [], "m.a": [_Edge("m.a", "m.site", 1, 0, True)],
                                           "m.b": [_Edge("m.b", "m.dead", 1, 0, True)],
                                           "m.c": [_Fan("m.c", ("m.dead",), 1, 0)]}
    f = Finding("unresolved", "R", "unresolved/inferred-path", "", path=["m.a", "m.site", "m.goal"])
    got = InferredBasis(succ, ["m.a", "m.b", "m.c"], {"m.goal"}, [f])(f)
    assert sorted(x for x in got if "->" in x) == ["m.a -> .site", "m.site -> .goal"]


def test_inferred_basis_shares_a_fans_callees():
    fan = ("m.dead", "m.site")
    succ: dict[str, list[_Edge | _Fan]] = {"m.site": [_Edge("m.site", "m.goal", 1, 0, True)], "m.goal": [],
                                           "m.dead": [], "m.a": [_Fan("m.a", fan, 1, 0)],
                                           "m.b": [_Fan("m.b", fan, 2, 0)], "m.c": [_Fan("m.c", ("m.dead",), 3, 0)]}
    f = Finding("unresolved", "R", "unresolved/inferred-path", "", path=["m.a", "m.site", "m.goal"])
    got = InferredBasis(succ, ["m.a", "m.b", "m.c"], {"m.goal"}, [f])(f)
    assert sorted(x for x in got if "->" in x) == ["m.a -> .dead", "m.b -> .dead", "m.site -> .goal"]


# ---- H2: a skipped directory's files name nothing the code imports ----

PATHLIB = {"store.py": "class Repo:\n    def write_text(self, t):\n        pass\ndef save(t):\n    Repo().write_text(t)\n",
           "app.py": "import pathlib\ndef go():\n    p = pathlib.Path('x')\n    p.write_text('y')\n"}
WRITE = {"rules": [{"id": "R", "mediation": {"sink": "py:store.Repo.write_text", "door": ["py:store.save"]}}]}


@pytest.mark.parametrize("skipped", [".venv/lib/python3.12/site-packages/_pytest/pathlib.py",
                                     "build/lib/pathlib.py", "venv/lib/python3.12/site-packages/pathlib/__init__.py"])
def test_a_skipped_directory_does_not_name_a_standard_module(tmp_path, skipped):
    assert run(tmp_path, {**PATHLIB, skipped: "def x():\n    pass\n"}, WRITE) == ("pass", set())
    idx = _index([tmp_path], [])
    assert idx.outside("pathlib.Path")
    assert not any(n.startswith((".venv", "build", "venv")) for n in idx.files_named)


def test_a_skipped_file_gives_no_alias(tmp_path):
    # venv/build/x.py, skipped from both roots: named build.x from venv/, and venv.build.x from the top
    write(tmp_path, {"app.py": "", "venv/build/x.py": ""})
    idx = _index([tmp_path, tmp_path / "venv"], [])
    assert idx.module_alias == {}


def test_a_skipped_directory_under_a_project_package_still_names_its_modules(tmp_path):
    # app/build/jobs.py is importable as app.build.jobs: not a scanned module, but a file under the roots
    write(tmp_path, {"app/__init__.py": "", "app/ops.py": "import app.build.jobs\n", "app/build/jobs.py": ""})
    idx = _index([tmp_path], [])
    assert "app.build.jobs" in idx.files_named and "app.build" in idx.package_names
    assert not idx.outside("app.build.jobs.run")
    rep = scan.scan(RULE, [tmp_path])
    assert "unresolved/unknown-module" not in {f.code for f in rep.findings}


# ---- H3: a method taken as a value on an untyped receiver is not reached only through a door ----

DOOR_ONLY = {"payments.py": "import legacy\n" + SINK + "class Helper:\n    def run(self):\n        legacy.do()\n"
                            "def refund():\n    Helper().run()\n",
             "legacy.py": "def do():\n    pass\n"}


@pytest.mark.parametrize("use", ["obj.run()", "f = obj.run\n    f()", "import threading\n    threading.Thread(target=obj.run).start()"])
def test_a_method_taken_as_a_value_keeps_its_own_sites(tmp_path, use):
    files = {**DOOR_ONLY, "ops.py": f"def go(obj):\n    {use}\n"}
    assert run(tmp_path, files, exclude=("legacy.py",)) == ("unresolved", {"unresolved/excluded-callee"})


def test_a_helper_reached_only_through_the_door_stays_quiet(tmp_path):
    files = {**DOOR_ONLY, "ops.py": "def go(obj):\n    f = obj.other\n    f()\n"}
    assert run(tmp_path, files, exclude=("legacy.py",)) == ("pass", set())


# ---- H4: literal __dict__ writes, and setattr through another module's name for the door ----

REPO = (SINK + "class Repo:\n    def save(self):\n        pass\n"
        "def refund():\n    send_to_gateway(1, 2)\n    Repo().save()\n")


@pytest.mark.parametrize("write_", [
    "r.__dict__['save'] = print", "r.__dict__.update(save=print)", "r.__dict__.update({'save': print})",
    "r.__dict__.setdefault('save', print)", "r.__dict__.__setitem__('save', print)", "del r.__dict__['save']",
    "object.__setattr__(r, 'save', print)", "r.__setattr__('save', print)", "r.__delattr__('save')",
    "object.__delattr__(r, 'save')",
])
def test_a_literal_dict_write_over_the_sink_is_a_monkeypatch(tmp_path, write_):
    files = {"payments.py": REPO, "ops.py": f"from payments import Repo\ndef go():\n    r = Repo()\n    {write_}\n"}
    rep = scan.scan(SAVE, [write(tmp_path, files)])
    assert rep.rule_result("R") == "fail" and [f.code for f in rep.findings] == ["forbidden/monkeypatch"]
    how = "delattr" if "del" in write_ else "setattr"
    assert rep.findings[0].message.startswith(f"{how} on 'save'")


@pytest.mark.parametrize("use", ["setattr(r, 'save', print)", "delattr(r, 'save')"])
def test_the_builtin_setattr_is_one_finding(tmp_path, use):
    files = {"payments.py": REPO, "ops.py": f"from payments import Repo\ndef go():\n    r = Repo()\n    {use}\n"}
    rep = scan.scan(SAVE, [write(tmp_path, files)])
    assert [f.message.split(" ")[0] for f in rep.findings] == [use.split("(")[0]]


@pytest.mark.parametrize("write_", [
    "r.__dict__['other'] = print", "r.__dict__.update(other=1)", "x = r.__dict__.get('other')",
    "r.__dict__.update({'other': 1})", "object.__setattr__(r, 'other', 1)",
])
def test_a_dict_write_of_another_name_is_not(tmp_path, write_):
    files = {"payments.py": REPO, "ops.py": f"from payments import Repo\ndef go():\n    r = Repo()\n    {write_}\n"}
    assert run(tmp_path, files, SAVE) == ("pass", set())


def test_own_dict_writes_in_init_stay_quiet(tmp_path):
    files = {"payments.py": REPO, "ops.py": "class C:\n    def __init__(self):\n        self.__dict__['cache'] = 1\n"
                                            "        self.__dict__.update(size=2)\n"}
    assert run(tmp_path, files, SAVE) == ("pass", set())


def test_a_module_dict_write_over_the_door_is_a_monkeypatch(tmp_path):
    files = {"payments.py": REPO, "ops.py": "import payments\ndef go():\n    payments.__dict__['refund'] = print\n"}
    assert run(tmp_path, files) == ("fail", {"forbidden/monkeypatch", "unresolved/reflection"})


HELPER = "from payments import refund as pay_back, send_to_gateway as gw\nother = 1\n"


@pytest.mark.parametrize("use", ["setattr(helper, 'pay_back', print)", "delattr(helper, 'pay_back')",
                                 "setattr(helper, 'gw', print)", "delattr(helper, 'gw')"])
def test_setattr_on_another_modules_name_for_the_door_is_a_monkeypatch(tmp_path, use):
    files = {"payments.py": REPO, "helper.py": HELPER, "ops.py": f"import helper\ndef go():\n    {use}\n"}
    assert run(tmp_path, files) == ("fail", {"forbidden/monkeypatch"})


def test_setattr_on_another_name_of_that_module_is_not(tmp_path):
    files = {"payments.py": REPO, "helper.py": HELPER, "ops.py": "import helper\ndef go():\n    setattr(helper, 'other', 2)\n"}
    assert run(tmp_path, files) == ("pass", set())


# ---- M1: super(Cls, other) follows other's class, which the scan does not know ----

SUPER = ("from payments import Repo\nclass Safe:\n    def save(self):\n        pass\nclass Safe2:\n    def save(self):\n"
         "        pass\nclass C(Safe, Safe2):\n    def run(self, other):\n        super(Safe, {arg}).save()\n"
         "class Mix(Safe, Repo):\n    pass\ndef go():\n    C().run(Mix())\n")
PAY_REPO = "class Repo:\n    def save(self):\n        pass\ndef refund():\n    Repo().save()\n"


def test_super_of_another_object_is_name_matched(tmp_path):
    files = {"payments.py": PAY_REPO, "ops.py": SUPER.replace("{arg}", "other")}
    assert run(tmp_path, files, SAVE) == ("unresolved", {"unresolved/inferred-path"})


def test_super_of_self_still_follows_the_mro(tmp_path):
    files = {"payments.py": PAY_REPO, "ops.py": SUPER.replace("{arg}", "self")}
    assert run(tmp_path, files, SAVE) == ("pass", set())


# ---- M2: an imported singleton bound again in the importing module ----

@pytest.mark.parametrize("ops", [
    "from lib import s\nfrom payments import Repo\ns = Repo()\ndef go():\n    s.save()\n",
    "from lib import s\nfrom payments import Repo\ndef go():\n    s.save()\ndef reset():\n    global s\n    s = Repo()\n",
])
def test_an_imported_singleton_bound_again_is_not_typed(tmp_path, ops):
    files = {"payments.py": PAY_REPO, "lib.py": "class Safe:\n    def save(self):\n        pass\ns = Safe()\n", "ops.py": ops}
    assert run(tmp_path, files, SAVE) == ("unresolved", {"unresolved/inferred-path"})


def test_an_imported_singleton_bound_once_stays_typed(tmp_path):
    files = {"payments.py": PAY_REPO, "lib.py": "class Safe:\n    def save(self):\n        pass\ns = Safe()\n",
             "ops.py": "from lib import s\ndef go():\n    s.save()\n"}
    assert run(tmp_path, files, SAVE) == ("pass", set())


# ---- M3: a parameter named as a module class is not that class ----

BASES = ("from payments import send_to_gateway\n"
         "class Base:\n    def run(self):\n        pass\nclass Evil:\n    def run(self):\n        send_to_gateway(1, 2)\n"
         "class Target(Base):\n    pass\nclass Other:\n    pass\n"
         "def patch({p}):\n    {p}.__bases__ = (Evil,)\n"
         "def go():\n    patch(Target)\n    Target().run()\n")


@pytest.mark.parametrize("p", ["Other", "cls"])
def test_a_parameter_shadowing_a_class_is_any_class(tmp_path, p):
    files = {"payments.py": SINK + "def refund():\n    send_to_gateway(1, 2)\n", "ops.py": BASES.replace("{p}", p)}
    assert ("ops.Base.run", "ops.Evil.run") in calls_of(tmp_path, files, "ops.go")


@pytest.mark.parametrize("params", ["Helper, _send", "_send"])
def test_a_parameter_shadowing_a_function_is_not_the_replacement(tmp_path, params):
    ops = ("from payments import _send\nclass Helper:\n    def run(self):\n        pass\n"
           f"def patch({params}):\n    Helper.run = _send\n")
    files = {"payments.py": SINK + "def _send():\n    send_to_gateway(1, 2)\n", "ops.py": ops}
    assert calls_of(tmp_path, files, "ops.Helper.run") == []


def test_a_method_replaced_by_a_parameter_is_name_matched(tmp_path):
    ops = ("from payments import _send\nclass Helper:\n    def run(self):\n        pass\n"
           "def patch(_send):\n    Helper.run = _send\ndef go():\n    Helper().run()\n")
    files = {"payments.py": SINK + "def _send():\n    send_to_gateway(1, 2)\n", "ops.py": ops}
    g = scan.graph([write(tmp_path, files)], [])
    assert [type(c) for c in g.calls if c.caller == "ops.go"] == [_Fan]


def test_a_method_replaced_by_a_module_function_runs_it(tmp_path):
    ops = ("from payments import _send\nclass Helper:\n    def run(self):\n        pass\n"
           "def patch():\n    Helper.run = _send\n")
    files = {"payments.py": SINK + "def _send():\n    send_to_gateway(1, 2)\n", "ops.py": ops}
    assert calls_of(tmp_path, files, "ops.Helper.run") == [("payments._send",)]


# ---- M4: a def after a class-body alias is the method ----

ALIAS = "from payments import send_to_gateway\ndef quiet(self):\n    pass\nclass C:\n{body}def go():\n    C().save()\n"


def test_a_def_after_an_alias_is_the_method(tmp_path):
    body = "    save = quiet\n    def save(self):\n        send_to_gateway(1, 2)\n"
    files = {"payments.py": SINK + "def refund():\n    send_to_gateway(1, 2)\n", "ops.py": ALIAS.replace("{body}", body)}
    assert calls_of(tmp_path, files, "ops.go") == [("ops.C.save",)]


def test_an_alias_after_a_def_is_the_method(tmp_path):
    body = "    def save(self):\n        send_to_gateway(1, 2)\n    save = quiet\n"
    files = {"payments.py": SINK + "def refund():\n    send_to_gateway(1, 2)\n", "ops.py": ALIAS.replace("{body}", body)}
    assert calls_of(tmp_path, files, "ops.go") == [("ops.quiet",)]


# ---- M5: malformed rule fields are a clean error ----

@pytest.mark.parametrize("doc,msg", [
    ({"rules": [{"id": "R", "subject": "py:payments.refund", "mediation": {"sink": "py:payments.send"}}]},
     "subject must be a mapping"),
    ({"rules": [{"id": "R", "subject": ["x"], "mediation": {"sink": "py:payments.send"}}]}, "subject must be a mapping"),
    ({"rules": 5}, "rules must be a list"),
    ({"rules": "R"}, "rules must be a list"),
    ({"rules": {"id": "R"}}, "rules must be a list"),
])
def test_malformed_rules_are_a_config_error(doc, msg):
    with pytest.raises(scan.ScanConfigError, match=msg):
        scan.mediations(doc)


def test_a_subject_mapping_still_gives_the_default_door():
    med, _ = scan.mediations({"rules": [{"id": "R", "subject": {"target": "py:a.door"}, "mediation": {"sink": "py:a.b"}}]})
    assert med["R"].doors == ["py:a.door"]
