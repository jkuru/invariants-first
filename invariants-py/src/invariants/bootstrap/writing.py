"""Writing the drafted rule file: contracts active, suggested rules commented out, and the summary lines."""
from __future__ import annotations

import json
import re

from .finding import Found, Guard, Model


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _rule_id(g: Guard, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", g.qualname).strip("-").upper()
    rid = base if base not in taken else re.sub(r"[^A-Za-z0-9]+", "-", f"{g.module}.{g.qualname}").upper()
    taken.add(rid)
    return rid


def contracts(found: Found) -> list[tuple[str, Model]]:
    """(contract name, model) for each model with constraints: the contracts `inv init` drafts."""
    out: list[tuple[str, Model]] = []
    names: set[str] = set()
    for m in found.models:
        if not m.constraints:
            continue
        name = _snake(m.name)
        if name in names:
            name = _snake(f"{m.module.replace('.', '_')}_{m.name}")
        names.add(name)
        out.append((name, m))
    return out


def contract_record(m: Model) -> dict[str, str]:
    """The contract `inv init` drafts for a model."""
    return {"type": "pydantic", "ref": f"{m.module}:{m.name}"}


def _contract_lines(found: Found) -> list[str]:
    """One pydantic contract per model with constraints, each under a comment naming them; a model
    `inv lock` cannot pin is written commented out, with the reason."""
    lines: list[str] = []
    for name, m in contracts(found):
        shown = ", ".join(m.constraints[:4]) + (f", +{len(m.constraints) - 4} more" if len(m.constraints) > 4 else "")
        lines.append(f"  # {shown}")
        if m.validators:
            lines.append(f"  # (its {m.validators} validator function(s) are code, not schema: not pinned)")
        entry = f"{name}: {{type: pydantic, ref: {_q(contract_record(m)['ref'])}}}"
        if m.problem:
            lines.append(f"  # not written as a contract: `inv lock` cannot pin it: {m.problem}")
            lines.append(f"  # {entry}")
        else:
            lines.append(f"  {entry}")
    return lines


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
    contract_lines = _contract_lines(found)
    active = any(not m.problem for _, m in contracts(found))
    lines += ["contracts:" if active else "contracts: {}", *contract_lines, ""]
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
    if found.doors is not None:                    # a site several door rules see, drafted once (fix round 30)
        from .. import doors
        lines += doors.render_shared(found.doors)
    return "\n".join(lines).rstrip() + "\n"


def summary(found: Found) -> list[str]:
    models = [m for m in found.models if m.constraints and not m.problem]
    n_cons = sum(len(m.constraints) for m in models)
    rules = [g for g in found.guards if g.clauses]
    unpinnable = [m for m in found.models if m.constraints and m.problem]
    return [f"read {found.files} source file(s)",
            f"pinned {len(models)} pydantic model(s) with {n_cons} constraint(s)",
            *([f"left {len(unpinnable)} model(s) commented out: `inv lock` cannot pin them ("
               + ", ".join(m.name for m in unpinnable) + "; the file says why)"] if unpinnable else []),
            f"suggested {len(rules)} rule(s) from function preconditions (commented out; see the file)",
            *(_doors_summary(found))]


def _doors_summary(found: Found) -> list[str]:
    if found.doors is None:
        return []
    from .. import doors
    return doors.summary(found.doors)
