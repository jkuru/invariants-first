"""The rule file itself: loads, covers every target, and agrees with the code's vocabulary."""
from __future__ import annotations

import typing

import invariants as inv

from fact_pipeline import rules_path
from fact_pipeline.models import FUNCTIONAL, PREDICATES, FactReply

REQUIRED = {"K-1", "K-2", "K-4", "K-7", "K-11", "K-12", "K-15", "K-18", "K-19"}


def test_rule_file_has_the_catalog() -> None:
    assert REQUIRED <= set(inv.current().rules)


def test_verify_passes() -> None:
    assert inv.verify() == 10


def test_k4_vocabulary_matches_the_schema() -> None:
    doc = inv.read_document(rules_path())
    k4 = next(r for r in doc["rules"] if r["id"] == "K-4")
    assert all(f'"{p}"' in k4["bad"] for p in PREDICATES)
    literal = set(typing.get_args(FactReply.model_fields["predicate"].annotation))
    assert set(PREDICATES) == literal - {"NONE", "CANNOT_COMPLY"}


def test_k11_functional_list_matches_the_code() -> None:
    doc = inv.read_document(rules_path())
    k11 = next(r for r in doc["rules"] if r["id"] == "K-11")
    assert all(f'"{p}"' in k11["bad"] for p in FUNCTIONAL)


def test_detect_rule_is_capped_at_detect() -> None:
    rule = inv.current().rules["K-16"]
    assert {rule.mode(e) for e in rule.enforcers} == {"detect"}


def test_standing_reports() -> None:
    assert inv.standing("K-16")[0] == "monitored"
    assert inv.standing("K-12", scan=True)[0] == "held"
