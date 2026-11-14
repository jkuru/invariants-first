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


def _expand_door(raw: dict[str, Any], rid: str) -> None:
    """door/sink/allow -> mediation; without guard or bad, a structure rule checked by the scan."""
    if "sink" not in raw:
        raise LoadError(f"{rid}: a door rule names the sink it protects: add sink: py:module.function "
                        f"(the function that writes)")
    med = raw.get("mediation")
    if med is not None and not isinstance(med, dict):
        raise LoadError(f"{rid}: mediation must be a mapping")
    clash = sorted({"sink", "door", "allow", "allow_unresolved"} & set(med or {}))
    if clash:
        raise LoadError(f"{rid}: give {', '.join(clash)} at the top of the rule or under mediation, not both")
    sinks = _py_list(rid, "sink", raw.pop("sink"))
    doors = _py_list(rid, "door", raw.pop("door")) if "door" in raw else []
    allow_raw = raw.pop("allow", None)
    allow = _py_list(rid, "allow", allow_raw) if allow_raw not in (None, []) else []   # emptied: none left
    new = {**(med or {}), "sink": sinks if len(sinks) > 1 else sinks[0]}
    if doors:
        new["door"] = doors if len(doors) > 1 else doors[0]
    if allow:
        new["allow"] = allow
    if "allow_unresolved" in raw:                      # reviewed unresolved sites, as written
        new["allow_unresolved"] = raw.pop("allow_unresolved")
    if not doors and "guard" not in raw:
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
    return doc
