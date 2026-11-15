"""Out-of-call enforcement: the monitor, and rule files shipped inside a package."""
from __future__ import annotations

import importlib.resources
import pathlib
import time
from typing import Any, Iterable

from .errors import LoadError
from .evaluate import evaluate, report
from .model import Outcome
from .registry import Registry, current


def monitor(rule_id: str, rows: Iterable[tuple[Any, dict[str, Any]]],
            reg: Registry | None = None) -> list[tuple[Any, Outcome]]:
    """Evaluate a rule's monitor enforcer over (key, base) rows; record each outcome. Never blocks.

    base is the binding environment for one row, e.g. {"args": {...}, "self": store}. The
    enforcer's view (if any) applies. Returns (key, outcome) for every row; an
    ablated rule, or one with no live monitor enforcer, returns [].
    """
    reg = reg or current()
    rule = reg.rules[rule_id]
    i, enf = rule.enforcer("monitor")
    if i is None or enf is None or reg.mode(rule, enf) == "off" or reg.skips(rule_id):
        return []
    key_i = i if i in rule.names_for else "rule"
    out = []
    for key, base in rows:
        started = time.time_ns()
        o = evaluate(rule, base, key_i, reg)
        report(reg, rule, {**enf, "mode": "detect"}, o, "report", started=started, key=key)
        out.append((key, o))
    return out


def load_resource(package: str, name: str = "invariants.yaml", **kw: Any) -> Registry:
    """inv.load a rule file shipped inside a package (importlib.resources); artifacts resolve beside it."""
    from .loader import load
    try:
        res = importlib.resources.files(package) / name
    except ModuleNotFoundError as e:
        raise LoadError(f"rule file package {package!r}: {e}") from e
    if not res.is_file():
        raise LoadError(f"{package} has no resource {name!r}")
    with importlib.resources.as_file(res) as path:
        return load(pathlib.Path(path), **kw)
