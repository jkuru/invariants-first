"""Standing: a computed report."""
from __future__ import annotations

import invariants as inv
from support import load_variant, rule


def test_r1_held_only_with_scan(reg):
    s1, _ = inv.standing("R-1", scan=True)
    s1n, why1n = inv.standing("R-1")
    assert s1 == "held" and s1n == "enforce(known-callers)" and "scan" in why1n[0]


def test_r7_with_a_provider_is_held(reg):
    assert inv.standing("R-7", scan=True) == ("held", [])


def test_r20_names_its_residual_path(reg):
    s20, why20 = inv.standing("R-20", scan=True)
    assert s20 == "enforce(known-callers)" and any("concurrent" in w for w in why20)


def test_standing_other_states(reg):
    assert inv.standing("R-21", scan=True)[0] == "enforce(known-callers)"     # residual path
    det = load_variant(lambda d: rule(d, "P-2").update(status="detect"))
    assert inv.standing("P-2", det)[0] == "monitored"
    cand = load_variant(lambda d: rule(d, "P-2").update(status="candidate"))
    assert inv.standing("P-2", cand) == ("candidate", [])
    ret = load_variant(lambda d: rule(d, "P-2").update(status="retired"))
    assert inv.standing("P-2", ret) == ("retired", [])


def test_standing_method_matches_function(reg):
    assert reg.standing("R-1", scan=True) == inv.standing("R-1", scan=True)
