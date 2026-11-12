"""The fact-pipeline command line (a batch worker; no web service)."""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

import invariants as inv

from . import monitor, rules_path
from .fake_model import FAULTS, FakeModel
from .pipeline import Pipeline
from .store import Store


def _fault(s: str) -> tuple[str, int]:
    name, _, times = s.partition(":")
    if name not in FAULTS:
        raise argparse.ArgumentTypeError(f"unknown fault {name!r}; choose from {', '.join(FAULTS)}")
    return name, int(times or 1)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fact-pipeline", description="Ingest documents, extract cited facts, answer.")
    p.add_argument("--db", default="facts.db", help="SQLite file (default: facts.db)")
    p.add_argument("--rules", default=None, help="rule file (default: the project's invariants.yaml)")
    p.add_argument("--inject", action="append", default=[], type=_fault, metavar="FAULT[:N]",
                   help="make the fake model return a bad reply N times (default 1)")
    sub = p.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", help="intake, extract and store documents")
    ing.add_argument("files", nargs="+")
    ing.add_argument("--publisher", required=True)
    ing.add_argument("--trust", choices=("trusted", "untrusted"), default="trusted")
    ing.add_argument("--async", dest="use_async", action="store_true", help="extract chunks concurrently")
    ent = sub.add_parser("add-entity", help="mint an entity with aliases and external records")
    ent.add_argument("name")
    ent.add_argument("--alias", action="append", default=[])
    ent.add_argument("--xref", action="append", default=[], metavar="SYSTEM:KEY")
    sub.add_parser("promote", help="promote every CHECKED fact the promotion door allows")
    ask = sub.add_parser("ask", help="answer a question with citations")
    ask.add_argument("question")
    rm = sub.add_parser("delete-source", help="withdraw a source and retract what rested on it")
    rm.add_argument("source_id")
    sub.add_parser("monitor", help="run the monitor over stored data; exit 1 on any finding")
    facts = sub.add_parser("facts", help="list facts")
    facts.add_argument("--status")
    sub.add_parser("conflicts", help="list Conflict records")
    sub.add_parser("reviews", help="list open reviews")
    rr = sub.add_parser("resolve-review", help="rule that an ambiguous mention means an entity")
    rr.add_argument("review_id", type=int)
    rr.add_argument("entity_id")
    return p


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=lambda o: o.__dict__))


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `fact-pipeline`."""
    a = parser().parse_args(argv)
    try:
        inv.load(a.rules or rules_path())
        inv.verify()
    except (inv.LoadError, inv.CoverageError) as e:
        print(f"fact-pipeline: rules: {e}", file=sys.stderr)
        return 2
    client = FakeModel(dict(a.inject))
    store = Store(a.db)
    pipe = Pipeline(store, client)
    try:
        if a.command == "ingest":
            for f in a.files:
                text = pathlib.Path(f).read_text()
                uri = pathlib.Path(f).resolve().as_uri()
                rep = (asyncio.run(pipe.process_async(uri, text, a.publisher, a.trust)) if a.use_async
                       else pipe.process(uri, text, a.publisher, a.trust))
                _print({"file": f, **rep.__dict__})
        elif a.command == "add-entity":
            xrefs = tuple(tuple(x.split(":", 1)) for x in a.xref)
            print(store.add_entity(a.name, tuple(a.alias), xrefs))  # type: ignore[arg-type]
        elif a.command == "promote":
            _print({"promoted": pipe.promote_all(), "audit_sample": pipe.audit_sample()})
        elif a.command == "ask":
            _print(pipe.answer(a.question))
        elif a.command == "delete-source":
            _print(pipe.delete_source(a.source_id))
        elif a.command == "monitor":
            found = monitor.run(store)
            _print([f.__dict__ for f in found])
            return 1 if found else 0
        elif a.command == "facts":
            _print(store.facts(a.status))
        elif a.command == "conflicts":
            _print(store.conflicts())
        elif a.command == "reviews":
            _print(store.reviews())
        elif a.command == "resolve-review":
            _print(pipe.resolve_review(a.review_id, a.entity_id).__dict__)
    except inv.InvariantError as e:
        print(f"fact-pipeline: {e.rule}: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
