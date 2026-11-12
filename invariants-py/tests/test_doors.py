"""Door rules (door/sink/allow), door discovery in `inv init`, and the scan's receiver types."""
from __future__ import annotations

import ast
import json
import pathlib
import textwrap

import pytest

from invariants import doors, scan
from invariants.errors import LoadError
from invariants.loader import expand_short_forms, schema_path
from cli_support import run_inv, write_project

TEST_EXCLUDE = ["tests", "test", "testing", "test_*.py", "*_test.py", "conftest.py"]

STORE = '''\
def write_balance(conn, account, cents):
    conn.execute("UPDATE balances SET cents = ? WHERE account = ?", (cents, account))
'''
SERVICE = '''\
from bank.store import write_balance

def apply_transfer(conn, src, dst, cents, balances):
    if cents <= 0:
        raise ValueError("transfer must be positive")
    write_balance(conn, src, balances[src] - cents)
    write_balance(conn, dst, balances[dst] + cents)
'''
API = '''\
from bank.service import apply_transfer

def transfer(conn, body, balances):
    apply_transfer(conn, body["src"], body["dst"], body["cents"], balances)

def payout(conn, account, cents, balances):
    apply_transfer(conn, account, "payouts", cents, balances)
'''
BATCH = '''\
from bank.store import write_balance

def refund_all(conn, rows):
    for account, cents in rows:
        write_balance(conn, account, cents)

def fix_balance(conn, account):
    conn.execute("UPDATE balances SET cents = 0 WHERE account = ?", (account,))
'''


def _bank(tmp_path: pathlib.Path, batch: bool = True, **extra: str) -> pathlib.Path:
    files = {"pyproject.toml": '[project]\nname = "bank"\nversion = "0"\n',
             "bank/__init__.py": "", "bank/store.py": STORE, "bank/service.py": SERVICE, "bank/api.py": API,
             "tests/test_store.py": "from bank.store import write_balance\n\ndef test_w():\n"
                                    "    write_balance(None, 'a', 1)\n", **extra}
    if batch:
        files.setdefault("bank/batch.py", BATCH)
    return write_project(tmp_path / "bank", files)


DOOR = {"id": "LEDGER", "door": "py:bank.service.apply_transfer", "sink": "py:bank.store.write_balance"}


def _scan(root: pathlib.Path, rule: dict) -> scan.Report:
    return scan.scan(expand_short_forms({"rules": [dict(rule)]}), [root], TEST_EXCLUDE)


# --- the short door form ---------------------------------------------------------------------------
def test_door_rule_expands_to_a_structure_rule():
    r = expand_short_forms({"rules": [dict(DOOR, allow="py:bank.batch.refund_all")]})["rules"][0]
    assert r["mediation"] == {"sink": "py:bank.store.write_balance", "door": "py:bank.service.apply_transfer",
                              "allow": ["py:bank.batch.refund_all"]}
    assert r["bad"] == "false" and r["status"] == "enforce"
    assert r["enforcers"] == [{"point": "structure", "timing": "pre_effect", "mode": "enforce"}]
    assert not {"door", "sink", "allow"} & set(r)


@pytest.mark.parametrize("empty", [None, []])
def test_an_emptied_allow_list_means_no_known_back_doors(empty):
    r = expand_short_forms({"rules": [dict(DOOR, allow=empty)]})["rules"][0]
    assert "allow" not in r["mediation"]


def test_door_rule_with_guard_makes_the_guarded_function_the_door():
    r = expand_short_forms({"rules": [{"id": "R", "guard": "py:bank.service.apply_transfer", "bad": "args.cents <= 0",
                                       "sink": "py:bank.store.write_balance"}]})["rules"][0]
    assert r["mediation"] == {"sink": "py:bank.store.write_balance"}
    assert r["subject"] == {"target": "py:bank.service.apply_transfer"}
    assert r["enforcers"][0]["point"] == "decorator"


@pytest.mark.parametrize("rule,msg", [
    ({"id": "R", "door": "py:a.d"}, "names the sink"),
    ({"id": "R", "sink": "py:a.s"}, "names its door"),
    ({"id": "R", "door": "py:a.d", "sink": "a.s"}, "py:module.function"),
    ({"id": "R", "door": "py:a.d", "sink": "py:a.s", "bad": "x"}, "has no bad"),
    ({"id": "R", "door": "py:a.d", "sink": "py:a.s", "mediation": {"sink": "py:a.t"}}, "not both"),
    ({"id": "R", "door": "py:a.d", "sink": "py:a.s", "enforcers": []}, "drop enforcers"),
    ({"id": "R", "door": "py:a.d", "sink": "py:a.s", "allow": ["a.b"]}, "py:module.function"),
])
def test_door_rule_errors_say_what_to_write(rule, msg):
    with pytest.raises(LoadError, match=msg):
        expand_short_forms({"rules": [rule]})


def test_schema_accepts_the_door_form_and_the_expanded_one():
    import jsonschema
    schema = json.loads(schema_path().read_text())
    v = jsonschema.Draft202012Validator(schema)
    assert not list(v.iter_errors({"rules": [dict(DOOR, allow=["py:bank.batch.refund_all"])]}))
    assert not list(v.iter_errors(expand_short_forms({"rules": [dict(DOOR, allow=["py:x.y"])]})))
    assert list(v.iter_errors({"rules": [{"id": "R", "door": "py:a.d"}]}))          # no sink, no bad


def test_a_door_rule_is_enforce_without_a_test(tmp_path):
    root = _bank(tmp_path, batch=False)
    (root / "invariants.yaml").write_text(json.dumps({"rules": [DOOR]}))
    r = run_inv("validate", "invariants.yaml", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr


# --- the scan with allow ---------------------------------------------------------------------------
def test_back_doors_are_found_and_allow_freezes_them(tmp_path):
    root = _bank(tmp_path)
    rep = _scan(root, DOOR)
    got = {(f.kind, f.owner) for f in rep.for_rule("LEDGER")}
    assert got == {("bypass", "bank.batch.refund_all")}          # fix_balance's raw SQL is below the API
    frozen = dict(DOOR, allow=["py:bank.batch.refund_all"])
    assert _scan(root, frozen).rule_result("LEDGER") == "pass"


NEW_CALLER = '''\
from bank.batch import refund_all

def instant_refund(conn, account):
    refund_all(conn, [(account, 10_000)])
'''


def test_an_allowed_back_door_is_not_a_door_for_new_callers(tmp_path):
    root = _bank(tmp_path, **{"bank/new_api.py": NEW_CALLER})
    rep = _scan(root, dict(DOOR, allow=["py:bank.batch.refund_all"]))
    (f,) = rep.for_rule("LEDGER")
    assert f.kind == "bypass" and f.path == ["bank.new_api.instant_refund", "bank.batch.refund_all",
                                             "bank.store.write_balance"]
    both = dict(DOOR, allow=["py:bank.batch.refund_all", "py:bank.new_api.instant_refund"])
    assert _scan(root, both).rule_result("LEDGER") == "pass"     # only a person can add it (a weakening)


def test_new_code_called_from_an_allowed_back_door_is_a_bypass(tmp_path):
    batch = BATCH.replace("        write_balance(conn, account, cents)\n",
                          "        _helper(conn, account, cents)\n") + (
        "\ndef _helper(conn, account, cents):\n    write_balance(conn, account, cents)\n")
    root = _bank(tmp_path, **{"bank/batch.py": batch})
    rep = _scan(root, dict(DOOR, allow=["py:bank.batch.refund_all"]))
    (f,) = rep.for_rule("LEDGER")
    assert f.kind == "bypass" and f.owner == "bank.batch._helper"


@pytest.mark.parametrize("trick,code", [
    ("    setattr(svc, 'apply_transfer', lambda *a: None)\n", "forbidden/monkeypatch"),
    ("    svc.apply_transfer.__wrapped__(conn, 'a', 'b', 1, {})\n", "forbidden/unwrap"),
    ("    eval('1')\n", "unresolved/reflection"),
])
def test_an_allowed_back_door_still_cannot_tamper_with_the_door(tmp_path, trick, code):
    batch = "import bank.service as svc\n" + BATCH.replace("def refund_all(conn, rows):\n",
                                                         "def refund_all(conn, rows):\n" + trick)
    root = _bank(tmp_path, **{"bank/batch.py": batch})
    rule = dict(DOOR, allow=["py:bank.batch.refund_all"])
    assert code in {f.code for f in _scan(root, rule).for_rule("LEDGER")}


def test_a_bypass_message_names_the_door_not_the_allowed_back_doors(tmp_path):
    root = _bank(tmp_path, **{"bank/new.py": "from bank.store import write_balance\n\n"
                                            "def sneak(conn):\n    write_balance(conn, 'a', 1)\n"})
    rep = _scan(root, dict(DOOR, allow=["py:bank.batch.refund_all"]))
    (f,) = rep.for_rule("LEDGER")
    assert f.kind == "bypass" and f.owner == "bank.new.sneak"
    assert f.message.endswith("without passing bank.service.apply_transfer")


def test_an_allow_entry_that_is_no_longer_a_back_door_fails(tmp_path):
    root = _bank(tmp_path, **{"bank/batch.py": BATCH.replace("write_balance(conn, account, cents)", "pass")})
    rep = _scan(root, dict(DOOR, allow=["py:bank.batch.refund_all"]))
    (f,) = rep.for_rule("LEDGER")
    assert (f.kind, f.code, f.owner) == ("missing", "missing/allow-unused", "bank.batch.refund_all")
    gone = _scan(root, dict(DOOR, allow=["py:bank.batch.gone"]))
    assert [f.code for f in gone.for_rule("LEDGER")] == ["missing/allow"]


def test_allow_covers_the_ways_a_frozen_back_door_reaches_the_sink(tmp_path):
    root = _bank(tmp_path, batch=False, **{"bank/hack.py": textwrap.dedent('''\
        import bank.store as s

        def hack(conn):
            getattr(s, "write_balance")(conn, "a", 1)
        ''')})
    assert _scan(root, DOOR).rule_result("LEDGER") == "fail"
    assert _scan(root, dict(DOOR, allow="py:bank.hack.hack")).rule_result("LEDGER") == "pass"


def test_findings_carry_their_function(tmp_path):
    root = _bank(tmp_path)
    data = _scan(root, DOOR).as_dict()
    assert {f["owner"] for f in data["findings"]} == {"bank.batch.refund_all"}


# --- receiver types --------------------------------------------------------------------------------
REPO = '''\
class Repo:
    def __init__(self, conn):
        self.conn = conn

    def add(self, t, n):
        self.conn.execute("UPDATE usage SET n = n + ? WHERE t = ?", (n, t))
'''


def _edges(tmp_path: pathlib.Path, code: str) -> dict[tuple[str, str], bool]:
    root = write_project(tmp_path / "rt", {"q/__init__.py": "", "q/repo.py": REPO, "q/use.py": code})
    g = scan.graph([root])
    return {(e.caller, e.callee): e.inferred for e in g.edges if e.callee == "q.repo.Repo.add"}


@pytest.mark.parametrize("code,caller", [
    ("from q.repo import Repo\ndef f(c): Repo(c).add('t', 1)\n", "q.use.f"),
    ("from q.repo import Repo\ndef f(c):\n    r = Repo(c)\n    r.add('t', 1)\n", "q.use.f"),
    ("from q.repo import Repo\ndef f(r: Repo): r.add('t', 1)\n", "q.use.f"),
    ("from q.repo import Repo\ndef f(r: 'Repo'): r.add('t', 1)\n", "q.use.f"),
    ("from typing import Optional\nfrom q.repo import Repo\ndef f(r: Optional[Repo]): r.add('t', 1)\n", "q.use.f"),
    ("from q.repo import Repo\ndef f(r: Repo | None): r.add('t', 1)\n", "q.use.f"),
    ("from q import repo\ndef f(r: repo.Repo): r.add('t', 1)\n", "q.use.f"),
    ("from q.repo import Repo\nclass S:\n    def __init__(self, c):\n        self.r = Repo(c)\n"
     "    def go(self):\n        self.r.add('t', 1)\n", "q.use.S.go"),
    ("from q.repo import Repo\nclass S:\n    def __init__(self, r: Repo):\n        self.r = r\n"
     "    def go(self):\n        self.r.add('t', 1)\n", "q.use.S.go"),
    ("from q.repo import Repo\nclass S:\n    def __init__(self, r: Repo, n):\n        self.r, self.n = r, n\n"
     "    def go(self):\n        self.r.add('t', 1)\n", "q.use.S.go"),
    ("from q.repo import Repo\ndef f(c):\n    r, k = Repo(c), 1\n    r.add('t', k)\n", "q.use.f"),
    # an attribute chain on a typed receiver: ctx.repo is a Repo because Ctx sets it so
    ("from q.repo import Repo\nclass Ctx:\n    def __init__(self, c):\n        self.repo = Repo(c)\n"
     "def f(ctx: Ctx): ctx.repo.add('t', 1)\n", "q.use.f"),
    ("from q.repo import Repo\nclass Ctx:\n    def __init__(self, c):\n        self.repo = Repo(c)\n"
     "class Outer:\n    def __init__(self, c):\n        self.ctx = Ctx(c)\n"
     "    def go(self):\n        self.ctx.repo.add('t', 1)\n", "q.use.Outer.go"),
    ("import dataclasses\nfrom q.repo import Repo\n@dataclasses.dataclass\nclass Ctx:\n    repo: Repo\n"
     "def f(ctx: Ctx): ctx.repo.add('t', 1)\n", "q.use.f"),
    # a factory or FastAPI dependency annotated to return the repository
    ("from q.repo import Repo\ndef get_repo() -> Repo: ...\ndef f():\n    r = get_repo()\n    r.add('t', 1)\n", "q.use.f"),
    ("from typing import Optional\nfrom q.repo import Repo\ndef get_repo() -> Optional[Repo]: ...\n"
     "def f():\n    get_repo().add('t', 1)\n", "q.use.f"),
    ("from q.repo import Repo\ndef get_repo() -> 'Repo | None': ...\nclass S:\n    def __init__(self):\n"
     "        self.r = get_repo()\n    def go(self):\n        self.r.add('t', 1)\n", "q.use.S.go"),
])
def test_a_receiver_of_known_class_is_an_extracted_edge(tmp_path, code, caller):
    assert _edges(tmp_path, code) == {(caller, "q.repo.Repo.add"): False}


@pytest.mark.parametrize("code", [
    "def f(r): r.add('t', 1)\n",                                                     # no annotation
    "from q.repo import Repo\ndef f(c, other):\n    r = Repo(c)\n    r = other\n    r.add('t', 1)\n",
    "from q.repo import Repo\ndef f(c, xs):\n    r = Repo(c)\n    for r in xs:\n        r.add('t', 1)\n",
    "from q.repo import Repo\ndef f(c):\n    r = Repo(c)\n    def g(): pass\n    (r := c).add('t', 1)\n",
    "from q.repo import Repo\ndef f(c, pair):\n    r = Repo(c)\n    r, k = pair\n    r.add('t', 1)\n",
    "from q.repo import Repo\nclass S:\n    def __init__(self, c, pair):\n        self.r = Repo(c)\n"
    "        self.r, self.k = pair\n    def go(self):\n        self.r.add('t', 1)\n",
    "from q.repo import Repo\nclass S:\n    def __init__(self, c, o):\n        self.r = Repo(c)\n"
    "    def swap(self, o):\n        self.r = o\n    def go(self):\n        self.r.add('t', 1)\n",
    # set from outside the class: ctx.repo may hold anything
    "from q.repo import Repo\nclass Ctx:\n    def __init__(self, c):\n        self.repo = Repo(c)\n"
    "def f(ctx: Ctx): ctx.repo.add('t', 1)\ndef patch(ctx, other):\n    ctx.repo = other\n",
    "from q.repo import Repo\nclass Ctx:\n    def __init__(self, c):\n        self.repo = Repo(c)\n"
    "def f(ctx: Ctx): ctx.repo.add('t', 1)\ndef patch(ctx, other):\n    setattr(ctx, 'repo', other)\n",
    "from q.repo import Repo\ndef f(ctx): ctx.repo.add('t', 1)\n",                     # ctx untyped
    "from q.repo import Repo\ndef get_repo(): ...\ndef f():\n    get_repo().add('t', 1)\n",   # no annotation
    "from q.repo import Repo\ndef get_repo() -> Repo | int: ...\ndef f():\n    get_repo().add('t', 1)\n",
])
def test_an_unsure_receiver_stays_name_matched(tmp_path, code):
    got = _edges(tmp_path, code)
    assert got and all(got.values())                     # only inferred edges: the scan reports unresolved


def test_an_annotated_receiver_also_reaches_subclass_overrides(tmp_path):
    root = write_project(tmp_path / "sub", {
        "q/__init__.py": "", "q/repo.py": REPO,
        "q/fast.py": "from q.repo import Repo\nclass Fast(Repo):\n    def add(self, t, n):\n        pass\n",
        "q/use.py": "from q.repo import Repo\ndef f(r: Repo): r.add('t', 1)\n"
                    "def g(c):\n    Repo(c).add('t', 1)\n"})
    g = scan.graph([root])
    got = {(e.caller, e.callee) for e in g.edges if e.callee.endswith(".add") and not e.inferred}
    assert ("q.use.f", "q.fast.Fast.add") in got and ("q.use.f", "q.repo.Repo.add") in got
    assert ("q.use.g", "q.fast.Fast.add") not in got        # Repo(c) is exactly a Repo


# --- discovery -------------------------------------------------------------------------------------
def test_discover_finds_the_door_and_freezes_the_back_doors(tmp_path):
    root = _bank(tmp_path)
    found = doors.discover([root], TEST_EXCLUDE)
    (d,) = found.doors
    assert (d.rid, d.doors, d.sinks) == ("BALANCES-DOOR", ["bank.service.apply_transfer"],
                                         ["bank.store.write_balance"])
    assert d.allow == ["bank.batch.refund_all"] and d.active     # fix_balance's raw SQL is below the API
    assert d.callers == ["bank.api.payout", "bank.api.transfer"] and "tables" not in d.rule()
    assert len(d.back_doors) == 1 and d.back_doors[0].startswith("bank/batch.py:")
    assert _scan(root, d.rule()).rule_result("BALANCES-DOOR") == "pass"


def test_discover_freezes_the_call_chain_of_a_back_door(tmp_path):
    cli = {"bank/cli.py": "from bank.batch import refund_all\n\ndef main():\n    refund_all(None, [])\n"}
    (d,) = doors.discover([_bank(tmp_path, **cli)], TEST_EXCLUDE).doors
    assert d.allow == ["bank.batch.refund_all", "bank.cli.main"] and d.active
    root2 = _bank(tmp_path / "two", **cli, **{"bank/new_api.py": NEW_CALLER})
    rep = _scan(root2, d.rule())                                  # an agent adds a new caller later
    (f,) = rep.for_rule(d.rid)
    assert f.kind == "bypass" and f.path[0] == "bank.new_api.instant_refund"

def test_discover_gives_each_writer_its_own_door(tmp_path):
    root = write_project(tmp_path / "p", {
        "p/__init__.py": "",
        "p/store.py": "def insert(c, n):\n    c.execute('INSERT INTO pools (n) VALUES (?)', (n,))\n"
                      "def resize(c, n):\n    c.execute('UPDATE pools SET n = ?', (n,))\n",
        "p/service.py": "from p.store import insert, resize\n"
                        "def create(c, n):\n    insert(c, n)\ndef grow(c, n):\n    resize(c, n)\n",
        "p/api.py": "from p.service import create, grow\n"
                    "def a(c): create(c, 1)\ndef b(c): create(c, 2)\ndef x(c): grow(c, 3)\ndef y(c): grow(c, 4)\n"})
    (d,) = doors.discover([root]).doors
    assert d.doors == ["p.service.create", "p.service.grow"] and d.sinks == ["p.store.insert", "p.store.resize"]
    assert d.back_doors == [] and d.active


def test_discover_needs_a_shared_door_and_lists_tables_without_one(tmp_path):
    root = write_project(tmp_path / "n", {
        "n/__init__.py": "",
        "n/a.py": "def one(c):\n    c.execute('DELETE FROM jobs')\n",
        "n/b.py": "def two(c):\n    c.execute('UPDATE jobs SET x = 1')\n"})
    found = doors.discover([root])
    assert found.doors == [] and found.no_door == {"table jobs": ["n.a.one", "n.b.two"]}
    text = "\n".join(doors.render(found))
    assert "no shared door" in text and "table jobs: n.a.one, n.b.two" in text


def test_discover_ranks_money_first_and_lists_what_it_drops(tmp_path):
    files = {"m/__init__.py": ""}
    for t in ("alpha", "beta", "balances"):
        files[f"m/{t}.py"] = (f"def w_{t}(c):\n    c.execute('UPDATE {t} SET x = 1')\n"
                              f"def door_{t}(c):\n    w_{t}(c)\n"
                              f"def e1_{t}(c):\n    door_{t}(c)\ndef e2_{t}(c):\n    door_{t}(c)\n")
    found = doors.discover([write_project(tmp_path / "m", files)], limit=1)
    assert [d.rid for d in found.doors] == ["BALANCES-DOOR"]
    assert sorted(d.rid for d in found.dropped) == ["ALPHA-DOOR", "BETA-DOOR"]
    assert "More doors found but not written" in "\n".join(doors.render(found))


def test_a_back_door_outside_any_function_leaves_the_rule_commented_out(tmp_path):
    root = _bank(tmp_path, **{"bank/boot.py": "from bank.store import write_balance\n\n"
                                             "write_balance(None, 'a', 0)\n"})
    (d,) = doors.discover([root], TEST_EXCLUDE).doors
    assert not d.active and "not inside a function" in d.why_inactive
    text = "\n".join(doors.render(doors.DoorsFound([d], [], {}, 1)))
    assert "  # - id: BALANCES-DOOR" in text and "\n  - id:" not in text


# --- inv init and inv check, end to end ------------------------------------------------------------
def test_init_writes_a_door_rule_that_check_passes_and_a_new_back_door_fails(tmp_path):
    root = _bank(tmp_path)
    r = run_inv("init", "--no-lock", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "door BALANCES-DOOR" in r.stdout and "found 1 back door(s) in bank/batch.py" in r.stdout
    text = (root / "invariants.yaml").read_text()
    assert '  - id: BALANCES-DOOR\n    door: "py:bank.service.apply_transfer"' in text
    assert run_inv("lock", cwd=root).returncode == 0
    r = run_inv("check", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no path skips a door or guard" in r.stdout
    (root / "bank/promo.py").write_text("from bank.store import write_balance\n\n"
                                        "def bonus(conn):\n    write_balance(conn, 'a', 100)\n")
    r = run_inv("check", cwd=root)
    assert r.returncode == 1 and "bank.promo.bonus -> bank.store.write_balance" in r.stdout


def test_check_skips_tests_by_default_and_pyproject_can_scan_them(tmp_path):
    root = _bank(tmp_path, batch=False)
    (root / "invariants.yaml").write_text(json.dumps({"rules": [DOOR]}))
    assert run_inv("lock", cwd=root).returncode == 0
    assert run_inv("check", cwd=root).returncode == 0                  # tests/test_store.py is skipped
    with (root / "pyproject.toml").open("a") as f:
        f.write("\n[tool.invariants.scan]\nexclude = []\n")
    r = run_inv("check", cwd=root)
    assert r.returncode == 1 and "tests.test_store.test_w" in r.stdout


def test_allowing_a_new_back_door_is_a_weakening_and_removing_one_is_not(tmp_path):
    root = _bank(tmp_path)
    rule = dict(DOOR, allow=["py:bank.batch.refund_all", "py:bank.batch.fix_balance"])
    (root / "invariants.yaml").write_text(json.dumps({"rules": [rule]}))
    assert run_inv("lock", cwd=root).returncode == 0
    more = dict(rule, allow=[*rule["allow"], "py:bank.api.transfer"])
    (root / "invariants.yaml").write_text(json.dumps({"rules": [more]}))
    r = run_inv("lock", "--check", cwd=root)
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "mediation.allow" in out and "a back door is now allowed: py:bank.api.transfer" in out
    fewer = dict(rule, allow=["py:bank.batch.refund_all"])
    (root / "invariants.yaml").write_text(json.dumps({"rules": [fewer]}))
    r = run_inv("lock", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr                      # a tightening re-pins without a person


def test_init_no_doors_skips_discovery(tmp_path):
    root = _bank(tmp_path)
    r = run_inv("init", "--no-lock", "--no-doors", cwd=root)
    assert r.returncode == 0 and "door" not in r.stdout.replace("back door", "")
    assert "BALANCES-DOOR" not in (root / "invariants.yaml").read_text()


# --- the repository layer: ORMs, document stores and API clients, no SQL needed ------------------
@pytest.mark.parametrize("code,write", [
    ("self.session.add(x)", True),
    ("db.session.add_all(xs)", True),
    ("session.execute(update(T).where(T.a == 1).values(b=2))", True),
    ("session.execute(insert(T).values(a=1))", True),
    ("session.execute(select(T))", False),                    # a read
    ("conn.execute('SELECT 1')", False),                      # SQL text is the SQL reader's job
    ("session.commit()", False),                              # commits writes made elsewhere
    ("Expense.objects.filter(id=1).update(status='x')", True),
    ("Order.objects.create(total=1)", True),
    ("self.collection.insert_one(doc)", True),
    ("self.table.put_item(Item=x)", True),
    ("stripe.Refund.create(charge=c, amount=n)", True),
    ("self.client.post(url)", False),                         # post is not a store write by itself
    ("counts.update(other)", False),                          # dict.update
    ("seen.add(x)", False),                                   # set.add
    ("self.items.delete(x)", False),
])
def test_store_writes_are_told_from_ordinary_method_calls(code, write):
    call = ast.parse(code, mode="eval").body
    assert (doors._store_call(call) is not None) is write


@pytest.mark.parametrize("name,write", [("add_usage", True), ("save", True), ("setStatus", True), ("set_size", True),
                                        ("get", False), ("current", False), ("list_all", False), ("settle", False),
                                        ("__init__", False), ("addresses", False)])
def test_write_names(name, write):
    assert doors._is_write_name(name) is write


@pytest.mark.parametrize("fid,svc", [
    ("app.service.refund", True), ("app.services.refund", True), ("app.billing_service.refund", True),
    ("app.svc.BillingService.refund", True), ("app.x.OrderManager.place", True),
    ("pool_service.api.split", False), ("pool_service.rogue.hack", False), ("app.api.BillingView.post", False),
])
def test_the_service_layer_is_judged_by_its_own_module_or_class_name(fid, svc):
    assert doors._service_like(fid) is svc


ORM_APP = {
    "shop/__init__.py": "",
    "shop/repo.py": textwrap.dedent('''\
        import stripe
        from sqlalchemy import delete, select, update


        class OrderRepo:
            def __init__(self, session):
                self.session = session

            def get(self, oid):
                return self.session.execute(select(Order).where(Order.id == oid)).first()

            def add(self, order):
                self.session.add(order)

            def cancel(self, oid):
                self.session.execute(update(Order).where(Order.id == oid).values(state="cancelled"))

            def purge(self):
                self.session.execute(delete(Order))


        class PaymentGateway:
            def charge(self, customer, cents):
                return stripe.Charge.create(customer=customer, amount=cents)
        '''),
    "shop/service.py": textwrap.dedent('''\
        from shop.repo import OrderRepo, PaymentGateway


        class OrderService:
            def __init__(self, repo: OrderRepo, pay: PaymentGateway):
                self.repo, self.pay = repo, pay

            def place(self, order, customer):
                self.pay.charge(customer, order.total)
                self.repo.add(order)

            def cancel(self, oid):
                self.repo.cancel(oid)
        '''),
    "shop/api.py": textwrap.dedent('''\
        from shop.repo import OrderRepo, PaymentGateway
        from shop.service import OrderService


        def place(session, order, customer):
            OrderService(OrderRepo(session), PaymentGateway()).place(order, customer)


        def cancel(session, oid):
            OrderService(OrderRepo(session), PaymentGateway()).cancel(oid)


        def admin_cancel(session, oid):
            OrderRepo(session).cancel(oid)


        def purge_all(session):
            OrderRepo(session).purge()
        '''),
}


def test_an_orm_and_api_client_codebase_gets_door_rules_without_sql(tmp_path):
    root = write_project(tmp_path / "shop", ORM_APP)
    found = doors.discover([root])
    by_id = {d.rid: d for d in found.doors}
    assert set(by_id) == {"ORDER-REPO-DOOR", "PAYMENT-GATEWAY-DOOR"}
    o = by_id["ORDER-REPO-DOOR"]
    # get() is a read; purge() has no door at all but is still the repository's write, so its caller is a
    # back door: nobody but a door may call any write of the repository
    assert o.sinks == ["shop.repo.OrderRepo.add", "shop.repo.OrderRepo.cancel", "shop.repo.OrderRepo.purge"]
    assert o.doors == ["shop.service.OrderService.place", "shop.service.OrderService.cancel"]
    assert "tables" not in o.rule()
    assert o.allow == ["shop.api.admin_cancel", "shop.api.purge_all"] and o.active
    p = by_id["PAYMENT-GATEWAY-DOOR"]
    assert p.sinks == ["shop.repo.PaymentGateway.charge"] and p.back_doors == []
    for d in found.doors:
        assert _scan(root, d.rule()).rule_result(d.rid) == "pass"
    sneak = dict(ORM_APP, **{"shop/batch.py": "from shop.repo import PaymentGateway\n\n"
                                             "def bonus(c):\n    PaymentGateway().charge(c, -500)\n"})
    root2 = write_project(tmp_path / "shop2", sneak)
    rep = _scan(root2, p.rule())
    assert [f.owner for f in rep.for_rule(p.rid)] == ["shop.batch.bonus"]


def test_default_excludes_reach_nested_tests_and_migrations(tmp_path):
    from invariants.cli import DEFAULT_SCAN_EXCLUDE
    root = write_project(tmp_path / "nest", {
        "app/__init__.py": "", "app/x.py": "def f(): pass\n",
        "app/tests/test_x.py": "def test_a(): pass\n", "app/sub/conftest.py": "",
        "app/alembic/versions/v1.py": "def upgrade(): pass\n", "app/migrations/0001.py": "X = 1\n"})
    names = [m for _, _, m in scan.iter_sources([root], DEFAULT_SCAN_EXCLUDE)]
    assert names == ["app", "app.x"]


def test_an_attribute_chain_resolves_in_the_module_that_defines_the_class(tmp_path):
    # Ctx imports Repo in its own module; the caller, elsewhere, never names Repo
    root = write_project(tmp_path / "x", {
        "q/__init__.py": "", "q/repo.py": REPO,
        "q/ctx.py": "from q.repo import Repo\n\nclass Ctx:\n    def __init__(self, c):\n        self.repo = Repo(c)\n",
        "q/use.py": "from q.ctx import Ctx\n\ndef f(ctx: Ctx):\n    ctx.repo.add('t', 1)\n"})
    g = scan.graph([root])
    assert {(e.caller, e.inferred) for e in g.edges if e.callee == "q.repo.Repo.add"} == {("q.use.f", False)}


def test_a_factory_in_another_module_resolves_where_it_is_written(tmp_path):
    root = write_project(tmp_path / "f", {
        "q/__init__.py": "", "q/repo.py": REPO,
        "q/deps.py": "from q.repo import Repo\n\ndef get_repo(c) -> Repo:\n    return Repo(c)\n",
        "q/use.py": "from q.deps import get_repo\n\ndef f(c):\n    get_repo(c).add('t', 1)\n"})
    g = scan.graph([root])
    assert {(e.caller, e.inferred) for e in g.edges if e.callee == "q.repo.Repo.add"} == {("q.use.f", False)}


# --- FastAPI layouts: routes -> a thin layer -> crud, typed sessions ------------------------------
FASTAPI_APP = {
    "app/__init__.py": "",
    "app/deps.py": textwrap.dedent('''\
        from typing import Annotated
        from fastapi import Depends
        from sqlmodel import Session

        def get_db():
            yield None

        SessionDep = Annotated[Session, Depends(get_db)]
        '''),
    "app/crud.py": textwrap.dedent('''\
        from sqlmodel import Session

        def create_item(*, s: Session, title: str):
            s.add(title)
            s.commit()

        def read_item(*, s: Session, item_id: int):
            return s.get(item_id)
        '''),
    "app/logic/__init__.py": "",
    "app/logic/items.py": textwrap.dedent('''\
        from app import crud

        def place_item(s, title):
            if not title:
                raise ValueError("empty title")
            crud.create_item(s=s, title=title)
        '''),
    "app/routes/__init__.py": "",
    "app/routes/items.py": textwrap.dedent('''\
        from fastapi import APIRouter
        from app import crud
        from app.deps import SessionDep
        from app.logic import items

        router = APIRouter()

        @router.post("/items")
        def create(session: SessionDep, title: str):
            items.place_item(session, title)

        @router.post("/items/quick")
        def quick_create(session: SessionDep, title: str):
            crud.create_item(s=session, title=title)

        @router.get("/items/{item_id}")
        def read(session: SessionDep, item_id: int):
            return crud.read_item(s=session, item_id=item_id)
        '''),
}


def test_a_typed_session_is_a_store_whatever_its_name():
    tree = ast.parse("from sqlmodel import Session\nX = 1\ndef f(s: Session, sess: 'AsyncSession', n: int):\n"
                     "    s.add(1)\n    sess.delete(2)\n    n.add(3)\n")
    assert sorted(doors._writes_in(tree.body[2])) == ["s.add", "sess.delete"]
    alias = ast.parse("from typing import Annotated\nfrom sqlmodel import Session\nSessionDep = Annotated[Session, 1]\n")
    assert doors.store_aliases(alias) == {"SessionDep"}
    fn = ast.parse("def g(db: SessionDep):\n    db.merge(1)\n").body[0]
    assert doors._writes_in(fn, frozenset({"SessionDep"})) == ["db.merge"]


def test_routes_through_a_one_to_one_layer_to_crud_get_a_door(tmp_path):
    root = write_project(tmp_path / "fa", FASTAPI_APP)
    (d,) = doors.discover([root]).doors
    assert d.sinks == ["app.crud.create_item"]                    # read_item does not write
    assert d.doors == ["app.logic.items.place_item"]              # called once, but only by a route
    assert d.allow == ["app.routes.items.quick_create"] and d.active    # the route that skips it, frozen
    rep = scan.scan(expand_short_forms({"rules": [d.rule()]}), [root])
    assert rep.rule_result(d.rid) == "pass"


def test_a_one_caller_layer_that_no_route_calls_is_not_a_door(tmp_path):
    routes = FASTAPI_APP["app/routes/items.py"].replace("    items.place_item(session, title)",
                                                        "    crud.create_item(s=session, title=title)")
    files = dict(FASTAPI_APP, **{"app/routes/items.py": routes,
                                 "app/jobs.py": "from app.logic import items\n\ndef nightly(s):\n"
                                                "    items.place_item(s, 'x')\n"})
    assert doors.discover([write_project(tmp_path / "fb", files)]).doors == []
