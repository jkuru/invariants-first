"""Writing the drafted rule file: contracts active, suggested rules commented out, and the summary lines."""
from __future__ import annotations

import json
import re

from .finding import Found, Guard


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _rule_id(g: Guard, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", g.qualname).strip("-").upper()
    rid = base if base not in taken else re.sub(r"[^A-Za-z0-9]+", "-", f"{g.module}.{g.qualname}").upper()
    taken.add(rid)
    return rid


def _contract_lines(found: Found) -> list[str]:
    """One pydantic contract per model with constraints, each under a comment naming them."""
    contracts: list[str] = []
    names: set[str] = set()
    for m in found.models:
        if not m.constraints:
            continue
        name = _snake(m.name)
        if name in names:
            name = _snake(f"{m.module.replace('.', '_')}_{m.name}")
        names.add(name)
        shown = ", ".join(m.constraints[:4]) + (f", +{len(m.constraints) - 4} more" if len(m.constraints) > 4 else "")
        contracts.append(f"  # {shown}")
        if m.validators:
            contracts.append(f"  # (its {m.validators} validator function(s) are code, not schema: not pinned)")
        contracts.append(f"  {name}: {{type: pydantic, ref: {_q(f'{m.module}:{m.name}')}}}")
    return contracts


def _suggested_lines(rules: list[Guard]) -> list[str]:
    """The suggested short-form rules, commented out, under how to turn one on."""
    lines = ["",
             "# Suggested rules, from the preconditions at the top of these functions. Each one keeps the",
             "# check even if a later edit deletes the `if` or `assert`. To turn one on:",
             "#   1. uncomment it,",
             "#   2. put @inv.guard(\"<id>\") on the function (import invariants as inv),",
             "#   3. write a test that calls it with bad input and expects inv.Rejected, and list it in `tests:`",
             "#      (until a rule names a test it only logs; with one it blocks, and `pytest --inv-ablate`",
             "#      proves the test fails when the guard is switched off),",
             "#   4. run `inv lock`.",
             "#"]
    taken: set[str] = set()
    for g in rules:
        rid = _rule_id(g, taken)
        lines.append(f"#  - id: {rid}")
        lines.append(f"#    guard: {_q(f'py:{g.module}.{g.qualname}')}")
        if len(g.clauses) == 1:
            cel, msg = g.clauses[0]
            lines.append(f"#    statement: {_q(msg)}")
            lines.append(f"#    bad: {_q(cel)}")
        else:
            lines.append("#    bad:")
            for cel, msg in g.clauses:
                lines.append(f"#      - {{when: {_q(cel)}, message: {_q(msg)}}}")
        lines.append("#    tests: []")
        for s in g.skipped:
            lines.append(f"#    # not translated, {s}")
        lines.append("#")
    return lines


def render(found: Found, locked: bool = False) -> str:
    """The rule file text: contracts active, suggested rules commented out. locked: `inv init` pins it
    in invariants.lock right after writing it (without --no-lock), and the header says so."""
    header = ("# invariants.yaml, drafted by `inv init` and pinned in invariants.lock. Review both, then commit "
              "them." if locked else
              "# invariants.yaml, drafted by `inv init`. Review it, then run `inv lock` and commit both files.")
    lines = [header,
             "#",
             "# The contracts below pin rules your code already has. From now on `inv check` fails when a",
             "# change lets a model accept more than it does today (a raised limit, a removed",
             "# constraint, a new enum value), unless someone accepts it with `inv lock --accept-weakening`.",
             "spec: invariant/v0.1", ""]
    contracts = _contract_lines(found)
    lines += ["contracts:", *contracts, ""] if contracts else ["contracts: {}", ""]
    rules = [g for g in found.guards if g.clauses]
    lines.append("rules:")
    if found.doors is not None:
        from .. import doors
        lines += doors.render(found.doors)
    if rules:
        lines += _suggested_lines(rules)
    untranslated = [g for g in found.guards if not g.clauses]
    if untranslated:
        lines.append("# Preconditions found but not translated to CEL (write these by hand if they matter):")
        for g in untranslated:
            for s in g.skipped:
                lines.append(f"#   {g.module}.{g.qualname} {s}")
    return "\n".join(lines).rstrip() + "\n"


def summary(found: Found) -> list[str]:
    models = [m for m in found.models if m.constraints]
    n_cons = sum(len(m.constraints) for m in models)
    rules = [g for g in found.guards if g.clauses]
    return [f"read {found.files} source file(s)",
            f"pinned {len(models)} pydantic model(s) with {n_cons} constraint(s)",
            f"suggested {len(rules)} rule(s) from function preconditions (commented out; see the file)",
            *(_doors_summary(found))]


def _doors_summary(found: Found) -> list[str]:
    if found.doors is None:
        return []
    from .. import doors
    return doors.summary(found.doors)
