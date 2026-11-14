"""The short rule forms (guard/tests, door/sink/allow), rewritten into the full form before anything reads them."""
from __future__ import annotations

from typing import Any

from ..errors import LoadError

SHORT_FIELDS = frozenset({"guard", "tests", "door", "sink", "allow", "allow_unresolved"})
SHORT_ENFORCER = {"point": "decorator", "timing": "pre_effect", "mode": "enforce"}
DOOR_FIELDS = ("door", "sink", "allow", "allow_unresolved")
STRUCTURE_ENFORCER = {"point": "structure", "timing": "pre_effect", "mode": "enforce"}


def _py_list(rid: str, key: str, v: Any) -> list[str]:
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not items or not all(isinstance(x, str) and x.startswith("py:") for x in items):
        raise LoadError(f"{rid}: {key} must be a 'py:module.function' path or a non-empty list of them")
    return list(items)


def _check_accepts(rid: str, entries: Any, shared: str | None = None) -> None:
    """A top-level allow_unresolved checked as the scan checks mediation.allow_unresolved, its errors
    naming the field as the rule spells it (allow_unresolved[0], not mediation.allow_unresolved[0]).
    shared: the place of one entry of the file's top-level allow_unresolved (`allow_unresolved[2]`),
    which its errors name instead."""
    from ..scan import ScanConfigError
    from ..scan.mediation import _accepts
    try:
        _accepts(rid, entries)
    except ScanConfigError as e:
        long, short = f"{rid}: mediation.allow_unresolved", f"{rid}: allow_unresolved"
        msg = str(e)
        if shared is not None and msg.startswith(long + "[0]"):
            raise LoadError(shared + msg[len(long) + 3:]) from None
        raise LoadError(short + msg[len(long):] if msg.startswith(long) else msg) from None


def _expand_door(raw: dict[str, Any], rid: str) -> None:
    """door/sink/allow -> mediation; without guard or bad, a structure rule checked by the scan."""
    if "sink" not in raw:
        raise LoadError(f"{rid}: a door rule names the sink it protects: add sink: py:module.function "
                        f"(the function that writes)")
    med = raw.get("mediation")
    if med is not None and not isinstance(med, dict):
        raise LoadError(f"{rid}: mediation must be a mapping")
    both = sorted(set(DOOR_FIELDS) & set(med or {}) & set(raw))
    if both:
        raise LoadError(f"{rid}: give {', '.join(both)} at the top of the rule or under mediation, not both")
    # mediation.allow_unresolved (as `inv check`'s hint for a long-form rule spells it) is kept as written;
    # a door or allow under mediation beside a top-level sink: is half of each form
    moved = sorted({"door", "allow"} & set(med or {}))
    if moved:
        raise LoadError(f"{rid}: this rule gives sink: at the top (the door:/sink: form), so give "
                        f"{', '.join(moved)} at the top too, not under mediation")
    if "allow_unresolved" in raw:
        _check_accepts(rid, raw["allow_unresolved"])
    paired = isinstance(raw["sink"], dict)             # each sink mapped to the doors that may reach it
    if paired:
        from .shapes import _check_pairs
        _check_pairs(rid, raw["sink"], "sink")
    sink = raw.pop("sink") if paired else _py_list(rid, "sink", raw.pop("sink"))
    doors = _py_list(rid, "door", raw.pop("door")) if "door" in raw else []
    allow_raw = raw.pop("allow", None)
    allow = _py_list(rid, "allow", allow_raw) if allow_raw not in (None, []) else []   # emptied: none left
    new = {**(med or {}), "sink": sink if paired or len(sink) > 1 else sink[0]}
    if doors:
        new["door"] = doors if len(doors) > 1 else doors[0]
    if allow:
        new["allow"] = allow
    if "allow_unresolved" in raw:                      # reviewed unresolved sites, as written
        new["allow_unresolved"] = raw.pop("allow_unresolved")
    if not doors and not paired and "guard" not in raw:
        raise LoadError(f"{rid}: a door rule names its door: add door: py:module.function (the function every "
                        f"write must go through), or guard: to make the guarded function the door")
    raw["mediation"] = new
    if "guard" in raw or "bad" in raw or "subject" in raw:
        if "guard" not in raw and "bad" in raw:
            raise LoadError(f"{rid}: a door rule is checked by the scan and has no bad; to also check values at "
                            f"run time, add guard: (the function whose arguments bad reads)")
        return
    if "enforcers" in raw:
        raise LoadError(f"{rid}: a door rule adds its structure enforcer; drop enforcers or use the long form")
    raw["bad"] = "false"                          # nothing to evaluate at run time: the scan is the check
    raw["enforcers"] = [dict(STRUCTURE_ENFORCER)]
    raw.setdefault("status", "enforce")


def _expand_tests(raw: dict[str, Any], rid: str) -> None:
    """tests -> falsified_by."""
    if "falsified_by" in raw:
        raise LoadError(f"{rid}: give tests or falsified_by, not both (tests is the short name)")
    tests = raw.pop("tests")
    if isinstance(tests, str):
        tests = [tests]
    if not isinstance(tests, list) or not all(isinstance(t, str) and t for t in tests):
        raise LoadError(f"{rid}: tests must be a pytest node id or a list of them")
    raw["falsified_by"] = tests


def _expand_guard(raw: dict[str, Any], rid: str) -> None:
    """guard -> subject.target, one decorator enforcer, and a status from whether the rule names tests."""
    target = raw.pop("guard")
    if not isinstance(target, str) or not target.startswith("py:"):
        raise LoadError(f"{rid}: guard must be the 'py:module.function' the rule protects, got {target!r}")
    subject = raw.get("subject")
    if subject is not None and not isinstance(subject, dict):
        raise LoadError(f"{rid}: subject must be a mapping")
    if subject and "target" in subject:
        raise LoadError(f"{rid}: give guard or subject.target, not both (guard is the short form)")
    if "enforcers" in raw:
        raise LoadError(f"{rid}: guard adds the decorator enforcer; drop enforcers or use subject.target "
                        f"with your own enforcers")
    raw["subject"] = {**(subject or {}), "target": target}
    raw["enforcers"] = [dict(SHORT_ENFORCER)]
    raw.setdefault("status", "enforce" if raw.get("falsified_by") else "detect")


def expand_short_forms(doc: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the short rule form into the full one, in place, before anything else reads the file.

    A short rule names the function it guards and, optionally, the tests that prove the guard works:

        - id: PAY-1
          guard: py:payments.refund
          bad: 'args.amount > args.charged - args.refunded'
          tests: [tests/test_payments.py::test_over_refund_is_rejected]

    becomes subject.target, one decorator enforcer (pre_effect, mode enforce) and falsified_by. With
    no `status`, it is `enforce` when the rule names tests and `detect` (log, do not block) when it does
    not, so the loader's evidence check never refuses a short rule. The lock pins the full form, so
    switching between the two forms is not a change, and deleting `tests:` lowers the status
    (a weakening).

    A door rule names the function every write must go through and the function that writes:

        - id: LEDGER-DOOR
          door: py:billing.service.apply_transfer
          sink: py:billing.store.write_balance
          allow: [py:billing.batch_refund.refund_all]     # known back doors, frozen

    becomes mediation {sink, door, allow}, `bad: 'false'` and one structure enforcer: nothing runs in
    production, and `inv scan` (inside `inv check`) fails any call path to the sink that skips the
    door. `allow` lists known back doors (mediation.allow): the scan
    lets them through, so today's code passes and a new back door fails; the lock reports an added
    entry as a weakening. With guard:
    as well, the guarded function is the door and the rule also checks its arguments."""
    rules = doc.get("rules")
    for raw in rules if isinstance(rules, list) else []:
        if not isinstance(raw, dict) or not SHORT_FIELDS & set(raw):
            continue
        rid = raw.get("id", "?")
        if any(k in raw for k in DOOR_FIELDS):
            _expand_door(raw, rid)
        if "tests" in raw:
            _expand_tests(raw, rid)
        if "guard" in raw:
            _expand_guard(raw, rid)
    expand_shared_accepts(doc)
    return doc


def expand_shared_accepts(doc: dict[str, Any]) -> dict[str, Any]:
    """The file's top-level allow_unresolved, given to the rules each entry lists, in place (after the
    rules' short forms are expanded). An entry names one reviewed site, as a rule's own entry does, and
    the path laws it is accepted under:

        allow_unresolved:
          - {at: "app/plugins.py:41", code: unresolved/reflection, sha: 3f9c0a17be52d4e1,
             why: "loads the exporters named in settings; none imports a store", rules: [LEDGER-DOOR, USER-DOOR]}

    Each listed rule gets the entry (with its rules list, so the scan says which entry accepted a finding)
    at the end of its mediation.allow_unresolved: it accepts the site once under each rule, and the lock
    pins it rule by rule, so adding an entry or a rule to its list is a weakening there, as adding a
    rule's own entry is."""
    if "allow_unresolved" not in doc:
        return doc
    entries = doc.pop("allow_unresolved")
    if entries is None:
        return doc
    if not isinstance(entries, list):
        raise LoadError("allow_unresolved must be a list of entries {at, code, sha, why, rules}")
    rules = doc.get("rules")
    by_id = {r["id"]: r for r in rules if isinstance(r, dict) and isinstance(r.get("id"), str)} \
        if isinstance(rules, list) else {}
    for i, e in enumerate(entries):
        where = f"allow_unresolved[{i}]"
        if not isinstance(e, dict):
            raise LoadError(f"{where} must be a mapping {{at, code, sha, why, rules}}")
        if "rules" not in e:
            raise LoadError(f"{where}: an entry here names its rules (rules: [ID, ...], the path laws it accepts "
                            f"the site under); an entry for one rule may go in that rule's own allow_unresolved")
        ids = e["rules"]
        if not (isinstance(ids, list) and ids and all(isinstance(r, str) for r in ids)):
            raise LoadError(f"{where}: rules must list the ids of the path laws the entry accepts its site under")
        for rid in ids:
            if ids.count(rid) > 1:
                raise LoadError(f"{where}: rule {rid} is listed twice")
            if rid not in by_id:
                raise LoadError(f"{where}: rule {rid} does not exist")
            med = by_id[rid].get("mediation")
            if not (isinstance(med, dict) and "sink" in med):
                raise LoadError(f"{where}: rule {rid} is not a path law (it declares no sink), so it has no "
                                f"unresolved site to accept")
        _check_accepts(ids[0], [e], where)
        for rid in ids:
            med = by_id[rid]["mediation"]
            own = med.get("allow_unresolved")
            if own is not None and not isinstance(own, list):
                continue                                   # malformed: the scan says so
            med["allow_unresolved"] = [*(own or []), {**e, "rules": list(ids)}]
    return doc
