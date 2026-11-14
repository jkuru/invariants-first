"""Round 32: allow, in a rule whose sink is a mapping, maps each allowed function to the sink(s) it may
reach without a door.

With door-to-sink pairing (round 30) allow was rule-wide: a back door frozen for one sink could later
write any other sink of the mapping and pass. Now:

1. Format: in a map-form rule, `allow:` (and mediation.allow) maps each allowed function to the sink or
   sinks it may reach (`allow: {py:admin.fix: [py:store.record_refund]}`); a plain list there is a
   LoadError showing the map form. Each listed sink is a key of the sink mapping; each entry names one.
   List-form rules keep the plain list.
2. Scan: an entry excuses only the sinks it lists; a path from it to another sink is a bypass. An entry
   (or one of its listed sinks) no longer reached without a door is missing/allow-unused naming the sink.
3. Lock: adding an entry, or a sink to an entry, is WEAKENED; removing either is a tightening.
4. `inv init` drafts the map, each frozen back door with the sinks it reaches.
5. `inv status` and MCP explain show the sinks each entry excuses.
"""
from __future__ import annotations

import json
import pathlib

import pytest
import yaml
from cli_support import write_project

import invariants as inv
from invariants import doors, scan
from invariants.errors import LoadError
from invariants.loader import expand_short_forms
from invariants.lock import changes, pinning
from invariants.lock.rule_diff import _mediation_changes
from invariants.scan import ScanConfigError

REPO = "class LedgerRepo:\n" \
       "    def record_refund(self, x): pass\n" \
       "    def cancel_subscription(self, x): pass\n" \
       "    def insert_credit(self, x): pass\n"
SERVICE = ("from app.repo import LedgerRepo\n"
           "class BillingService:\n"
           "    def __init__(self): self.repo = LedgerRepo()\n"
           "    def refund(self, x): self.repo.record_refund(x)\n"
           "    def cancel(self, x): self.repo.cancel_subscription(x)\n"
           "    def credit(self, x): self.repo.insert_credit(x)\n")
ROUTES = ("from app.service import BillingService\n"
          "svc = BillingService()\n"
          "def r1(x): svc.refund(x)\n"
          "def r2(x): svc.cancel(x)\n"
          "def r3(x): svc.credit(x)\n"
          "def r4(x): svc.refund(x); svc.cancel(x)\n")
ADMIN_ONE = "from app.repo import LedgerRepo\ndef fix(x):\n    LedgerRepo().record_refund(x)\n"
ADMIN_ALL = ("from app.repo import LedgerRepo\ndef fix(x):\n    LedgerRepo().record_refund(x)\n"
             "    LedgerRepo().cancel_subscription(x)\n    LedgerRepo().insert_credit(x)\n")

R, SV = "py:app.repo.LedgerRepo.", "py:app.service.BillingService."
REFUND, CANCEL, CREDIT = R + "record_refund", R + "cancel_subscription", R + "insert_credit"
PAIRS = {CANCEL: [SV + "cancel"], CREDIT: [SV + "credit"], REFUND: [SV + "refund"]}
FIX = "py:app.admin.fix"


def _app(tmp_path: pathlib.Path, admin: str = ADMIN_ONE) -> pathlib.Path:
    return write_project(tmp_path, {"app/__init__.py": "", "app/repo.py": REPO, "app/service.py": SERVICE,
                                    "app/routes.py": ROUTES, "app/admin.py": admin})


def _rule(allow=None) -> dict:
    return {"id": "LEDGER", "sink": json.loads(json.dumps(PAIRS)), **({"allow": allow} if allow is not None else {})}


def _scan(root: pathlib.Path, rule: dict) -> scan.Report:
    return scan.scan(expand_short_forms({"rules": [rule]}), [root])


# --- 1. format -----------------------------------------------------------------------------------------------
def test_a_plain_list_allow_in_a_map_rule_does_not_load():
    with pytest.raises(LoadError, match=r"allow: \{py:"):
        inv.load({"rules": [_rule([FIX])]}, install=False)
    long = {"id": "LEDGER", "status": "enforce", "bad": "false",
            "enforcers": [{"point": "structure", "timing": "pre_effect", "mode": "enforce"}],
            "mediation": {"sink": PAIRS, "allow": [FIX]}}
    with pytest.raises(LoadError, match=r"mediation.allow.*allow: \{py:"):
        inv.load({"rules": [long]}, install=False)
    with pytest.raises(ScanConfigError, match=r"allow: \{py:"):
        scan.mediations({"rules": [{"id": "L", "mediation": {"sink": PAIRS, "allow": FIX}}]})


@pytest.mark.parametrize("allow,msg", [
    ({FIX: [R + "nope"]}, "not a sink of this rule"),
    ({FIX: []}, "non-empty"),
    ({FIX: 3}, "non-empty"),
    ({"app.admin.fix": [REFUND]}, "'py:'"),
    ({FIX: ["app.repo.x"]}, "'py:'"),
])
def test_a_malformed_allow_map_does_not_load(allow, msg):
    with pytest.raises(LoadError, match=msg):
        inv.load({"rules": [_rule(allow)]}, install=False)


def test_the_map_form_loads_and_a_string_value_is_one_sink():
    reg = inv.load({"rules": [_rule({FIX: REFUND})]}, install=False)
    assert reg.rules["LEDGER"].raw["mediation"]["allow"] == {FIX: [REFUND]}
    (med,) = scan.mediations(expand_short_forms({"rules": [_rule({FIX: REFUND})]}))[0].values()
    assert med.allow == [FIX] and med.allow_to == {FIX: [REFUND]}
    inv.load({"rules": [_rule({})]}, install=False)          # emptied: none left


def test_a_list_form_rule_keeps_the_plain_list_and_refuses_the_map():
    rule = {"id": "L", "door": SV + "refund", "sink": REFUND}
    inv.load({"rules": [dict(rule, allow=[FIX])]}, install=False)
    with pytest.raises(LoadError, match="allow"):
        inv.load({"rules": [dict(rule, allow={FIX: [REFUND]})]}, install=False)


def test_the_schema_takes_the_allow_map():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(inv.schema_path().read_text())
    for doc in ({"rules": [_rule({FIX: [REFUND]})]}, expand_short_forms({"rules": [_rule({FIX: REFUND})]})):
        jsonschema.validate(doc, schema)


# --- 2. scan -------------------------------------------------------------------------------------------------
def test_a_back_door_frozen_for_one_sink_writing_another_is_a_bypass(tmp_path):
    root = _app(tmp_path, ADMIN_ALL)
    rep = _scan(root, _rule({FIX: [REFUND]}))
    found = rep.for_rule("LEDGER")
    assert sorted((f.kind, f.path[-1]) for f in found) == [
        ("bypass", CANCEL[3:]), ("bypass", CREDIT[3:])], found
    assert rep.rule_result("LEDGER") == "fail"


def test_an_entry_listing_every_sink_it_writes_passes(tmp_path):
    root = _app(tmp_path, ADMIN_ALL)
    rep = _scan(root, _rule({FIX: [REFUND, CANCEL, CREDIT]}))
    assert rep.findings == [] and rep.rule_result("LEDGER") == "pass", rep.findings
    assert rep.as_dict()["rules"]["LEDGER"]["allow_to"] == {FIX: [REFUND, CANCEL, CREDIT]}


def test_an_entry_for_one_sink_passes_when_it_writes_only_that(tmp_path):
    rep = _scan(_app(tmp_path), _rule({FIX: REFUND}))
    assert rep.findings == [] and rep.rule_result("LEDGER") == "pass", rep.findings


def test_a_listed_sink_no_longer_reached_is_unused_naming_the_sink(tmp_path):
    rep = _scan(_app(tmp_path), _rule({FIX: [REFUND, CANCEL]}))
    found = rep.for_rule("LEDGER")
    assert [(f.code, f.owner) for f in found] == [("missing/allow-unused", "app.admin.fix")], found
    assert CANCEL in found[0].message and REFUND not in found[0].message, found[0].message


def test_an_entry_reaching_none_of_its_sinks_is_unused_for_each(tmp_path):
    rep = _scan(_app(tmp_path), _rule({"py:app.routes.r2": [REFUND, CREDIT]}))
    msgs = sorted(f.message for f in rep.for_rule("LEDGER") if f.code == "missing/allow-unused")
    assert len(msgs) == 2 and CREDIT in msgs[0] + msgs[1] and REFUND in msgs[0] + msgs[1], msgs


def test_a_door_of_another_sink_allowed_for_a_sink_it_writes(tmp_path):
    # cancel (the door of cancel_subscription) also writes a refund: allowed for that sink only
    root = write_project(tmp_path, {"app/__init__.py": "", "app/repo.py": REPO, "app/routes.py": ROUTES,
                                    "app/admin.py": ADMIN_ONE, "app/service.py": SERVICE.replace(
                                        "    def cancel(self, x): self.repo.cancel_subscription(x)\n",
                                        "    def cancel(self, x):\n        self.repo.cancel_subscription(x)\n"
                                        "        self.repo.record_refund(x)\n")})
    assert _scan(root, _rule({FIX: REFUND})).rule_result("LEDGER") == "fail"
    chain = {SV + "cancel": REFUND, "py:app.routes.r2": REFUND, "py:app.routes.r4": REFUND}   # frozen with it
    rep = _scan(root, _rule({FIX: REFUND, **chain}))
    assert rep.rule_result("LEDGER") == "pass", rep.findings


# --- 3. lock -------------------------------------------------------------------------------------------------
def _med(allow):
    return {"sink": PAIRS, **({"allow": allow} if allow is not None else {})}


@pytest.mark.parametrize("old,new,weak", [
    (None, {FIX: [REFUND]}, True),                               # an entry added
    ({FIX: [REFUND]}, {FIX: [REFUND, CANCEL]}, True),            # a sink added to an entry
    ({FIX: [REFUND]}, {FIX: [REFUND], "py:x.y": [CANCEL]}, True),
    ({FIX: [REFUND, CANCEL]}, {FIX: [REFUND]}, False),           # a sink removed
    ({FIX: [REFUND]}, None, False),                              # an entry removed
    ([FIX], {FIX: [REFUND]}, False),                             # the old rule-wide list: every sink
    ({FIX: [REFUND]}, [FIX], True),
])
def test_allow_map_changes_are_judged_entry_by_entry(old, new, weak):
    got = _mediation_changes("rule R", _med(old), _med(new))
    assert got and any(c.weakened for c in got) == weak, [str(c) for c in got]
    assert all("mediation.allow" in str(c) for c in got), [str(c) for c in got]


def test_the_same_allow_map_written_another_way_is_no_change():
    assert _mediation_changes("rule R", _med({FIX: REFUND}), _med({FIX: [REFUND]})) == []
    assert _mediation_changes("rule R", _med({FIX: [REFUND, CANCEL]}), _med({FIX: [CANCEL, REFUND]})) == []


def test_the_lock_pins_the_allow_map():
    sem = pinning.rule_semantics(expand_short_forms({"rules": [_rule({FIX: REFUND})]})["rules"][0])
    assert sem["mediation"]["allow"] == {FIX: [REFUND]}
    sem2 = pinning.rule_semantics(expand_short_forms({"rules": [_rule({FIX: [REFUND, CREDIT]})]})["rules"][0])
    got = changes({"rules": {"LEDGER": {"semantic": sem}}}, {"rules": {"LEDGER": {"semantic": sem2}}})
    assert [c.weakened for c in got] == [True] and CREDIT in str(got[0]), [str(c) for c in got]


# --- 4. inv init ---------------------------------------------------------------------------------------------
def _drafted(root: pathlib.Path) -> tuple[doors.Door, dict]:
    found = doors.discover([root])
    (d,) = found.doors
    rules = yaml.safe_load("rules:\n" + "\n".join(doors.render(found)))["rules"]
    return d, next(r for r in rules if r["id"] == d.rid)


def test_init_drafts_the_allow_map_with_the_sinks_each_back_door_reaches(tmp_path):
    root = _app(tmp_path)
    d, rule = _drafted(root)
    assert d.pairs is not None and d.active, d.why_inactive
    assert rule["allow"] == {FIX: [REFUND]}, rule
    assert _scan(root, rule).rule_result(d.rid) == "pass"


def test_init_lists_every_sink_a_back_door_reaches(tmp_path):
    root = _app(tmp_path, ADMIN_ALL)
    d, rule = _drafted(root)
    assert {k: sorted(v) for k, v in rule["allow"].items()} == {FIX: sorted([REFUND, CANCEL, CREDIT])}, rule
    assert _scan(root, rule).rule_result(d.rid) == "pass"


# --- 5. status, explain --------------------------------------------------------------------------------------
def test_status_and_explain_show_what_each_entry_excuses():
    from invariants.mcp.tools import _rule_summary
    from invariants.status_report import checks_text
    reg = inv.load({"rules": [_rule({FIX: REFUND})]}, install=False)
    text = checks_text(reg.rules["LEDGER"])
    assert f"{FIX} may reach {REFUND} without a door" in text, text
    summary = _rule_summary(reg.rules["LEDGER"].raw)
    assert summary["allow"] == [FIX] and summary["allow_to"] == {FIX: [REFUND]}


def test_standing_names_the_sinks_a_live_back_door_reaches():
    from invariants.registry.coverage import _mediation_problems
    reg = inv.load({"rules": [{"id": "J", "sink": {"py:json.loads": "py:json.load", "py:json.dumps": "py:json.dump"},
                               "allow": {"py:json.decoder.JSONDecoder.decode": "py:json.loads"}}]}, install=False)
    why = [w for w in _mediation_problems(reg.rules["J"]) if w.startswith("mediation.allow keeps")]
    assert why == ["mediation.allow keeps 1 live back door(s) to the sink: py:json.decoder.JSONDecoder.decode "
                   "(to py:json.loads) (remove them, or declare each in mediation.residual_paths)"], why
