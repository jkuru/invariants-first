"""The rule file itself: loads, covers every target, and agrees with the code's vocabulary."""
from __future__ import annotations

import json
import re
import typing

import invariants as inv

from fact_pipeline import rules_path
from fact_pipeline.models import PREDICATES, FactReply

REQUIRED = {"K-1", "K-2", "K-4", "K-7", "K-11", "K-12", "K-15", "K-18", "K-19"}


def test_rule_file_has_the_catalog() -> None:
    assert REQUIRED <= set(inv.current().rules)


def test_verify_passes() -> None:
    assert inv.verify() == 10


def _rule_list(rule_id: str) -> list[str]:
    """The one list literal in the rule's bad (K-4's vocabulary, K-11's functional predicates)."""
    doc = inv.read_document(rules_path())
    bad = next(r for r in doc["rules"] if r["id"] == rule_id)["bad"]
    (items,) = re.findall(r"\[([^\]]*)\]", bad)
    return [json.loads(x) for x in items.split(",")]


def test_k4_vocabulary_equals_the_schema_literal() -> None:
    """K-4's list in the rule file equals the FactReply.predicate Literal (the pinned contract), in
    order. PREDICATES is derived from the Literal, so Python holds one copy."""
    literal = typing.get_args(FactReply.model_fields["predicate"].annotation)
    assert tuple(_rule_list("K-4")) == PREDICATES == tuple(p for p in literal if p not in ("NONE", "CANNOT_COMPLY"))


def test_k11_functional_predicates_are_in_the_vocabulary() -> None:
    """K-11's functional list lives only in the rule file; each entry must be a known predicate."""
    functional = _rule_list("K-11")
    assert functional and len(set(functional)) == len(functional) and set(functional) <= set(PREDICATES)


def test_detect_rule_is_capped_at_detect() -> None:
    rule = inv.current().rules["K-16"]
    assert {rule.mode(e) for e in rule.enforcers} == {"detect"}


def test_standing_reports() -> None:
    assert inv.standing("K-16")[0] == "monitored"
    assert inv.standing("K-12", scan=True)[0] == "held"
