"""The bundled JSON Schema agrees with the loader."""
from __future__ import annotations

import json

import jsonschema
import pytest

import invariants as inv
from support import FIXTURES, RULES, load_error, read_doc, rule, variant

SCHEMA = json.loads(inv.schema_path().read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("path", sorted(p.name for p in FIXTURES.iterdir() if p.suffix in (".json", ".yaml", ".yml")))
def test_every_example_rule_file_validates(path):
    VALIDATOR.validate(read_doc(FIXTURES / path))


@pytest.mark.parametrize("app", ["clean", "bypass"])
def test_cli_example_rule_files_validate(app):
    VALIDATOR.validate(read_doc(FIXTURES / "cli" / app / "invariants.yaml"))


INVALID = {
    "unknown rule field": lambda d: rule(d, "R-1").update(severity="high"),
    "unknown top-level field": lambda d: d.update(extra=1),
    "effects on a rule": lambda d: rule(d, "R-1").update(effects="none"),
    "missing status": lambda d: rule(d, "R-1").pop("status"),
    "missing bad": lambda d: rule(d, "R-1").pop("bad"),
    "missing id": lambda d: rule(d, "R-1").pop("id"),
    "bad status": lambda d: rule(d, "R-1").update(status="approved"),
    "empty bad list": lambda d: rule(d, "R-1").update(bad=[]),
    "clause without when": lambda d: rule(d, "R-1").update(bad=[{"message": "x"}]),
    "enforcer without mode": lambda d: rule(d, "R-1")["enforcers"][0].pop("mode"),
    "bad mode": lambda d: rule(d, "R-1")["enforcers"][0].update(mode=False),
    "non-edge with target": lambda d: rule(d, "R-1")["enforcers"][0].update(target="py:jobs.split_work"),
    "edge without target": lambda d: rule(d, "R-7")["enforcers"][1].pop("target"),
    "rows on a decorator": lambda d: rule(d, "R-1")["enforcers"][0].update(view="rows"),
    "view with bad root": lambda d: rule(d, "R-20")["enforcers"][0]["view"].update(n="argz.slots"),
    "bind with bad root": lambda d: rule(d, "R-21")["bind"].update(req="request.req"),
    "on_exhaust in policy": lambda d: rule(d, "R-7")["model_policy"].update(on_exhaust="escalate"),
    "semantic feedback": lambda d: rule(d, "R-7")["model_policy"]["feedback"].update(semantic="hint"),
    "abstain without value": lambda d: rule(d, "R-7")["model_policy"]["abstain"].pop("value"),
    "negative retries": lambda d: rule(d, "R-7")["model_policy"].update(format_retries=-1),
    "held_data with resample": lambda d: rule(d, "R-7")["model_policy"].update(semantic_resamples=1),
    "target without effects": lambda d: d["targets"]["py:jobs.split_work"].pop("effects"),
    "transactional without transaction": lambda d: d["targets"]["py:pool.PoolService.resize_door"].pop("transaction"),
    "transaction on non-transactional": lambda d: d["targets"]["py:jobs.split_work"].update(
        transaction="py:pool.PoolService.txn"),
    "provider without consistency": lambda d: d["providers"]["job"].pop("consistency"),
    "bad consistency": lambda d: d["providers"]["job"].update(consistency="eventual"),
    "unknown mediation field": lambda d: rule(d, "R-1").update(mediation={"gate": "x"}),
    "sink not a py path": lambda d: rule(d, "R-1").update(mediation={"sink": "jobs.split_work"}),
    "empty door list": lambda d: rule(d, "R-1").update(mediation={"sink": "py:jobs.x", "door": []}),
    "tables not a list": lambda d: rule(d, "R-1").update(mediation={"sink": "py:jobs.x", "tables": "pools"}),
    "applies_to not a pattern": lambda d: rule(d, "R-1").update(applies_to=3),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_schema_and_loader_both_reject(name):
    doc = variant(INVALID[name])
    assert list(VALIDATOR.iter_errors(doc)), f"schema accepts: {name}"
    assert load_error(INVALID[name]) is not None, f"loader accepts: {name}"


VALID_VARIANTS = {
    "single string bad": lambda d: rule(d, "P-2").update(bad="size(args.job_id) == 0"),
    "defaults for on_violation/on_error": lambda d: [(e.pop("on_violation"), e.pop("on_error"))
                                                     for e in rule(d, "P-2")["enforcers"]],
    "scan mediation fields": lambda d: rule(d, "R-1").update(
        mediation={"chokepoint": "py:jobs.split_work", "sink": ["py:jobs.x"], "door": "py:jobs.split_work",
                   "tables": ["pools"]}, applies_to=["py:jobs.*"]),
    "no targets or providers": lambda d: (d.pop("targets"), d.pop("providers"), d.update(rules=[
        {"id": "C-1", "status": "candidate", "bad": "args.x > 1"}])),
}


@pytest.mark.parametrize("name", sorted(VALID_VARIANTS))
def test_schema_and_loader_both_accept(name):
    doc = variant(VALID_VARIANTS[name])
    VALIDATOR.validate(doc)
    inv.load(doc, install=False, base_dir=FIXTURES)


def test_original_validates():
    VALIDATOR.validate(RULES)
