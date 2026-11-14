"""Round 31 (scan): lookups the scan missed, stale typing, huge literals' shas, and rule checks.

1. `x.__getattribute__("save")`, `object.__getattribute__(x, "save")`, `type.__getattribute__(type(x),
   "pay")` on a receiver that is not a scanned module or class passed: they are getattr(x, k).
2. `type(r).__dict__["pay"](r)` (and .get, __class__, __mro__[i]) never fanned: typed or name-matched.
3. A datetime's tz-asking methods were typed as never calling user code; `"{0.total}".format(a)` runs a
   property getter.
4. A value class name rebound (`global Path`) and a name written from another module (`ops.Safe = Repo`,
   `setattr(ops, "Safe", Repo)`) stayed typed.
5. Two different huge int literals shared a sha.
A. With a sink mapping, a stale allow entry that is a door of another sink was never reported.
C. A rule's own allow_unresolved entry naming other rules said "shared" but they never got it.
D. A sink that is its own door silently disabled its check.
"""
from __future__ import annotations

import pathlib
import textwrap

import pytest

import invariants_scan as scan
from invariants_scan.accept import site_sha

PAY = ("def send_to_gateway(c, a):\n    pass\nclass Repo(Exception):\n    def pay(self):\n"
       "        send_to_gateway(1, 2)\n"
       "def refund(c, a):\n    Repo().pay()\n")
RULE = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway", "door": ["py:payments.refund"]}}]}
PAY10 = ("class Repo:\n    def save(self):\n        pass\nclass Svc:\n    def __init__(self):\n        self.r = Repo()\n"
         "    def refund(self):\n        self.r.save()\n")
RULE10 = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.Repo.save", "door": ["py:payments.Svc.refund"]}}]}


def run(tmp_path: pathlib.Path, files: dict[str, str], pay: str = PAY, rule: dict = RULE) -> tuple[str, set[str]]:
    for k, v in {"payments.py": pay, **files}.items():
        p = tmp_path / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(v))
    rep = scan.scan(rule, [tmp_path])
    return rep.rule_result("R"), {f.code for f in rep.findings}


@pytest.mark.parametrize("body", [
    "def go(r):\n    r.__getattribute__('pay')()\n",
    "def go(r):\n    object.__getattribute__(r, 'pay')()\n",
    "def go(r):\n    type.__getattribute__(type(r), 'pay')(r)\n",
    "import threading\ndef go(r):\n    threading.Thread(target=r.__getattribute__('pay')).start()\n",
])
def test_getattribute_on_a_value_is_a_getattr(tmp_path, body):
    res, codes = run(tmp_path, {"ops.py": body})
    assert res == "unresolved" and "unresolved/inferred-path" in codes, codes


@pytest.mark.parametrize("body", [
    "def go(m):\n    m.__getattribute__('send_to_gateway')(1, 2)\n",
    "def go(m):\n    object.__getattribute__(m, 'send_to_gateway')(1, 2)\n",
])
def test_getattribute_of_the_sink_name_is_forbidden(tmp_path, body):
    assert run(tmp_path, {"ops.py": body}) == ("fail", {"forbidden/getattr-sink"})


@pytest.mark.parametrize("body", [
    "import payments\ndef go():\n    payments.Repo().__getattribute__('pay')()\n",
    "import payments\ndef go():\n    object.__getattribute__(payments.Repo(), 'pay')()\n",
    "import payments\ndef go():\n    r = payments.Repo()\n    type(r).__dict__['pay'](r)\n",
])
def test_typed_lookups_are_edges(tmp_path, body):
    assert run(tmp_path, {"ops.py": body}) == ("fail", {"bypass"})


@pytest.mark.parametrize("body", [
    "def go(r):\n    type(r).__dict__['pay'](r)\n",
    "def go(r):\n    type(r).__dict__.get('pay')(r)\n",
    "def go(r):\n    r.__class__.__dict__['pay'](r)\n",
    "def go(r):\n    type(r).__mro__[0].__dict__['pay'](r)\n",
])
def test_class_namespace_lookup_fans(tmp_path, body):
    res, codes = run(tmp_path, {"ops.py": body})
    assert res == "unresolved" and "unresolved/inferred-path" in codes, codes


def test_untyped_getattribute_of_a_sink_method_name(tmp_path):
    assert run(tmp_path, {"ops.py": "def go(r):\n    r.__getattribute__('save')()\n"}, PAY10, RULE10) \
        == ("fail", {"forbidden/getattr-sink"})


PAY_TZ = ("import datetime\ndef send_to_gateway(c, a):\n    pass\n"
          "class TZ(datetime.tzinfo):\n    def utcoffset(self, dt):\n        send_to_gateway(1, 2)\n        return None\n"
          "class Acct:\n    @property\n    def total(self):\n        send_to_gateway(1, 2)\n        return 1\n"
          "def refund(c, a):\n    TZ().utcoffset(None)\n    Acct().total\n")


@pytest.mark.parametrize("body", [
    "import datetime\ndef go(tz):\n    datetime.datetime(2020, 1, 1, tzinfo=tz).utcoffset()\n",
    "import datetime\ndef go(tz):\n    datetime.datetime(2020, 1, 1, tzinfo=tz).isoformat()\n",
    "import datetime\ndef go(tz):\n    datetime.datetime(2020, 1, 1, tzinfo=tz).astimezone()\n",
    "def go(a):\n    print('{0.total}'.format(a))\n",
    "def go(a):\n    print('{x.total:>{w}}'.format_map({'x': a, 'w': 3}))\n",
    "def go(a):\n    print('{0:>{1.total}}'.format(3, a))\n",
])
def test_stdlib_calls_into_user_code(tmp_path, body):
    res, codes = run(tmp_path, {"ops.py": body}, PAY_TZ)
    assert res == "unresolved" and "unresolved/inferred-path" in codes, codes


def test_format_field_naming_the_sink_is_forbidden(tmp_path):
    assert run(tmp_path, {"ops.py": "def go(m):\n    '{0.send_to_gateway}'.format(m)\n"})[1] \
        == {"forbidden/getattr-sink"}


def test_plain_datetime_and_format_stay_typed(tmp_path):
    body = "import datetime\ndef go(x):\n    datetime.date(2020, 1, 1).replace(day=2)\n    '{0}'.format(x)\n"
    assert run(tmp_path, {"ops.py": body}, PAY_TZ) == ("pass", set())


PAY3 = ("def send_to_gateway(c, a):\n    pass\n"
        "class Repo:\n    def resolve(self):\n        send_to_gateway(1, 2)\n"
        "class Safe:\n    def resolve(self):\n        pass\n"
        "def refund(c, a):\n    Repo().resolve()\n")
GO = "import payments\nfrom payments import Safe\ndef go():\n    Safe().resolve()\n"


@pytest.mark.parametrize("files", [
    {"ops.py": "import payments\nfrom pathlib import Path\ndef go():\n    Path('x').resolve()\n"
               "def patch():\n    global Path\n    Path = payments.Repo\n"},
    {"ops.py": GO, "ev.py": "import ops, payments\nops.Safe = payments.Repo\n"},
    {"ops.py": GO, "ev.py": "import ops, payments\nsetattr(ops, 'Safe', payments.Repo)\n"},
    {"ops.py": "from payments import Safe\ndef go():\n    Safe().resolve()\n",
     "ev.py": "import ops\nfrom payments import Repo\nops.Safe = Repo\n"},
])
def test_rebound_names_are_not_typed(tmp_path, files):
    res, codes = run(tmp_path, files, PAY3)
    assert res == "unresolved" and "unresolved/inferred-path" in codes, codes


def test_unpatched_name_stays_typed(tmp_path):
    assert run(tmp_path, {"ops.py": GO, "ev.py": "import ops\nops.other = 1\n"}, PAY3) == ("pass", set())


def test_huge_int_literals_hash_apart():
    a = "getattr(obj, NAMES[0x3" + "0" * 5000 + "])"
    b = "getattr(obj, NAMES[0x2" + "0" * 5000 + "])"
    assert site_sha("unresolved/computed-attribute", "ops.go", a) != \
        site_sha("unresolved/computed-attribute", "ops.go", b)


def _mapped(tmp_path: pathlib.Path, routes: str) -> scan.Report:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "store.py").write_text("def record_refund(x): pass\ndef cancel_sub(x): pass\n")
    (tmp_path / "app" / "routes.py").write_text(routes)
    rule = {"rules": [{"id": "A", "mediation": {
        "sink": {"py:app.store.record_refund": "py:app.routes.refund", "py:app.store.cancel_sub": "py:app.routes.cancel"},
        "allow": {"py:app.routes.cancel": "py:app.store.record_refund"}}}]}   # round 32: a map
    return scan.scan(rule, [tmp_path])


def test_stale_allow_that_is_another_sinks_door_is_reported(tmp_path):
    rep = _mapped(tmp_path, "from app import store\ndef refund(x): store.record_refund(x)\n"
                            "def cancel(x):\n    store.cancel_sub(x)\n")
    assert [f.code for f in rep.findings] == ["missing/allow-unused"], rep.findings


def test_allow_used_by_another_sink_is_kept(tmp_path):
    rep = _mapped(tmp_path, "from app import store\ndef refund(x): store.record_refund(x)\n"
                            "def cancel(x):\n    store.cancel_sub(x)\n    store.record_refund(x)\n")
    assert rep.findings == [] and rep.rule_result("A") == "pass"


@pytest.mark.parametrize("med", [
    {"sink": {"py:a.s": ["py:a.s"], "py:a.t": "py:a.d"}},
    {"sink": ["py:a.s"], "door": ["py:a.s"]},
])
def test_a_sink_is_never_its_own_door(med):
    with pytest.raises(scan.ScanConfigError, match="own door"):
        scan.mediations({"rules": [{"id": "A", "mediation": med}]})


ENTRY = {"at": "a.py:3:9", "code": "unresolved/reflection", "sha": "e015a14365f0149c", "why": "reviewed"}


def test_own_entry_naming_other_rules_is_refused():
    doc = {"rules": [
        {"id": "A", "mediation": {"sink": "py:a.s", "door": "py:a.d", "allow_unresolved": [{**ENTRY, "rules": ["A", "B"]}]}},
        {"id": "B", "mediation": {"sink": "py:a.t", "door": "py:a.d"}}]}
    with pytest.raises(scan.ScanConfigError, match="rules names B"):
        scan.mediations(doc)


def test_shared_entry_given_to_each_rule_loads():
    e = {**ENTRY, "rules": ["A", "B"]}
    doc = {"rules": [
        {"id": "A", "mediation": {"sink": "py:a.s", "door": "py:a.d", "allow_unresolved": [dict(e)]}},
        {"id": "B", "mediation": {"sink": "py:a.t", "door": "py:a.d", "allow_unresolved": [dict(e)]}}]}
    scanned, _ = scan.mediations(doc)
    assert scanned["B"].accept[0].rules == ("A", "B")


def test_shared_entry_from_the_top_level_list_loads(tmp_path):
    from invariants.loader import short_forms
    doc = {"rules": [{"id": "A", "door": "py:a.d", "sink": "py:a.s"}, {"id": "B", "door": "py:a.d", "sink": "py:a.t"}],
           "allow_unresolved": [{**ENTRY, "rules": ["A", "B"]}]}
    doc = short_forms.expand_short_forms(doc)
    scanned, _ = scan.mediations(doc)
    assert scanned["A"].accept[0].rules == ("A", "B") == scanned["B"].accept[0].rules


def test_old_sha_hint_names_accept_weakening():
    import inspect

    from invariants_scan import accept
    assert "inv lock --accept-weakening" in inspect.getsource(accept)
