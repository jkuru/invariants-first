"""`inv-scan` (`python -m invariants_scan`): the scan over a JSON rule document, with the standard library
only. The document holds `rules`, each with `id` and a `mediation` section (sink, door, allow, ...): the
long form. A YAML rule file, the short forms (door:, sink:, guard:) and the pyproject.toml settings are
read by invariants-first's `inv scan`, which runs this same scan.

Exit codes: 0 no finding (or only accepted ones), 1 a bypass, forbidden or missing finding, 2 a usage or
rule-document error, 3 only unresolved findings, 4 an enforce rule was skipped because it is ablated.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from importlib import metadata
from typing import Sequence

from . import ScanConfigError, scan, to_sarif, to_text

OK, FAILED, USAGE, UNRESOLVED, ABLATED = 0, 1, 2, 3, 4


def _version() -> str:
    try:
        return metadata.version("invariants-scan")
    except metadata.PackageNotFoundError:     # running from a source tree that is not installed
        return "0.0.0+unknown"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inv-scan", description=(
        "Check that every call path to a protected sink passes its door: a static, read-only scan of the "
        "Python source (nothing is imported or run)."))
    p.add_argument("rules", help="a JSON rule document: {\"rules\": [{\"id\": ..., \"mediation\": {...}}]}")
    p.add_argument("--root", action="append", default=[], metavar="DIR",
                   help="a source root to scan (repeatable; default: the rule document's directory)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip files matching GLOB")
    p.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    p.add_argument("--output", metavar="FILE", help="write the report to FILE instead of stdout")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    path = pathlib.Path(a.rules)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"inv-scan: {path}: {e}", file=sys.stderr)
        return USAGE
    if not isinstance(doc, dict):
        print(f"inv-scan: {path}: a rule document is a JSON object with a rules list", file=sys.stderr)
        return USAGE
    roots = [pathlib.Path(r) for r in a.root] or [path.resolve().parent]
    for r in roots:
        if not r.exists():
            print(f"inv-scan: source root {r} does not exist", file=sys.stderr)
            return USAGE
    try:
        report = scan(doc, roots, a.exclude)
    except ScanConfigError as e:
        print(f"inv-scan: {path}: {e}", file=sys.stderr)
        return USAGE
    if a.format == "sarif":
        text = json.dumps(to_sarif(report, _version()), indent=2)
    elif a.format == "json":
        text = json.dumps(report.as_dict(), indent=2)
    else:
        text = to_text(report)
    if a.output:
        pathlib.Path(a.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    kinds = {f.kind for f in report.findings}
    if kinds & {"bypass", "forbidden", "missing"}:
        return FAILED
    if report.ablated_enforce:
        print(f"inv-scan: enforce rule(s) {', '.join(report.ablated_enforce)} skipped because they are ablated; "
              f"this is not a pass", file=sys.stderr)
        return ABLATED
    return UNRESOLVED if "unresolved" in kinds else OK


if __name__ == "__main__":
    sys.exit(main())
