"""Standing: a computed report (prototype checks 63-65)."""
from __future__ import annotations

import invariants as inv
from support import load_variant, rule


def test_r20_without_storage_enforcer_is_not_held(reg):
    _, why20 = inv.standing("R-20", scan=True)
    nostore = load_variant(lambda d: rule(d, "R-20")["enforcers"].pop(1))
    s20b, why20b = inv.standing("R-20", nostore, scan=True)
    assert not any("pool_leased" in w for w in why20)
    assert s20b != "held" and any("pool_leased (snapshot)" in w for w in why20b)


def test_r1_held_only_with_scan(reg):
    s1, _ = inv.standing("R-1", scan=True)
    s1n, why1n = inv.standing("R-1")
    assert s1 == "held" and s1n == "enforce(known-callers)" and "scan" in why1n[0]


def test_r7_immutable_provider_counts_like_same_txn(reg):
    s7, _ = inv.standing("R-7", scan=True)
    s7b, why7b = inv.standing("R-7", load_variant(lambda d: d["providers"]["job"].update(consistency="snapshot")),
                              scan=True)
    assert reg.providers["job"].consistency == "immutable" and s7 == "held" and s7b != "held"
    assert any("job (snapshot)" in w for w in why7b)


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
