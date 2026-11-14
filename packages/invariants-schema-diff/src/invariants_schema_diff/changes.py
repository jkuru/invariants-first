"""The values a comparison of two JSON documents reports through: canonical JSON and its digests,
type-strict equality, Change (one explained difference) and the leaf-level JSON diff. Standard library
only; invariants-first's lock reports its own changes with the same Change."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

MAX_DEPTH = 24         # nesting of one annotation (generics inside generics, aliases of aliases)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(obj: Any) -> bytes:
    """Canonical JSON bytes (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _plain(obj: Any) -> Any:
    """A JSON round trip: tuples become lists, keys become strings, so stored and fresh locks compare equal."""
    return json.loads(canonical(obj))


def same(a: Any, b: Any) -> bool:
    """Type-strict equality: 1 and true, or 1 and 1.0, differ (Python's == says they are equal)."""
    return canonical(a) == canonical(b)


# --- explaining changes -------------------------------------------------------------------------
@dataclasses.dataclass
class Change:
    """One difference between a stored lock and a fresh one."""

    where: str                     # e.g. "rule R-22", "target py:m.f", "contract api", "schema m.Reply"
    what: str                      # e.g. "bad", "enforcer decorator mode", "signature"
    old: Any = None
    new: Any = None
    weakened: bool = False
    note: str = ""
    # informational: the lock records it (a prose digest, a signature's spelling), but it changes no
    # rule's meaning, so it alone never makes the lock stale; `inv lock` refreshes it
    info: bool = False

    def __str__(self) -> str:
        if self.what in ("added", "removed") or (self.old is None and self.new is None):
            body = self.what
        else:
            body = f"{self.what}: {_short(self.old)} -> {_short(self.new)}"
        tag = "  WEAKENED" if self.weakened else "  (not stale)" if self.info else ""
        note = f" ({self.note})" if self.note else ""
        return f"{self.where}: {body}{tag}{note}"


def _short(v: Any, limit: int = 240) -> str:
    if v is None:
        return "(none)"
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)
    s = repr(s) if isinstance(v, str) else s
    return s if len(s) <= limit else s[:limit - 3] + "..."


def _is_ref(v: Any) -> bool:
    return isinstance(v, dict) and len(v) == 1 and isinstance(v.get("$ref"), str) and v["$ref"].startswith("sha256:")


def _json_paths(a: Any, b: Any, path: str = "",
                refs: list[tuple[str, str, str]] | None = None) -> list[tuple[str, Any, Any]]:
    """Leaf-level differences between two JSON values, as (path, old, new), compared type-strictly
    (1 and true differ). Lists of scalars diff as sets. With `refs`, two different schema-table
    references are collected there as (path, old digest, new digest) instead of being reported."""
    out: list[tuple[str, Any, Any]] = []
    if refs is not None and _is_ref(a) and _is_ref(b):
        if a["$ref"] != b["$ref"]:
            refs.append((path, a["$ref"], b["$ref"]))
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append((p, None, b[k]))
            elif k not in b:
                out.append((p, a[k], None))
            else:
                out += _json_paths(a[k], b[k], p, refs)
        return out
    if isinstance(a, list) and isinstance(b, list) and not same(a, b) and all(
            not isinstance(x, (dict, list)) for x in [*a, *b]):
        ka, kb = [canonical(x) for x in a], [canonical(x) for x in b]
        sa = [x for x, k in zip(a, ka) if k not in kb]
        sb = [x for x, k in zip(b, kb) if k not in ka]
        if sa and sb:                                  # ["int"] -> ["float"]: one replacement
            out.append((path, sa, sb))
        elif sb:
            out.append((f"{path} added", None, sb))
        elif sa:
            out.append((f"{path} removed", sa, None))
        return out or [(path, a, b)]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            out += _json_paths(x, y, f"{path}[{i}]", refs)
        return out
    return [] if same(a, b) else [(path, a, b)]
