"""Load and verify, plus JSON/YAML loading."""
from __future__ import annotations

import gc
import json

import pytest

import invariants as inv
from support import FIXTURES, RULE_FILE, RULES, YAML_RULE_FILE, load_error, load_variant, raises, rule, variant


def test_rule_file_loads(reg):
    assert len(reg.rules) == 6 and len(reg.targets) == 6 and len(reg.providers) == 4
    assert list(reg.rules) == ["R-1", "R-7", "R-20", "R-21", "P-1", "P-2"]


def test_verify_passes(reg):
    assert inv.verify is inv.verify_coverage
    assert inv.verify() == 6


def test_yaml_rule_file_loads_the_same(reg):
    import yaml
    assert yaml.safe_load(YAML_RULE_FILE.read_text()) == RULES
    with inv.isolated() as y:
        inv.load(YAML_RULE_FILE)
        assert list(y.rules) == list(reg.rules)
        assert y.targets == reg.targets
        assert {n: p.raw for n, p in y.providers.items()} == {n: p.raw for n, p in reg.providers.items()}
        assert [r.max_status for r in y.rules.values()] == [r.max_status for r in reg.rules.values()]
        assert inv.verify() == 6


def test_yml_suffix_is_yaml(tmp_path):
    p = tmp_path / "rules.yml"
    p.write_text("rules:\n  - {id: C-1, status: candidate, bad: 'args.x > 1'}\n")
    assert list(inv.load(p, install=False).rules) == ["C-1"]


def test_yaml_bare_off_gets_a_hint(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("rules:\n  - id: C-1\n    status: candidate\n    bad: 'false'\n"
                 "    enforcers: [{point: decorator, timing: pre_effect, mode: off}]\n")
    e = raises(inv.LoadError, inv.load, p, install=False)
    assert e is not None and "quote it" in str(e)


@pytest.mark.parametrize("text,suffix", [("{not json", ".json"), ("a: [", ".yaml"), ("[1, 2]", ".json")])
def test_unreadable_file_is_load_error(tmp_path, text, suffix):
    p = tmp_path / f"rules{suffix}"
    p.write_text(text)
    assert raises(inv.LoadError, inv.load, p, install=False) is not None


def test_missing_file_is_load_error(tmp_path):
    assert raises(inv.LoadError, inv.load, tmp_path / "nope.json", install=False) is not None


def test_install_false_leaves_current_registry_alone(reg):
    other = inv.load(variant(lambda d: d["rules"].pop()), install=False, base_dir=FIXTURES)
    assert len(other.rules) == 5 and len(reg.rules) == 6 and inv.current() is reg


def test_load_rejects_unknown_field():
    e = load_error(lambda d: rule(d, "R-1").update(severity="high"))
    assert e is not None and "unknown field" in str(e)


def test_load_rejects_missing_target():
    assert load_error(lambda d: rule(d, "R-1")["subject"].update(target="py:jobs.split_wrok")) is not None


def test_load_refuses_post_effect_on_external_target():
    e = load_error(lambda d: rule(d, "P-2")["enforcers"][0].update(timing="post_effect"))
    assert e is not None and "timing check fails" in str(e)


def test_load_rejects_args_rename():
    e = load_error(lambda d: rule(d, "R-1").update(bad="inv.sum(result) != args.total"))
    assert e is not None and "args.total" in str(e)


def test_load_rejects_bare_has():
    assert load_error(lambda d: rule(d, "R-21").update(bad="!has(approval)")) is not None


def _gateway(artifact: str):
    return lambda d: rule(d, "R-20")["enforcers"].append(
        {"point": "gateway", "timing": "pre_effect", "mode": "enforce", "artifact": artifact})


def test_load_rejects_changed_artifact_hash():
    e = load_error(_gateway("pool_schema.sql#sha256:" + "0" * 64))
    assert e is not None and "changed" in str(e)


def test_load_rejects_missing_artifact():
    e = load_error(_gateway("nope.yaml"))
    assert e is not None and "missing" in str(e)


def test_verify_fails_for_unguarded_target(reg):
    bad = load_variant(lambda d: d["rules"].append(
        {"id": "R-23x", "status": "detect", "statement": "set_size only via the door",
         "subject": {"target": "py:pool.set_size"}, "bad": "false",
         "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]}))
    e = raises(inv.CoverageError, inv.verify, bad)
    assert e is not None and "R-23x" in str(e)


def remediate_unguarded(cause, job_id):
    return f"remediate {job_id}"   # no @inv.edge


def test_verify_fails_when_edge_target_is_not_an_edge():
    bad = load_variant(lambda d: rule(d, "R-7")["enforcers"][1].update(target=f"py:{__name__}.remediate_unguarded"))
    e = raises(inv.CoverageError, inv.verify, bad)
    assert e is not None and "not wrapped by @inv.edge" in str(e)


def test_verify_fails_for_guard_naming_unknown_rule(reg):
    @inv.guard("NOPE-1")
    def stray() -> None: ...
    assert inv.verify() == 6                      # default scope: the packages of the loaded targets
    e = raises(inv.CoverageError, inv.verify, None, [__name__])
    assert e is not None and "NOPE-1" in str(e)
    del stray, e
    gc.collect()
    assert inv.verify(None, [__name__]) == 6


def test_verify_scope_is_target_packages(reg):
    assert {"jobs", "pool", "triage"} <= reg.scope()


def test_status_enforce_needs_falsified_by():
    e = load_error(lambda d: rule(d, "R-21").pop("falsified_by"))
    assert e is not None and "highest allowed: detect" in str(e)


def test_effective_mode_table():
    t = {(m, s): inv.effective_mode(m, s) for m in ("off", "detect", "enforce")
         for s in ("candidate", "contested", "detect", "enforce", "retired")}
    assert t[("enforce", "candidate")] == t[("enforce", "contested")] == t[("enforce", "detect")] == "detect"
    assert t[("enforce", "enforce")] == "enforce" and t[("enforce", "retired")] == "off"
    assert t[("detect", "enforce")] == "detect" and t[("off", "enforce")] == "off"


def test_effects_on_a_rule_is_rejected():
    e = load_error(lambda d: rule(d, "R-1").update(effects="none"))
    assert e is not None and "belongs to the target" in str(e)


def test_provider_return_type_must_match():
    e = load_error(lambda d: d["providers"]["approval"].update(type="py:pool.ResizePoolRequest | null"))
    assert e is not None and "returns" in str(e)


def test_provider_registered_under_another_name():
    e = load_error(lambda d: d["providers"]["approval"].update(**{"from": "py:pool.pool_size"}))
    assert e is not None and "registered as 'pool_size'" in str(e)


def test_provider_signature_must_match_args():
    e = load_error(lambda d: d["providers"]["pool_size"]["args"].pop("conn"))
    assert e is not None and "signature" in str(e)


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: rule(d, "R-1").pop("status"), "missing status"),
    (lambda d: rule(d, "R-1").pop("bad"), "missing bad"),
    (lambda d: rule(d, "R-1").pop("id"), "missing id"),
    (lambda d: rule(d, "R-1").update(bad=[]), "bad must be"),
    (lambda d: rule(d, "R-1").update(bad=[{"message": "x"}]), "string when"),
    (lambda d: d["rules"].append(dict(rule(d, "R-1"))), "duplicate id"),
    (lambda d: rule(d, "R-1").update(status="approved"), "not in"),
    (lambda d: rule(d, "R-1")["enforcers"][0].pop("mode"), "needs point, timing, mode"),
    (lambda d: rule(d, "R-1")["enforcers"][0].update(target="py:jobs.split_work"), "edge enforcers"),
    (lambda d: rule(d, "R-1")["enforcers"][0].update(view="rows"), "view must be a name map"),
    (lambda d: rule(d, "R-20")["enforcers"][0]["view"].update(n="argz.slots"), "bad root"),
    (lambda d: rule(d, "R-20")["enforcers"][0]["view"].update(n="provider.nope"), "no declared provider"),
    (lambda d: rule(d, "R-1").update(bad="nope > 1"), "undeclared name"),
    (lambda d: rule(d, "R-1").update(bad="args.x >"), "does not parse"),
    (lambda d: d.update(extra=1), "unknown field"),
    (lambda d: d["targets"]["py:jobs.split_work"].pop("effects"), "missing effects"),
    (lambda d: d["providers"]["job"].pop("type"), "needs"),
    (lambda d: rule(d, "R-7").pop("model_policy"), "needs model_policy"),
    (lambda d: rule(d, "R-7")["model_policy"].update(format_retries=-1), "non-negative"),
    (lambda d: rule(d, "R-7")["model_policy"]["feedback"].update(semantic="hint"), "feedback"),
    (lambda d: rule(d, "R-7")["model_policy"]["abstain"].pop("value"), "abstain needs"),
    (lambda d: rule(d, "R-7")["model_policy"]["abstain"].update(field="reason"), "not in CauseReply"),
])
def test_load_negatives(mutate, needle):
    e = load_error(mutate)
    assert e is not None and needle in str(e), str(e)


def test_retired_rule_skips_target_checks():
    reg = load_variant(lambda d: rule(d, "R-1").update(status="retired", subject={"target": "py:jobs.nope"}))
    assert reg.rules["R-1"].target is None


def test_load_from_dict_and_path_agree(reg):
    again = inv.load(json.loads(RULE_FILE.read_text()), install=False, base_dir=FIXTURES)
    assert list(again.rules) == list(reg.rules)


def test_provider_nullable_type_without_spaces():
    reg = load_variant(lambda d: d["providers"]["approval"].update(type="py:pool.Approval|null"))
    assert reg.providers["approval"].nullable
