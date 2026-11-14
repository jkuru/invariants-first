"""Round 30: three features the owner approved.

1. Door-to-sink pairing: `sink:` (and mediation.sink) may map each sink to the doors that may reach it.
   A door listed for one sink is no door for another, so a write moved into the wrong door is a bypass.
   The list form keeps its meaning. The lock pins the mapping (a door added to a sink, a sink dropped,
   or a move to the list form that widens a sink's doors is WEAKENED); `inv status`, MCP explain and
   the scan's JSON show the pairing; `inv init` drafts the mapping when a repository's writes have
   different doors.
2. `inv init --root/--exclude` writes [tool.invariants.scan] in pyproject.toml (never over existing
   settings), so the next `inv check` scans what init scanned.
3. One allow_unresolved entry for several rules: a top-level `allow_unresolved:` whose entries carry
   `rules: [ID, ...]`; each accepts its one site once under each rule it lists.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tomllib

import pytest
import yaml
from cli_support import run_inv, write_project

import invariants as inv
from invariants import doors, scan
from invariants.errors import LoadError
from invariants.loader import expand_short_forms, read_document
from invariants.lock import changes, pinning
from invariants.lock.rule_diff import _mediation_changes
from invariants.scan import ScanConfigError

REPO = pathlib.Path(__file__).resolve().parents[2]
SUPPORT = REPO / "apps" / "support-agent"

# --- a small billing app: one repository, three writes, each with its own door ----------------------------
STORE = ("class LedgerRepo:\n"
         "    def record_refund(self, o, c):\n        pass\n"
         "    def record_cancel(self, c):\n        pass\n"
         "    def insert_credit(self, c, n):\n        pass\n")
SERVICE = ("from bill.store import LedgerRepo\n\n"
           "class BillingService:\n"
           "    def __init__(self, repo: LedgerRepo):\n        self.repo = repo\n"
           "    def refund(self, o, c):\n        self.repo.record_refund(o, c)\n"
           "    def cancel(self, c):\n        self.repo.record_cancel(c)\n"
           "    def credit(self, c, n):\n        self.repo.insert_credit(c, n)\n")
# the defect: a refund record written inside cancel (the support-agent case)
SERVICE_BAD = SERVICE.replace("        self.repo.record_cancel(c)\n",
                              "        self.repo.record_cancel(c)\n        self.repo.record_refund('all', c)\n")
API = ("from bill.service import BillingService\n\n"
       "def refund_route(s: BillingService, o, c):\n    s.refund(o, c)\n"
       "def refund_tool(s: BillingService, o, c):\n    s.refund(o, c)\n"
       "def cancel_route(s: BillingService, c):\n    s.cancel(c)\n"
       "def cancel_tool(s: BillingService, c):\n    s.cancel(c)\n"
       "def credit_route(s: BillingService, c):\n    s.credit(c, 1)\n"
       "def credit_tool(s: BillingService, c):\n    s.credit(c, 2)\n")

S, D = "py:bill.store.LedgerRepo.", "py:bill.service.BillingService."
LIST_RULE = {"id": "LEDGER", "door": [D + "refund", D + "cancel", D + "credit"],
             "sink": [S + "record_refund", S + "record_cancel", S + "insert_credit"]}
PAIRS = {S + "record_refund": [D + "refund"], S + "record_cancel": D + "cancel", S + "insert_credit": [D + "credit"]}
MAP_RULE = {"id": "LEDGER", "sink": PAIRS}


def _bill(tmp_path: pathlib.Path, service: str = SERVICE, **extra: str) -> pathlib.Path:
    return write_project(tmp_path, {"bill/__init__.py": "", "bill/store.py": STORE, "bill/service.py": service,
                                    "bill/api.py": API, **extra})


def _scan(root: pathlib.Path, *rules: dict) -> scan.Report:
    return scan.scan(expand_short_forms({"rules": [json.loads(json.dumps(r)) for r in rules]}), [root])


# --- 1. pairing: the scan ------------------------------------------------------------------------------------
def test_the_list_form_lets_any_door_reach_any_sink_as_before(tmp_path):
    rep = _scan(_bill(tmp_path, SERVICE_BAD), LIST_RULE)
    assert rep.rule_result("LEDGER") == "pass", rep.findings


def test_a_write_moved_into_another_sinks_door_is_a_bypass(tmp_path):
    rep = _scan(_bill(tmp_path, SERVICE_BAD), MAP_RULE)
    (f,) = rep.for_rule("LEDGER")
    assert f.kind == "bypass" and f.path[-2:] == ["bill.service.BillingService.cancel",
                                                  "bill.store.LedgerRepo.record_refund"], f
    assert "bill.service.BillingService.refund" in f.message, f.message     # the door record_refund needs


def test_the_mapping_passes_clean_code(tmp_path):
    rep = _scan(_bill(tmp_path), MAP_RULE)
    assert rep.rule_result("LEDGER") == "pass" and rep.findings == [], rep.findings
    out = rep.as_dict()["rules"]["LEDGER"]
    assert out["pairs"] == {S + "record_refund": [D + "refund"], S + "record_cancel": [D + "cancel"],
                            S + "insert_credit": [D + "credit"]}
    assert sorted(out["doors"]) == sorted([D + "refund", D + "cancel", D + "credit"])


def test_a_back_door_to_one_sink_is_reported_once(tmp_path):
    rogue = "from bill.store import LedgerRepo\n\ndef purge(r: LedgerRepo, c):\n    r.record_cancel(c)\n"
    rep = _scan(_bill(tmp_path, **{"bill/rogue.py": rogue}), MAP_RULE)
    assert [(f.kind, f.path[0]) for f in rep.for_rule("LEDGER")] == [("bypass", "bill.rogue.purge")]


def test_allow_maps_each_entry_to_its_sinks_and_unused_is_per_sink(tmp_path):
    # round 32: allow in a map-form rule names the sinks each entry may reach (it was rule-wide)
    rogue = "from bill.store import LedgerRepo\n\ndef purge(r: LedgerRepo, c):\n    r.record_cancel(c)\n"
    root = _bill(tmp_path, **{"bill/rogue.py": rogue})
    rep = _scan(root, dict(MAP_RULE, allow={"py:bill.rogue.purge": S + "record_cancel"}))
    assert rep.findings == [], rep.findings
    rep = _scan(root, dict(MAP_RULE, allow={"py:bill.rogue.purge": S + "record_cancel",
                                            "py:bill.api.refund_tool": S + "record_refund"}))
    assert [(f.code, f.owner) for f in rep.findings] == [("missing/allow-unused", "bill.api.refund_tool")]


def test_a_shared_unresolved_site_is_reported_once_under_a_mapping(tmp_path):
    root = _bill(tmp_path, **{"bill/plug.py": "import importlib\n\ndef load(n):\n    importlib.import_module(n)\n"})
    rep = _scan(root, MAP_RULE)
    assert [f.code for f in rep.for_rule("LEDGER")] == ["unresolved/reflection"], rep.findings


def test_a_door_named_in_the_mapping_is_a_guarded_door(tmp_path):
    # unwrapping a decorated door skips its checks: the doors the mapping names count as guarded doors
    guard = ("import functools, payments\ndef deco(f):\n    @functools.wraps(f)\n    def w(*a):\n"
             "        return f(*a)\n    return w\n@deco\ndef door(c, a):\n    payments.send_to_gateway(c, a)\n")
    root = write_project(tmp_path, {"payments.py": "def send_to_gateway(c, a):\n    pass\n", "svc.py": guard,
                                    "ops.py": "def f(fn):\n    return fn.__wrapped__\n"})
    rep = scan.scan({"rules": [{"id": "R", "mediation": {"sink": {"py:payments.send_to_gateway": "py:svc.door"}}}]},
                    [root])
    assert "forbidden/unwrap" in [f.code for f in rep.findings], rep.findings


def test_a_missing_door_of_two_sinks_is_reported_once(tmp_path):
    pairs = {S + "record_refund": [D + "gone"], S + "record_cancel": [D + "gone", D + "cancel"],
             S + "insert_credit": [D + "credit"]}
    rep = _scan(_bill(tmp_path), {"id": "LEDGER", "sink": pairs})
    assert [f.code for f in rep.findings if f.kind == "missing"] == ["missing/door"], rep.findings


# --- 1. pairing: the rule file -------------------------------------------------------------------------------
def test_door_may_be_absent_or_the_union_and_nothing_else():
    union = {"id": "R", "sink": {"py:a.s": "py:a.d", "py:a.t": ["py:a.e", "py:a.d"]}, "door": ["py:a.e", "py:a.d"]}
    scan.mediations(expand_short_forms({"rules": [union]}))
    wrong = {"id": "R", "mediation": {"sink": {"py:a.s": "py:a.d"}, "door": ["py:a.d", "py:a.x"]}}
    with pytest.raises(ScanConfigError, match="mediation.door .*exactly the doors the sink mapping"):
        scan.mediations({"rules": [wrong]})


@pytest.mark.parametrize("sink,msg", [
    ({}, "at least one sink"),
    ({"a.s": "py:a.d"}, "'py:' path"),
    ({"py:a.s": "a.d"}, "'py:' path"),
    ({"py:a.s": []}, "'py:' path"),
    ({"py:a.s": 3}, "'py:' path"),
])
def test_a_malformed_sink_mapping_does_not_load(sink, msg):
    with pytest.raises(LoadError, match=msg):
        inv.load({"rules": [{"id": "R", "sink": sink}]}, install=False)
    with pytest.raises(LoadError, match=msg):
        inv.load({"rules": [{"id": "R", "status": "enforce", "bad": "false",
                             "enforcers": [{"point": "structure", "timing": "pre_effect", "mode": "enforce"}],
                             "mediation": {"sink": sink}}]}, install=False)


def test_a_mapping_needs_no_door_and_takes_no_subject_door():
    doc = expand_short_forms({"rules": [{"id": "R", "sink": {"py:a.s": "py:a.d"}}]})
    (med,) = scan.mediations(doc)[0].values()
    assert med.doors == ["py:a.d"] and med.pairs == {"py:a.s": ["py:a.d"]}
    inv.load({"rules": [{"id": "R", "sink": {"py:a.s": "py:a.d"}}]}, install=False)


def test_the_schema_accepts_the_mapping():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(inv.schema_path().read_text())
    for doc in ({"rules": [{"id": "R", "sink": PAIRS}]},
                expand_short_forms({"rules": [{"id": "R", "sink": dict(PAIRS)}]})):
        jsonschema.validate(doc, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"rules": [{"id": "R", "sink": {"py:a": []}}]}, schema)


# --- 1. pairing: the lock ------------------------------------------------------------------------------------
def _med(sink, door=None):
    return {"sink": sink, **({"door": door} if door is not None else {})}


@pytest.mark.parametrize("old,new,weak,what", [
    (_med({"py:s": ["py:a"]}), _med({"py:s": ["py:a", "py:b"]}), True, "py:b may now reach py:s"),
    (_med({"py:s": ["py:a", "py:b"]}), _med({"py:s": ["py:a"]}), False, "py:b no longer reaches py:s"),
    (_med({"py:s": "py:a", "py:t": "py:b"}), _med({"py:s": "py:a"}), True, "no longer checked"),
    (_med({"py:s": "py:a", "py:t": "py:b"}), _med(["py:s", "py:t"], ["py:a", "py:b"]), True, "py:b may now reach py:s"),
    (_med(["py:s", "py:t"], ["py:a", "py:b"]), _med({"py:s": "py:a", "py:t": "py:b"}), False, "no longer reaches"),
    (_med({"py:s": "py:a"}), _med({"py:s": "py:a", "py:t": "py:b"}), False, "mediation.sink"),
])
def test_pairing_changes_are_judged_sink_by_sink(old, new, weak, what):
    got = _mediation_changes("rule R", old, new)
    assert got and any(c.weakened for c in got) == weak, [str(c) for c in got]
    assert what in " ".join(str(c) for c in got), [str(c) for c in got]


def test_the_same_pairing_written_another_way_is_no_weakening():
    a = _med({"py:s": "py:a", "py:t": ["py:b"]})
    assert not any(c.weakened for c in _mediation_changes("rule R", a, _med({"py:t": "py:b", "py:s": ["py:a"]})))
    assert not any(c.weakened for c in _mediation_changes("rule R", a, dict(a, door=["py:a", "py:b"])))
    same = _med({"py:s": ["py:a", "py:b"], "py:t": ["py:a", "py:b"]})
    assert not any(c.weakened for c in _mediation_changes("rule R", same, _med(["py:s", "py:t"], ["py:a", "py:b"])))


def test_the_lock_pins_the_mapping():
    doc = expand_short_forms({"rules": [dict(MAP_RULE)]})
    sem = pinning.rule_semantics(doc["rules"][0])
    assert sem["mediation"]["sink"] == PAIRS
    wider = json.loads(json.dumps(PAIRS))
    wider[S + "record_refund"] = [D + "refund", D + "cancel"]
    sem2 = pinning.rule_semantics(expand_short_forms({"rules": [{"id": "LEDGER", "sink": wider}]})["rules"][0])
    got = changes({"rules": {"LEDGER": {"semantic": sem}}}, {"rules": {"LEDGER": {"semantic": sem2}}})
    assert [c.weakened for c in got] == [True], [str(c) for c in got]


# --- 1. pairing: status, explain ----------------------------------------------------------------------------
def test_status_and_mcp_explain_show_the_pairing():
    from invariants.mcp.tools import _rule_summary
    from invariants.status_report import checks_text
    reg = inv.load({"rules": [{"id": "R", "sink": {"py:a.s": "py:a.d", "py:a.t": ["py:a.e"]}}]}, install=False)
    text = checks_text(reg.rules["R"])
    assert "every call to py:a.s goes through py:a.d, and every call to py:a.t goes through py:a.e" in text, text
    summary = _rule_summary(reg.rules["R"].raw)
    assert summary["pairs"] == {"py:a.s": ["py:a.d"], "py:a.t": ["py:a.e"]}
    assert summary["sinks"] == ["py:a.s", "py:a.t"] and summary["doors"] == ["py:a.d", "py:a.e"]


# --- 1. the support-agent scenario (in a copy: the app's own files are not touched) ------------------------
def _support_copy(tmp_path: pathlib.Path) -> pathlib.Path:
    dst = tmp_path / "support-agent"
    shutil.copytree(SUPPORT / "src", dst / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(SUPPORT / "invariants.yaml", dst / "invariants.yaml")
    return dst


SA = "py:support_agent.repo.LedgerRepo."
SB = "py:support_agent.service.BillingService."
SA_MAPPING = (f'    sink:\n      "{SA}record_refund": ["{SB}refund"]\n      "{SA}insert_credit": ["{SB}credit"]\n'
              f'      "{SA}cancel_subscription": ["{SB}cancel"]\n')


def _paired(text: str) -> str:
    start = text.index("  - id: LEDGER-REPO-DOOR\n")
    end = text.index("\n\n", start)
    return text[:start] + "  - id: LEDGER-REPO-DOOR\n" + SA_MAPPING.rstrip("\n") + text[end:]


def test_support_agent_a_refund_written_in_cancel_is_caught_only_with_the_pairing(tmp_path):
    app = _support_copy(tmp_path)
    svc = app / "src/support_agent/service.py"
    svc.write_text(svc.read_text().replace(
        "        self.ledger.cancel_subscription(customer_id)\n",
        "        self.ledger.cancel_subscription(customer_id)\n"
        "        self.ledger.record_refund(\"goodwill\", customer_id, 500)\n"))
    rules = app / "invariants.yaml"
    rep = scan.scan(read_document(rules), [app / "src"])
    assert rep.rule_result("LEDGER-REPO-DOOR") == "pass", rep.findings          # the list form: any door
    rules.write_text(_paired(rules.read_text()))
    rep = scan.scan(read_document(rules), [app / "src"])
    found = rep.for_rule("LEDGER-REPO-DOOR")
    assert [f.kind for f in found] == ["bypass"], found
    assert found[0].path[-2:] == ["support_agent.service.BillingService.cancel",
                                  "support_agent.repo.LedgerRepo.record_refund"], found[0].path
    assert rep.rule_result("STRIPE-GATEWAY-DOOR") == "pass"


def test_support_agent_paired_rule_passes_the_apps_own_code(tmp_path):
    app = _support_copy(tmp_path)
    (app / "invariants.yaml").write_text(_paired((app / "invariants.yaml").read_text()))
    rep = scan.scan(read_document(app / "invariants.yaml"), [app / "src"])
    assert rep.rule_result("LEDGER-REPO-DOOR") == "pass", rep.findings


def test_init_drafts_the_mapping_for_the_support_agent(tmp_path):
    app = _support_copy(tmp_path)
    found = doors.discover([app / "src"])
    d = next(d for d in found.doors if d.resource == "support_agent.repo.LedgerRepo")
    assert d.pairs == {"support_agent.repo.LedgerRepo.cancel_subscription": ["support_agent.service.BillingService.cancel"],
                       "support_agent.repo.LedgerRepo.insert_credit": ["support_agent.service.BillingService.credit"],
                       "support_agent.repo.LedgerRepo.record_refund": ["support_agent.service.BillingService.refund"]}
    text = "\n".join(doors.render(found))
    rule = yaml.safe_load("rules:\n" + text)["rules"]
    mine = next(r for r in rule if r["id"] == d.rid)
    assert "door" not in mine and mine["sink"] == {f"py:{k}": [f"py:{x}" for x in v] for k, v in d.pairs.items()}
    rep = scan.scan(expand_short_forms({"rules": [mine]}), [app / "src"])
    assert rep.rule_result(d.rid) == "pass", rep.findings


def test_init_keeps_the_list_form_when_every_write_has_the_same_doors(tmp_path):
    root = write_project(tmp_path, {
        "p/__init__.py": "", "p/store.py": "def a(session, n):\n    session.add(n)\n"
                                           "def b(session, n):\n    session.merge(n)\n",
        "p/service.py": "from p.store import a, b\ndef put(c, n):\n    a(c, n)\n    b(c, n)\n",
        "p/api.py": "from p.service import put\ndef x(c): put(c, 1)\ndef y(c): put(c, 2)\n"})
    (d,) = doors.discover([root]).doors
    assert d.pairs is None and d.rule()["door"] == "py:p.service.put"


def test_init_pairs_a_doorless_repository_write_with_every_door(tmp_path):
    root = _bill(tmp_path, **{"bill/store.py": STORE + "    def purge_all(self):\n        pass\n"})
    (d,) = doors.discover([root]).doors
    assert d.pairs is not None and sorted(d.pairs["bill.store.LedgerRepo.purge_all"]) == sorted(d.doors)


# --- 2. inv init writes the scan settings --------------------------------------------------------------------
LIB = {"lib/shop/__init__.py": "",
       "lib/shop/store.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n",
       "lib/shop/order_service.py": "from shop.store import OrderRepo\n"
                                    "def place(r: OrderRepo, o):\n    r.save_order(o)\n",
       "lib/shop/api.py": "from shop.order_service import place\ndef a(r, o):\n    place(r, o)\n"
                          "def b(r, o):\n    place(r, o)\n",
       # a back door in code init is told to leave out: `inv check` must leave it out too
       "lib/shop/legacy/__init__.py": "",
       "lib/shop/legacy/fix.py": "from shop.store import OrderRepo\ndef fix(r: OrderRepo):\n    r.save_order(1)\n"}


def test_init_writes_root_and_exclude_and_check_scans_the_same(tmp_path):
    root = write_project(tmp_path / "p", LIB)
    r = run_inv("init", "--root", "lib", "--exclude", "*/legacy/*", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["invariants"]["scan"]
    assert cfg == {"roots": ["lib"], "extend_exclude": ["*/legacy/*"]}, cfg
    assert "wrote [tool.invariants.scan]" in r.stdout and 'roots = ["lib"]' in r.stdout, r.stdout
    assert "drafts were scanned with" not in r.stdout
    text = (root / "invariants.yaml").read_text()
    assert "- id: ORDER-REPO-DOOR" in text and "allow:" not in text, text
    lock = json.loads((root / "invariants.lock").read_text())
    assert lock["scan"]["roots"] == ["lib"] and "*/legacy/*" in lock["scan"]["exclude"]
    c = run_inv("check", cwd=root)
    assert c.returncode == 0, c.stdout + c.stderr
    (root / "lib/shop/promo.py").write_text("from shop.store import OrderRepo\ndef p(r: OrderRepo):\n"
                                            "    r.save_order(2)\n")
    c = run_inv("check", cwd=root)
    assert c.returncode == 1 and "shop.promo.p" in c.stdout, c.stdout


def test_init_appends_to_a_pyproject_and_keeps_it_byte_for_byte(tmp_path):
    root = write_project(tmp_path / "p", LIB)
    before = b'[project]\r\nname = "p"\r\nversion = "0"\r\n\r\n[tool.ruff]\r\nline-length = 100  # keep\r\n'
    (root / "pyproject.toml").write_bytes(before)
    r = run_inv("init", "--no-lock", "--root", "lib", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    after = (root / "pyproject.toml").read_bytes()
    assert after.startswith(before) and b"\r\n[tool.invariants.scan]\r\nroots = [\"lib\"]\r\n" in after, after
    assert b"\n" not in after.replace(b"\r\n", b"")
    cfg = tomllib.loads(after.decode())
    assert cfg["tool"]["ruff"] == {"line-length": 100} and cfg["tool"]["invariants"]["scan"] == {"roots": ["lib"]}


def test_init_adds_a_missing_key_under_an_existing_scan_table(tmp_path):
    root = write_project(tmp_path / "p", LIB)
    before = '[tool.invariants.scan]  # ours\nextend_exclude = ["*/legacy/*"]\n\n[tool.other]\nx = 1\n'
    (root / "pyproject.toml").write_text(before)
    r = run_inv("init", "--no-lock", "--root", "lib", "--exclude", "*/legacy/*", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    after = (root / "pyproject.toml").read_text()
    assert after == before.replace("# ours\n", '# ours\nroots = ["lib"]\n'), after


def test_init_never_overwrites_scan_settings_that_differ(tmp_path):
    root = write_project(tmp_path / "p", LIB)
    before = '[tool.invariants.scan]\nroots = ["elsewhere"]\n'
    (root / "pyproject.toml").write_text(before)
    (root / "elsewhere").mkdir()
    r = run_inv("init", "--no-lock", "--root", "lib", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "pyproject.toml").read_text() == before
    assert 'roots = ["elsewhere"]' in r.stdout and 'roots = ["lib"]' in r.stdout and "left" in r.stdout, r.stdout


def test_init_says_nothing_new_when_the_settings_are_there(tmp_path):
    root = write_project(tmp_path / "p", LIB)
    before = '[tool.invariants.scan]\nroots = ["lib"]\n'
    (root / "pyproject.toml").write_text(before)
    r = run_inv("init", "--no-lock", "--root", "lib", cwd=root)
    assert r.returncode == 0 and (root / "pyproject.toml").read_text() == before
    assert "already" in r.stdout and "wrote [tool.invariants.scan]" not in r.stdout, r.stdout


def test_init_without_root_or_exclude_leaves_pyproject_alone(tmp_path):
    root = write_project(tmp_path / "p", {k[4:]: v for k, v in LIB.items()})
    r = run_inv("init", "--no-lock", cwd=root)
    assert r.returncode == 0 and not (root / "pyproject.toml").exists(), r.stdout


def test_scan_table_text_unit():
    from invariants.project import add_scan_settings
    assert add_scan_settings("", {"roots": ["src"]}) == '[tool.invariants.scan]\nroots = ["src"]\n'
    assert add_scan_settings("x = 1", {"roots": ["a"], "extend_exclude": ["b"]}) == \
        'x = 1\n\n[tool.invariants.scan]\nroots = ["a"]\nextend_exclude = ["b"]\n'
    # a scan table given as dotted keys cannot be edited in place: None (say what to add)
    assert add_scan_settings('[tool.invariants]\nscan.extend_exclude = ["b"]\n', {"roots": ["a"]}) is None


# --- 3. one allow_unresolved entry for several rules --------------------------------------------------------
PAY = "def send(c, a):\n    pass\n\ndef refund(c, a):\n    send(c, a)\n"
LEDGER = "def write(x):\n    pass\n\ndef post(x):\n    write(x)\n"
PLUG = "import importlib\n\ndef load(n):\n    importlib.import_module(n)\n"


def _shared_project(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    root = write_project(tmp_path / "src", {"payments.py": PAY, "ledger.py": LEDGER, "plug.py": PLUG})
    rep = scan.scan({"rules": [{"id": "A", "mediation": {"sink": "py:payments.send", "door": "py:payments.refund"}}]},
                    [root])
    (f,) = rep.findings
    return root, f.sha


def _rules_yaml(sha: str, rules: str = "[A, B]", extra: str = "") -> str:
    return ("rules:\n"
            "  - {id: A, door: py:payments.refund, sink: py:payments.send}\n"
            "  - {id: B, door: py:ledger.post, sink: py:ledger.write}\n"
            "  - {id: V, guard: py:payments.refund, bad: 'false'}\n"
            f"{extra}"
            "allow_unresolved:\n"
            f"  - {{at: plug.py:4, code: unresolved/reflection, sha: {sha}, why: plugins are ours, rules: {rules}}}\n")


def test_one_entry_accepts_its_site_under_each_rule_it_lists(tmp_path):
    root, sha = _shared_project(tmp_path)
    f = tmp_path / "invariants.yaml"
    f.write_text(_rules_yaml(sha))
    rep = scan.scan(read_document(f), [root])
    assert rep.findings == [] and sorted(x.rule for x in rep.accepted) == ["A", "B"], rep.findings
    assert all(x.accepted_by == "the allow_unresolved entry plug.py:4 shared by rules A, B" for x in rep.accepted)
    text = scan.to_text(rep)
    assert "shared by rules A, B" in text and "A: pass (1 accepted)" in text, text
    js = rep.as_dict()
    assert {a["accepted_by"] for a in js["accepted"]} == {"the allow_unresolved entry plug.py:4 shared by rules A, B"}
    sarif = scan.to_sarif(rep)
    sup = [r["suppressions"][0] for r in sarif["runs"][0]["results"]]
    assert all("shared by rules A, B" in s["justification"] for s in sup) and len(sup) == 2, sup


def test_a_shared_entry_unused_under_one_rule_is_unused_there(tmp_path):
    root, sha = _shared_project(tmp_path)
    # B's own entry accepts the site first: the shared entry is unused under B only (one finding per entry)
    f = tmp_path / "invariants.yaml"
    f.write_text(_rules_yaml(sha, "[A, B]").replace(
        "  - {id: B, door: py:ledger.post, sink: py:ledger.write}\n",
        "  - id: B\n    door: py:ledger.post\n    sink: py:ledger.write\n"
        f"    allow_unresolved: [{{at: plug.py:4, code: unresolved/reflection, sha: {sha}, why: mine}}]\n"))
    rep = scan.scan(read_document(f), [root])
    assert [(x.rule, x.code) for x in rep.findings] == [("B", "missing/allow-unresolved-unused")], rep.findings
    assert sorted((x.rule, x.accepted_by is None) for x in rep.accepted) == [("A", False), ("B", True)]
    # an entry whose sha is not that site's: unused under every rule it lists, once per rule
    f.write_text(_rules_yaml("0123456789abcdef", "[A, B]"))
    rep = scan.scan(read_document(f), [root])
    unused = sorted(x.rule for x in rep.findings if x.code == "missing/allow-unresolved-unused")
    assert unused == ["A", "B"], rep.findings


def test_a_shared_entry_beside_a_per_rule_one(tmp_path):
    root, sha = _shared_project(tmp_path)
    f = tmp_path / "invariants.yaml"
    f.write_text(_rules_yaml(sha, "[A]").replace(
        "  - {id: B, door: py:ledger.post, sink: py:ledger.write}\n",
        "  - id: B\n    door: py:ledger.post\n    sink: py:ledger.write\n"
        f"    allow_unresolved: [{{at: plug.py:4, code: unresolved/reflection, sha: {sha}, why: mine}}]\n"))
    rep = scan.scan(read_document(f), [root])
    assert rep.findings == [] and {x.rule: x.accepted_by for x in rep.accepted} == {
        "A": "the allow_unresolved entry plug.py:4 shared by rules A", "B": None}
    assert "(allow_unresolved: mine)" in scan.to_text(rep)          # a rule's own entry: shown as before


@pytest.mark.parametrize("rules,msg", [
    ("[A, Z]", "allow_unresolved\\[0\\]: rule Z does not exist"),
    ("[A, V]", "allow_unresolved\\[0\\]: rule V is not a path law"),
    ("[]", "allow_unresolved\\[0\\]: rules must list"),
    ("A", "allow_unresolved\\[0\\]: rules must list"),
    ("[A, A]", "allow_unresolved\\[0\\]: rule A is listed twice"),
])
def test_a_shared_entry_names_existing_path_laws(tmp_path, rules, msg):
    f = tmp_path / "invariants.yaml"
    f.write_text(_rules_yaml("0123456789abcdef", rules))
    with pytest.raises(LoadError, match=msg):
        read_document(f)


def test_a_shared_entry_is_checked_like_a_rules_own(tmp_path):
    f = tmp_path / "invariants.yaml"
    f.write_text(_rules_yaml("abc", "[A]"))
    with pytest.raises(LoadError, match="allow_unresolved\\[0\\]: sha must be the 16 hex digits"):
        read_document(f)
    f.write_text("rules:\n  - {id: A, door: py:payments.refund, sink: py:payments.send}\n"
                 "allow_unresolved:\n  - {at: plug.py:4, code: unresolved/reflection, sha: 0123456789abcdef, why: w}\n")
    with pytest.raises(LoadError, match="allow_unresolved\\[0\\]: an entry here names its rules"):
        read_document(f)
    f.write_text("rules: []\nallow_unresolved: {at: x}\n")
    with pytest.raises(LoadError, match="allow_unresolved must be a list"):
        read_document(f)


def test_a_dict_source_takes_the_shared_form_too():
    doc = {"rules": [{"id": "A", "door": "py:payments.refund", "sink": "py:payments.send"}],
           "allow_unresolved": [{"at": "plug.py:4", "code": "unresolved/reflection", "sha": "0123456789abcdef",
                                 "why": "w", "rules": ["A"]}]}
    reg = inv.load(doc, install=False)
    assert reg.rules["A"].raw["mediation"]["allow_unresolved"][0]["rules"] == ["A"]
    assert "allow_unresolved" in doc                       # the caller's dict is not changed


def _sem(path: pathlib.Path) -> dict:
    return {"rules": {r["id"]: {"semantic": pinning.rule_semantics(r)} for r in read_document(path)["rules"]}}


def test_the_lock_pins_shared_entries_rule_by_rule(tmp_path):
    root, sha = _shared_project(tmp_path)
    f = tmp_path / "invariants.yaml"
    f.write_text(_rules_yaml(sha, "[A]"))
    one = _sem(f)
    f.write_text(_rules_yaml(sha, "[A, B]"))
    two = _sem(f)
    got = changes(one, two)
    assert [(c.where, c.weakened) for c in got if c.weakened] == [("rule B", True)], [str(c) for c in got]
    assert not any(c.weakened for c in changes(two, one))              # removing B from rules: a tightening
    # adding an entry is a weakening; the same site moved from a rule's own list to the shared one is not
    f.write_text(_rules_yaml(sha, "[A]").split("allow_unresolved:")[0])
    none = _sem(f)
    assert [c.where for c in changes(none, two) if c.weakened] == ["rule A", "rule B"]
    f.write_text(_rules_yaml(sha, "[A]").split("allow_unresolved:")[0].replace(
        "  - {id: B, door: py:ledger.post, sink: py:ledger.write}\n",
        "  - id: B\n    door: py:ledger.post\n    sink: py:ledger.write\n"
        f"    allow_unresolved: [{{at: plug.py:4, code: unresolved/reflection, sha: {sha}, why: plugins are ours}}]\n")
        .replace("  - {id: A, door: py:payments.refund, sink: py:payments.send}\n",
                 "  - id: A\n    door: py:payments.refund\n    sink: py:payments.send\n"
                 f"    allow_unresolved: [{{at: plug.py:4, code: unresolved/reflection, sha: {sha}, why: plugins are ours}}]\n"))
    own = _sem(f)
    moved = changes(own, two)
    assert moved and not any(c.weakened for c in moved), [str(c) for c in moved]
    assert "now shared" in " ".join(str(c) for c in moved)


def test_init_drafts_a_site_seen_under_several_rules_once(tmp_path):
    root = write_project(tmp_path, {
        "app/__init__.py": "",
        "app/repo.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n"
                       "class UserRepo:\n    def save_user(self, u):\n        pass\n",
        "app/order_service.py": "from app.repo import OrderRepo\ndef place(o):\n    OrderRepo().save_order(o)\n",
        "app/user_service.py": "from app.repo import UserRepo\ndef join(u):\n    UserRepo().save_user(u)\n",
        "app/api.py": "from app.order_service import place\nfrom app.user_service import join\n"
                      "def a(o):\n    place(o)\ndef b(o):\n    place(o)\ndef c(u):\n    join(u)\ndef d(u):\n    join(u)\n",
        "app/plug.py": "import importlib\ndef load(n):\n    importlib.import_module(n)\n"})
    found = doors.discover([root])
    assert len(found.doors) == 2 and all(len(d.unresolved) == 1 for d in found.doors)
    body = "\n".join(doors.render(found))
    shared = "\n".join(doors.render_shared(found))
    assert "allow_unresolved:" not in body and "is in allow_unresolved below" in body, body
    ids = sorted(d.rid for d in found.doors)
    assert f"rules: [{', '.join(ids)}]" in shared and shared.count("at: ") == 1, shared
    # uncommented (with a why written and the rules uncommented), the file loads and both rules pass
    sha = found.doors[0].unresolved[0][2]
    rules = [json.loads(json.dumps(d.rule())) for d in found.doors]
    doc = {"rules": rules, "allow_unresolved": [{"at": found.doors[0].unresolved[0][0], "code": "unresolved/reflection",
                                                 "sha": sha, "why": "reviewed", "rules": ids}]}
    rep = scan.scan(expand_short_forms(doc), [root])
    assert rep.findings == [] and len(rep.accepted) == 2, rep.findings


def test_a_scan_header_inside_a_string_is_not_edited():
    from invariants.project import add_scan_settings
    text = '[tool.invariants]\nscan.extend_exclude = ["b"]\nx = """\n[tool.invariants.scan]\n"""\n'
    assert add_scan_settings(text, {"roots": ["a"]}) is None


def test_a_shared_entry_refuses_a_rule_with_mediation_but_no_sink(tmp_path):
    f = tmp_path / "invariants.yaml"
    f.write_text(_rules_yaml("0123456789abcdef", "[A, W]", extra=(
        "  - {id: W, status: detect, bad: 'false', mediation: {chokepoint: words}}\n")))
    with pytest.raises(LoadError, match="allow_unresolved\\[0\\]: rule W is not a path law"):
        read_document(f)


def test_a_rules_own_entry_with_rules_must_name_it():
    entry = {"at": "a.py:1", "code": "unresolved/reflection", "sha": "0123456789abcdef", "why": "w"}
    doc = {"rules": [{"id": "A", "mediation": {"sink": "py:a.s", "door": "py:a.d",
                                               "allow_unresolved": [dict(entry, rules=["B"])]}}]}
    with pytest.raises(ScanConfigError, match="rules lists the rules a shared entry"):
        scan.mediations(doc)
    doc["rules"][0]["mediation"]["allow_unresolved"] = [dict(entry, rules=["A"])]
    (med,) = scan.mediations(doc)[0].values()
    assert med.accept is not None and med.accept[0].rules == ("A",)
