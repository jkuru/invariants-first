"""Out-of-call enforcement points: the monitor, storage-enforcer rejections, and package rule files."""
from __future__ import annotations

import contextlib
import importlib.resources
import pathlib
from typing import Any, Iterable, Iterator, Mapping

from .errors import LoadError, Rejected
from .evaluate import evaluate, report
from .model import Outcome
from .registry import Registry, current


def monitor(rule_id: str, rows: Iterable[tuple[Any, dict[str, Any]]], reg: Registry | None = None,
            in_txn: bool = False) -> list[tuple[Any, Outcome]]:
    """Evaluate a rule's monitor enforcer over (key, base) rows; record each outcome. Never blocks.

    base is the binding environment for one row, e.g. {"args": {...}, "self": store}. The
    enforcer's view (if any) applies. in_txn=True asserts the rows are read inside one
    transaction, so same_txn providers can be read. Returns (key, outcome) for every row; an
    ablated rule, or one with no live monitor enforcer, returns [].
    """
    reg = reg or current()
    rule = reg.rules[rule_id]
    i, enf = rule.enforcer("monitor")
    if i is None or enf is None or reg.mode(rule, enf) == "off" or reg.is_ablated(rule_id):
        return []
    key_i = i if i in rule.names_for else "rule"
    out = []
    for key, base in rows:
        o = evaluate(rule, base, key_i, reg, None, in_txn)
        ev = report(reg, rule, {**enf, "mode": "detect"}, o, "report")
        ev["key"] = key
        out.append((key, o))
    return out


def storage_rejection(rule_id: str, exc: BaseException | None = None, reg: Registry | None = None,
                      message: str | None = None) -> Rejected:
    """A Rejected for a storage enforcer's refusal (a CHECK, trigger or UNIQUE), counted in the reach report.

    Raise it from the database error: `raise inv.storage_rejection("R-20", e) from e`. It has
    point "storage", which the ASGI and FastAPI mapping answers with StatusMap.storage (409).
    """
    reg = reg or current()
    rule = reg.rules[rule_id]
    _, enf = rule.enforcer("storage")
    enf = enf or {"point": "storage", "timing": "pre_effect", "mode": "enforce"}
    out = Outcome("violation", rule_id, [message or rule.raw.get("statement") or rule_id])
    report(reg, rule, enf, out, "reject")
    return Rejected(f"{rule_id} violation (storage): {'; '.join(out.messages)}", out, point="storage")


@contextlib.contextmanager
def storage_errors(markers: Mapping[str, str], types: tuple[type[BaseException], ...] = (Exception,),
                   reg: Registry | None = None) -> Iterator[None]:
    """Turn a database error whose text contains a marker into that rule's storage_rejection.

        with inv.storage_errors({"R-20:": "R-20"}, (sqlite3.IntegrityError,)):
            store.add_lease(...)
    """
    try:
        yield
    except types as e:
        text = str(e)
        for marker, rule_id in markers.items():
            if marker in text:
                raise storage_rejection(rule_id, e, reg) from e
        raise


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
