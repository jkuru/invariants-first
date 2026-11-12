"""Hints from a Graphify graph (graphify-out/graph.json) for `inv init` door discovery. Optional, read-only.

Graphify (https://github.com/Graphify-Labs/graphify) parses a codebase with tree-sitter, links docs to
code, and clusters the graph into communities (Leiden, else Louvain). `inv init` can read its graph to
find doors its naming heuristics miss: a function that sits between two communities (an API layer
and a repository layer, say) is a door candidate even when nothing is named Service or Repo, and the
calls Graphify resolved (a tool registry, a dispatch table) count as callers.

The graph only proposes. Every door rule it suggests is checked by the real scan (doors._freeze)
before it is written, and `inv check` never reads the graph: the scan's own extracted call edges are
the only evidence a path law is judged on. Nothing here imports Graphify or runs it; the file is
read as plain JSON, and nothing at run time depends on it.

What is read (graphify v8, schema_version 1): nodes with `source_file` and `source_location`
("L42") and an optional `community`; links with `source`, `target`, `relation` and `confidence`
(EXTRACTED | INFERRED | AMBIGUOUS). Node ids are not interpreted: a node is matched to a scanned
function by its file and line, so a change in Graphify's id scheme does not matter.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any

from . import scan as scanner

DEFAULT_PATH = pathlib.Path("graphify-out") / "graph.json"
CALL_RELATIONS = frozenset({"calls", "call", "invokes", "uses"})   # links that mean "this calls that"
TRUSTED = frozenset({"EXTRACTED", "INFERRED"})                    # AMBIGUOUS links are not used
_LINE = re.compile(r"L?(\d+)")
MAX_BYTES = 200_000_000


class GraphifyError(ValueError):
    """The graph file cannot be read as a Graphify graph."""


@dataclasses.dataclass
class Hints:
    """What the graph says about the scanned functions (keys are the scan's module.qualname ids)."""

    path: str
    nodes: int = 0
    matched: int = 0                                     # nodes matched to a scanned function
    community: dict[str, int] = dataclasses.field(default_factory=dict)
    calls: set[tuple[str, str]] = dataclasses.field(default_factory=set)   # (caller, callee) it resolved

    def summary(self) -> str:
        return (f"read the Graphify graph {self.path}: {self.nodes} node(s), {self.matched} matched to a "
                f"function, {len(set(self.community.values()))} communit(ies), {len(self.calls)} call(s) used")


def read(path: pathlib.Path) -> dict[str, Any]:
    """The graph document, checked for the parts `inv init` uses."""
    try:
        if path.stat().st_size > MAX_BYTES:
            raise GraphifyError(f"{path}: larger than {MAX_BYTES} bytes")
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise GraphifyError(f"{path}: {e.strerror or e}") from e
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GraphifyError(f"{path}: not JSON ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list):
        raise GraphifyError(f"{path}: not a Graphify graph (no nodes list)")
    links = doc.get("links", doc.get("edges", []))
    if not isinstance(links, list):
        raise GraphifyError(f"{path}: links must be a list")
    version = (doc.get("graph") or {}).get("schema_version") if isinstance(doc.get("graph"), dict) else None
    if version is not None and version != 1:
        raise GraphifyError(f"{path}: Graphify graph schema_version {version!r} (this reads version 1)")
    return {"nodes": doc["nodes"], "links": links}


def _line(loc: Any) -> int | None:
    if isinstance(loc, int) and not isinstance(loc, bool):
        return loc
    m = _LINE.match(str(loc or "").strip())
    return int(m.group(1)) if m else None


class _Locator:
    """file + line -> the innermost scanned function whose definition spans that line."""

    def __init__(self, g: scanner.Graph, bases: list[pathlib.Path]) -> None:
        self.bases = bases
        self.by_file: dict[str, list[tuple[int, int, str]]] = {}
        for fid, f in g.idx.funcs.items():
            if f.node is None:
                continue
            start = min([f.line, *(d.lineno for d in getattr(f.node, "decorator_list", []))])
            end = getattr(f.node, "end_lineno", None) or f.line
            self.by_file.setdefault(f.file.resolve().as_posix(), []).append((start, end, fid))

    def file(self, source: str) -> str | None:
        p = pathlib.PurePosixPath(source.replace("\\", "/"))
        if p.is_absolute():
            key = pathlib.Path(str(p)).resolve().as_posix()
            return key if key in self.by_file else None
        for base in self.bases:
            key = (base / str(p)).resolve().as_posix()
            if key in self.by_file:
                return key
        tail = "/" + p.as_posix().lstrip("./")
        hits = [k for k in self.by_file if k.endswith(tail)]
        return hits[0] if len(hits) == 1 else None           # an ambiguous tail matches nothing

    def find(self, source: Any, loc: Any) -> str | None:
        line = _line(loc)
        if not isinstance(source, str) or not source or line is None:
            return None
        key = self.file(source)
        if key is None:
            return None
        spans = [(end - start, fid) for start, end, fid in self.by_file[key] if start <= line <= end]
        return min(spans)[1] if spans else None


def hints(path: pathlib.Path, g: scanner.Graph, bases: list[pathlib.Path]) -> Hints:
    """Read the graph at path and map it onto the scan's functions (bases: where relative paths start)."""
    doc = read(path)
    loc = _Locator(g, bases)
    shown = path
    for base in bases[:1]:                               # shown relative to the project when inside it
        try:
            shown = path.resolve().relative_to(base.resolve())
        except ValueError:
            pass
    out = Hints(shown.as_posix(), nodes=len(doc["nodes"]))
    by_id: dict[str, str] = {}
    for n in doc["nodes"]:
        if not isinstance(n, dict) or "id" not in n:
            continue
        fid = loc.find(n.get("source_file"), n.get("source_location"))
        if fid is None:
            continue
        by_id[str(n["id"])] = fid
        out.matched += 1
        c = n.get("community")
        if isinstance(c, int) and not isinstance(c, bool):
            out.community.setdefault(fid, c)
    for e in doc["links"]:
        if not isinstance(e, dict):
            continue
        rel, conf = str(e.get("relation", "")).lower(), str(e.get("confidence", "EXTRACTED")).upper()
        if rel not in CALL_RELATIONS or conf not in TRUSTED:
            continue
        a, b = by_id.get(str(e.get("source"))), by_id.get(str(e.get("target")))
        if a and b and a != b:
            out.calls.add((a, b))
    return out


def find(project: pathlib.Path, given: str | None) -> pathlib.Path | None:
    """The graph to read: the one given, else graphify-out/graph.json in the project when it exists."""
    if given:
        return pathlib.Path(given)
    p = project / DEFAULT_PATH
    return p if p.is_file() else None
