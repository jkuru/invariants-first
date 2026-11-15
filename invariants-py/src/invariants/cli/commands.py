"""The `inv` subcommands: validate, lock, verify, scan, status, init, check and mcp (hook is in hooks.py)."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
from typing import TYPE_CHECKING, Any, Callable, Sequence

import invariants_scan as scanner

from .. import lock, status_report
from ..confinement import confined
from ..errors import CoverageError, LoadError
from ..loader import read_document, schema_path
from ..loader.reading import read_located
from ..registry import isolated
from .common import (ABLATED, ALLOW_WEAKENING_ENV, DEFAULT_FILES, FAILED, OK, UNRESOLVED, Usage, _captured, _compare,
                     _describe, _err, _out, _print_changes, _rule_file)
from .git_base import _base_step, base_read, base_scan_default
from ..project import (_contract_modules, _exclude, _import_path, _load_app, _roots, outside_roots, scan_settings,
                      source_dirs, write_scan_settings)
from .static_guards import guard_problems
from .worker import Imported, emit, imported, reraise

if TYPE_CHECKING:
    from ..doors import DoorsFound


UNRESOLVED_HINT = ("a person who reviewed an unresolved site can accept it in {where} "
                   "(at, code, sha, why); `inv scan --format json` gives each finding's sha")


def unresolved_hint(rids: Sequence[str], short_doors: set[str]) -> str:
    """UNRESOLVED_HINT spelled for each failing rule's form: `allow_unresolved:` at the top of a
    door:/sink: rule, mediation.allow_unresolved in a long-form one."""
    short = [r for r in rids if r in short_doors]
    long = [r for r in rids if r not in short_doors]
    if not short:
        where = "mediation.allow_unresolved"
    elif not long:
        where = "allow_unresolved (at the top of the rule, beside door: and sink:)"
    else:
        where = (f"allow_unresolved at the top of {', '.join(short)} (door:/sink: rules) or "
                 f"mediation.allow_unresolved in {', '.join(long)}")
    return UNRESOLVED_HINT.format(where=where)


def _new_accepts(base: Any, raw_rules: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """(rule id, at) of each allow_unresolved entry the rule file has and the lock at --base does not
    (by code and sha, one for one): the sites accepted since the base."""
    stored = base[0] if isinstance(base, tuple) else None
    if not isinstance(stored, dict):
        return set()
    out = set()
    for r in raw_rules:
        rid, med = r.get("id"), r.get("mediation")
        entries = med.get("allow_unresolved") if isinstance(med, dict) else None
        if not isinstance(rid, str) or not isinstance(entries, list):
            continue
        old = (((stored.get("rules") or {}).get(rid) or {}).get("semantic") or {}).get("mediation") or {}
        had = [(e.get("code"), e.get("sha")) for e in old.get("allow_unresolved") or [] if isinstance(e, dict)]
        for e in entries:
            if not isinstance(e, dict):
                continue
            if (e.get("code"), e.get("sha")) in had:
                had.remove((e.get("code"), e.get("sha")))
            elif isinstance(e.get("at"), str):
                out.add((rid, e["at"]))
    return out


def _is_new(line: str, new: set[tuple[str, str]]) -> bool:
    """An `accepted RULE FILE:LINE:COL ...` line of the scan names a site in `new` (an at may omit the column)."""
    parts = line.split()
    if len(parts) < 3:
        return False
    rid, where = parts[1], parts[2]
    return any(r == rid and (where == at or where.startswith(at + ":")) for r, at in new)
NO_JSONSCHEMA ="jsonschema is not installed; schema check skipped (the loader checks still run)"


def _schema_errors(doc: Any) -> list[str] | None:
    """Messages from the bundled JSON Schema, or None when jsonschema is not installed."""
    try:
        import jsonschema
    except ImportError:
        return None
    schema = json.loads(schema_path().read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"schema: {'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errs]


# the checks `inv validate` runs on the raw document besides the schema and the loader, each with the
# label its problems are printed under (`lockfile: R-1: applies_to pattern ...`)
VALIDATE_CHECKS: tuple[tuple[str, Callable[[dict[str, Any]], Any]], ...] = (
    ("scan", scanner.mediations),
    ("lockfile", lock.expand_applies_to),
)


def static_problems(doc: Any) -> tuple[list[str], bool]:
    """What `inv validate` finds in the raw document without importing anything: the bundled JSON
    Schema's messages and VALIDATE_CHECKS' (each labelled), and whether the schema check ran."""
    problems: list[str] = []
    schema = _schema_errors(doc)
    for label, check in VALIDATE_CHECKS:
        try:
            check(doc)
        # a malformed document (targets: a list, a rule that is not a mapping) is a problem to report,
        # whether or not the schema check ran before it to explain why
        except (LoadError, scanner.ScanConfigError, KeyError, TypeError, AttributeError, ValueError) as e:
            problems.append(f"{label}: {e}")
    return problems + _schema_lines(schema or []), schema is not None


def cmd_validate(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    imp = imported(a, path)
    try:
        doc = imp.doc if imp.doc is not None else read_document(path)
    except LoadError as e:
        _err(f"invalid: {e}")
        return FAILED
    problems, schema_ran = static_problems(doc)
    if not schema_ran:
        _err(f"note: {NO_JSONSCHEMA}")
    loaded = imp.load()
    emit(loaded.get("output", ""))
    if "raised" in loaded:
        reraise(loaded["raised"])
    if "problem" in loaded:
        problems.insert(_specific(problems), loaded["problem"])
    if problems:
        _err(f"invalid: {path}")
        for p in problems:
            _err(f"  {p}")
        return FAILED
    for n in loaded.get("notes") or []:
        _out(f"note: {n}")
    ids = [r["id"] for r in doc.get("rules") or []]
    _out(f"ok: {path}: {len(ids)} rule(s) {', '.join(ids)}")
    return OK


SCHEMA_LINE = 200


def _schema_lines(schema: list[str]) -> list[str]:
    """The JSON Schema's messages, each cut to SCHEMA_LINE characters: a oneOf failure quotes the
    whole value, and the loader's message for the same place says what is wrong in words."""
    out = []
    for line in schema:
        if len(line) > SCHEMA_LINE:
            line = line[:SCHEMA_LINE - 4] + " ..."
        out.append(line)
    return out


def _specific(problems: list[str]) -> int:
    """Where the loader's message goes: after the other specific messages, before the schema's."""
    return next((i for i, p in enumerate(problems) if p.startswith("schema: ")), len(problems))


def update_artifacts(path: pathlib.Path) -> list[str]:
    """Rewrite each stale `artifact: FILE#sha256:...` digest in the rule file in place. Returns what changed."""
    import hashlib
    text, changed = path.read_text(encoding="utf-8"), []
    for raw in read_document(path).get("rules") or []:
        for enf in (raw.get("enforcers") or []) if isinstance(raw, dict) else []:
            art = enf.get("artifact") if isinstance(enf, dict) else None
            if not isinstance(art, str) or "#sha256:" not in art:
                continue
            rel, _, old = art.partition("#sha256:")
            f = confined(path.resolve().parent / rel, f"{raw.get('id')}: artifact {rel}")
            if not f.is_file():
                raise LoadError(f"{raw.get('id')}: artifact {rel} missing")
            new = hashlib.sha256(f.read_bytes()).hexdigest()
            if new != old:
                if text.count(old) != 1:
                    raise LoadError(f"{raw.get('id')}: cannot find the digest of {rel} once in {path}; edit it by hand")
                text = text.replace(old, new)
                changed.append(f"{raw.get('id')}: {rel} sha256 {old[:12]}... -> {new[:12]}...")
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


def cmd_lock(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    if a.check and a.update_artifacts:
        raise Usage("--update-artifacts writes the rule file; it cannot be combined with --check")
    if a.check and a.accept_weakening:
        raise Usage("--accept-weakening re-pins the lock; it cannot be combined with --check")
    if a.update_artifacts:
        _report_updated_artifacts(path, a.accept_weakening)
    lock_path = pathlib.Path(a.lock) if a.lock else path.with_name(lock.LOCK_NAME)
    imp = imported(a, path)
    doc = imp.doc if imp.doc is not None and not a.update_artifacts else read_document(path)
    imp.doc = doc
    if _refuse_invalid(a, path, doc, imp):
        return FAILED
    stored_read = _lock_inputs(a, path, lock_path, doc, imp)
    fresh = _fresh_lock(a, path, doc, imp)
    stored: dict[str, Any] | None = None
    if isinstance(stored_read, BaseException):
        if isinstance(stored_read, lock.LockVersionError) or not a.check:
            raise stored_read
        _err(f"stale: {stored_read}")
        return FAILED
    stored = stored_read
    changes: list[lock.Change] = []
    header = ""
    if stored is not None:
        damaged = lock.schema_problems(stored)
        if a.check and _damaged(damaged, lock_path):
            return FAILED
        changes, header = _against_stored(stored, damaged, fresh, imp, path, lock_path)
    if a.check:
        return _check_result(changes, path, lock_path)
    return _write_lock(a, lock_path, stored, fresh, changes, header)


def _refuse_invalid(a: argparse.Namespace, path: pathlib.Path, doc: dict[str, Any], imp: Imported) -> bool:
    """`inv lock` on a rule file `inv validate` rejects: say so as validate does (True), and pin nothing.
    The loader's own refusal is raised by the pin step as before."""
    problems, _ = static_problems(doc)
    if not problems:
        return False
    loaded = imp.load()
    if "problem" in loaded:
        problems.insert(_specific(problems), loaded["problem"])
    _err(f"invalid: {path}")
    for p in problems:
        _err(f"  {p}")
    _err(f"`inv lock` pins only a rule file `inv validate` accepts; fix {path.name}, then run `inv lock` again")
    return True


def _lock_inputs(a: argparse.Namespace, path: pathlib.Path, lock_path: pathlib.Path, doc: dict[str, Any],
                 imp: Imported) -> dict[str, Any] | BaseException | None:
    """Everything `inv lock` reads besides the pins, read before the child that imports the app runs
    (it could rewrite a file): the stored lock (returned: the lock, the error reading it raised, or
    None when there is none and not --check), the scan settings, and the scan a lock that pinned none
    is compared with. Registers the pins and the stored lock's clause types with the child."""
    exists, stored = _read_stored(imp, lock_path)
    stored_read = stored if exists or a.check else None
    old = _old_lock(stored_read)
    if old is not None:
        imp.want_old(old)
    scan, scan_error = imp.before(("scan", tuple(a.root), tuple(a.exclude)), lambda: _scan_for_lock(a, path, doc))
    imp.want_build(scan, scan_error is not None)
    if old is not None and "scan" not in old and scan is not None:
        with contextlib.suppress(Exception):         # raised where it was asked for, as before
            _scan_default(imp, path)
    return stored_read


def _read_stored(imp: Imported, lock_path: pathlib.Path) -> tuple[bool, dict[str, Any] | BaseException]:
    """Whether the lock file exists, and the stored lock or the error reading it raised."""
    def read() -> tuple[bool, dict[str, Any] | BaseException]:
        try:
            return lock_path.exists(), lock.read(lock_path)
        except LoadError as e:
            return lock_path.exists(), e
    got: tuple[bool, dict[str, Any] | BaseException] = imp.before(("stored", str(lock_path)), read)
    return got


def _scan_default(imp: Imported, path: pathlib.Path) -> dict[str, Any]:
    """The scan a stored lock that pinned none scanned: the roots and excludes of the configuration."""
    got: dict[str, Any] = imp.before("scan-default",
                                     lambda: scan_settings(argparse.Namespace(root=[], exclude=[]), path))
    return got


def _old_lock(stored: dict[str, Any] | BaseException | None) -> dict[str, Any] | None:
    """The stored lock a comparison reads (its damaged schema entries dropped), if it can be read."""
    if not isinstance(stored, dict):
        return None
    return lock.without_damaged_schemas(stored) if lock.schema_problems(stored) else stored


def _report_updated_artifacts(path: pathlib.Path, accept_weakening: bool) -> None:
    """`inv lock --update-artifacts`: rewrite the stale artifact digests and say what changed."""
    updated = update_artifacts(path)
    for c in updated:
        _out(f"updated {c}")
    if updated and not accept_weakening:
        _out("note: an artifact digest is a possible weakening; the lock is re-pinned only with "
             "--accept-weakening once the artifact and its conformance test are reviewed")


def _fresh_lock(a: argparse.Namespace, path: pathlib.Path, doc: dict[str, Any],
                imp: Imported) -> dict[str, Any]:
    """The lock the rule file pins now, computed in the child that imports the app from this checkout."""
    scan, scan_error = imp.before(("scan", tuple(a.root), tuple(a.exclude)), lambda: _scan_for_lock(a, path, doc))
    got = imp.build(scan, scan_error is not None)
    if scan_error is not None:
        raise scan_error
    for w in got.get("warnings") or []:
        _err(w)
    fresh: dict[str, Any] = got["fresh"]
    return fresh


def _scan_for_lock(a: argparse.Namespace, path: pathlib.Path,
                   doc: dict[str, Any]) -> tuple[dict[str, Any] | None, Usage | None]:
    """The scan settings the lock pins (None when no rule has a sink), or the usage error computing
    them raised (raised once the rule file is known to load, as before)."""
    if not lock.mediated(doc):
        return None, None
    try:
        return scan_settings(a, path), None
    except Usage as e:
        return None, e


def contract_problem(name: str, c: Any, base_dir: pathlib.Path) -> str | None:
    """Why `inv lock` cannot pin one contract (naming the contract and its ref), else None. Imports
    the contract's module, as `inv lock` does."""
    try:
        lock.contract_hash(name, c if isinstance(c, dict) else {}, base_dir)
    except LoadError as e:
        return str(e)
    except Exception as e:                       # pydantic cannot build the model's JSON Schema, say
        ref = c.get("ref") or c.get("app") if isinstance(c, dict) else None
        first = (str(e).strip().splitlines() or [""])[0]
        return f"contract {name} ({ref}) cannot be pinned: {type(e).__name__}: {first}"
    return None


def contract_problems(doc: dict[str, Any], base_dir: pathlib.Path) -> list[str]:
    """contract_problem for every contract of a rule document."""
    contracts = doc.get("contracts") or {}
    found = [contract_problem(n, c, base_dir) for n, c in contracts.items()] if isinstance(contracts, dict) else []
    return [p for p in found if p]


def _damaged(damaged: list[str], lock_path: pathlib.Path) -> bool:
    """`inv lock --check` on a stored lock with a damaged schemas table: say why it is stale (True),
    else False."""
    if not damaged:
        return False
    _err(f"stale: {lock_path}: the schemas table is damaged ({len(damaged)} problem(s)):")
    for d in damaged:
        _err(f"  {d}")
    _err("run `inv lock` to rewrite it (it still compares rules against the stored lock)")
    return True


def _against_stored(stored: dict[str, Any], damaged: list[str], fresh: dict[str, Any],
                    imp: Imported, path: pathlib.Path,
                    lock_path: pathlib.Path) -> tuple[list[lock.Change], str]:
    """The changes from a current-version stored lock (a damaged schemas table left out) to the fresh one."""
    if damaged:
        _out(f"note: {lock_path}: the schemas table is damaged and is rewritten; changes to the "
             f"schemas it held cannot be shown:")
        for d in damaged:
            _out(f"  {d}")
    old = lock.without_damaged_schemas(stored) if damaged else stored
    default = _scan_default(imp, path) if "scan" not in old and "scan" in fresh else None
    types = imp.types(old)
    changes = _compare(lambda: lock.changes(old, fresh, types, default), lock_path)
    return changes, f"{lock_path}: {len(changes)} change(s) against the stored lock"


_MOVED_SITE = "mediation.allow_unresolved: moved "      # lock.rule_diff's change for a site that moved


def _check_result(changes: list[lock.Change], path: pathlib.Path, lock_path: pathlib.Path) -> int:
    """`inv lock --check`: fresh (informational changes listed), or stale with the changes listed."""
    weak = [c for c in changes if c.weakened]
    # a prose edit (statement, why, owner, class) or a signature spelled differently is recorded in
    # the lock but changes no rule's meaning: reported, never stale on its own
    if any(not c.info for c in changes):
        _err(f"stale: {lock_path} does not match {path}: {len(changes)} change(s)"
             + (f", {len(weak)} WEAKENED" if weak else ""))
        _print_changes(changes, err=True)
        if all(c.info or (c.what.startswith(_MOVED_SITE) and not c.weakened) for c in changes):
            # fix round 30: a moved site weakens nothing, and --relock re-pins exactly such a change
            _err("only reviewed sites moved (the code above them changed), which weakens nothing: run "
                 "`inv check --relock` to re-pin them")
        else:
            _err("review the changes, then run `inv lock" + (" --accept-weakening`" if weak else "`"))
        return FAILED
    if changes:
        _out(f"fresh: {lock_path} ({len(changes)} informational change(s), not stale; `inv lock` "
             f"refreshes them)")
        _print_changes(changes)
        return OK
    _out(f"fresh: {lock_path}")
    return OK


def _write_lock(a: argparse.Namespace, lock_path: pathlib.Path, stored: dict[str, Any] | None,
                fresh: dict[str, Any], changes: list[lock.Change], header: str) -> int:
    """Show the changes and write the fresh lock, unless one weakens a rule without --accept-weakening."""
    weak = [c for c in changes if c.weakened]
    if header and changes:
        _out(header)
        _print_changes(changes)
    if weak and not a.accept_weakening:
        _err(f"refused: {len(weak)} change(s) weaken a rule; the lock was not written. Review them and re-run "
             f"`inv lock --accept-weakening` to accept them.")
        return FAILED
    if stored is not None and not changes and lock.same(stored, fresh):
        _out(f"fresh: {lock_path} (unchanged)")
        return OK
    lock_path.write_text(lock.dumps(fresh), encoding="utf-8")
    _out(f"wrote {lock_path}: {len(fresh['rules'])} rule(s), {len(fresh['targets'])} target(s), "
         f"{len(fresh['providers'])} provider(s), {len(fresh['contracts'])} contract(s), "
         f"{len(fresh['schemas'])} schema(s)"
         + (f"; accepted {len(weak)} weakening(s)" if weak else ""))
    return OK


def cmd_verify(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    with _import_path(path, a.path), isolated() as reg:
        _load_app(path, a.imports)
        # the guards checked are those in the targets' packages and in each --import's package: a
        # guard on a targetless rule is found only in what is imported
        modules = reg.scope() | {m.split(".")[0] for m in a.imports}
        try:
            reg.verify(sorted(modules))
            ok, problems = True, []
        except CoverageError as e:
            ok, problems = False, str(e).split("; ")
        n = sum(1 for r in reg.rules.values() if r.target is not None)
        checked = _imported_in(modules)
        for w in outside_roots(path, read_document(path), a.path):
            _err(w)
    if a.json:
        _out(json.dumps({"passed": ok, "problems": problems, "targeted_rules": n}, indent=2))
    elif ok:
        _out(f"verify: pass ({n} targeted rule(s) guarded; {_checked_text(checked)})")
    else:
        _err("verify: FAIL")
        for p in problems:
            _err(f"  {p}")
    return OK if ok else FAILED


def _imported_in(packages: set[str]) -> list[str]:
    """The modules of these packages that are imported: verify checks the guards in these only."""
    import sys
    return sorted(m for m in list(sys.modules) if any(m == p or m.startswith(p + ".") for p in packages))


def _checked_text(checked: list[str]) -> str:
    """What verify's guard check covered: the imported modules, never a module nothing imported."""
    if not checked:
        return ("no guard module was imported, so no guard was checked; to check one, name a guarded "
                "function as a rule's target or pass --import MODULE")
    return (f"checked the guards in {len(checked)} imported module(s), each names a rule; a module nothing "
            f"imports is not checked: add it with --import MODULE")


def cmd_scan(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    doc = read_document(path)
    report = scanner.scan(doc, _roots(a, path), _exclude(a, path))
    if a.format == "sarif":
        from .. import __version__
        text = json.dumps(scanner.to_sarif(report, __version__), indent=2)
    elif a.format == "json":
        text = json.dumps(report.as_dict(), indent=2)
    elif not report.scanned and not report.findings and not report.ablated_enforce:
        # no rule names a sink: say so in plain words, rather than one "not scanned" line per rule
        text = "\n".join([f"no door rules to check ({report.files} file(s) scanned); a rule with door: and sink: "
                          f"(or guard: and sink:) is what `inv scan` checks", *(f"note: {n}" for n in report.notes)])
    else:
        text = scanner.to_text(report)
    if a.output:
        pathlib.Path(a.output).write_text(text + "\n", encoding="utf-8")
    else:
        _out(text)
    kinds = {f.kind for f in report.findings}
    if kinds & {"bypass", "forbidden", "missing"}:
        return FAILED
    if report.ablated_enforce:
        _err(f"inv scan: enforce rule(s) {', '.join(report.ablated_enforce)} skipped because they are ablated "
             f"(INV_ABLATE); this is not a pass")
        return ABLATED
    return UNRESOLVED if "unresolved" in kinds else OK


def cmd_status(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    ablation = None
    if a.ablation:
        try:
            ablation = status_report.load_ablation(a.ablation)
        except (OSError, ValueError) as e:
            raise Usage(f"--ablation: {e}") from e
    roots = _roots(a, path)
    with _import_path(path, a.path), isolated() as reg:
        _load_app(path, a.imports)
        report = scanner.scan(read_document(path), roots, _exclude(a, path))
        data = status_report.build(reg, report, ablation, a.imports)
    _out(json.dumps(data, indent=2) if a.json else status_report.to_text(data))
    return OK


def cmd_init(a: argparse.Namespace) -> int:
    from .. import bootstrap
    path = pathlib.Path(a.rulefile or DEFAULT_FILES[0])
    existing = [p for p in ([path] if a.rulefile else [pathlib.Path(n) for n in DEFAULT_FILES]) if p.exists()]
    if existing:
        raise Usage(_already_exists(existing[0]))
    project = path.resolve().parent
    roots = [pathlib.Path(r) for r in a.root] or source_dirs(path) or [project]
    for r in roots:
        if not r.exists():
            raise Usage(f"source root {r} does not exist")
    found = bootstrap.discover(project, roots, a.exclude)
    if not found.files:
        # nothing to draft from: almost always the wrong directory or --root, and a rule file written
        # anyway would make the rerun from the right place fail with "already exists"
        raise Usage(f"found no Python source file to read under {', '.join(str(r) for r in roots)}; nothing "
                    f"was written. Run `inv init` in your project's directory, or pass --root DIR (the "
                    f"directory that holds your package)")
    if not a.no_doors:
        found.doors = _init_doors(a, path, project)
    if not a.no_lock:
        _check_drafted_contracts(found, path, a.path)
    path.write_text(bootstrap.render(found, locked=not a.no_lock), encoding="utf-8")
    _out(f"wrote {path}")
    for line in bootstrap.summary(found):
        _out(f"  {line}")
    # fix round 30: what init scanned goes in [tool.invariants.scan], so `inv check` (and the lock) scan it
    base = path.resolve().parent
    for line in write_scan_settings(path, [pathlib.Path(os.path.relpath(pathlib.Path(r).resolve(), base)).as_posix()
                                           for r in a.root], list(a.exclude)):
        _out(line)
    if a.no_lock:
        _out("next: review it, run `inv lock`, and commit both files")
        return OK
    code = _init_lock(a, path)
    if code != OK:                  # nothing was pinned: the header says to run `inv lock`
        path.write_text(bootstrap.render(found), encoding="utf-8")
    return code


def _check_drafted_contracts(found: Any, path: pathlib.Path, extra: Sequence[str]) -> None:
    """Try the lock's pin on each contract `inv init` drafts (importing the model as `inv lock` will),
    so one pydantic cannot give a JSON Schema for is written commented out, with the reason, instead
    of failing the lock step."""
    from .. import bootstrap
    drafted = list(bootstrap.contracts(found))
    if not drafted:
        return
    imp = Imported(path, extra)
    imp.want_contracts([(name, bootstrap.contract_record(m)) for name, m in drafted])
    for (_, m), problem in zip(drafted, imp.contracts()):
        m.problem = problem


def _already_exists(path: pathlib.Path) -> str:
    """`inv init` on an existing rule file: it never overwrites one; without a lock, the next step is `inv lock`."""
    text = f"{path} already exists; inv init never overwrites a rule file"
    if not path.with_name(lock.LOCK_NAME).exists():
        text += (f". It has no {lock.LOCK_NAME} yet (an earlier `inv init` may have stopped at its lock step): "
                 f"review it, then run `inv lock {path}`; or delete {path} to draft it again")
    return text


def _init_doors(a: argparse.Namespace, path: pathlib.Path, project: pathlib.Path) -> DoorsFound:
    """`inv init`'s door rules, found with Graphify's hints unless --no-graphify."""
    from .. import doors
    # the same roots and excludes `inv check` will scan, so what init freezes is what check sees
    from .. import graphify
    graph = None if a.no_graphify else graphify.find(project, a.graphify)
    try:
        return doors.discover(_roots(argparse.Namespace(root=a.root), path),
                              _exclude(argparse.Namespace(exclude=a.exclude), path), limit=a.max_doors,
                              graph=graph, project=project)
    except graphify.GraphifyError as e:
        raise Usage(f"--graphify: {e}") from e


def _has_path_law(path: pathlib.Path) -> bool:
    """The rule file holds a rule `inv check` scans (as cmd_check decides: one with mediation)."""
    try:
        doc = read_document(path)
    except Exception:
        return True                                  # unreadable here: say nothing rather than guess
    return any(isinstance(r, dict) and r.get("mediation") for r in doc.get("rules") or [])


_DOOR_DRAFT = re.compile(r"^[ \t]*#[ \t]*-[ \t]*id:.*\n[ \t]*#[ \t]+door:", re.MULTILINE)


def _has_door_draft(path: pathlib.Path) -> bool:
    """The rule file holds a door rule `inv init` wrote commented out (`# - id: ...` then `#   door:`)."""
    try:
        return bool(_DOOR_DRAFT.search(path.read_text(encoding="utf-8")))
    except OSError:
        return False


def _init_lock(a: argparse.Namespace, path: pathlib.Path) -> int:
    """`inv init`'s last step: `inv lock` on the rule file it wrote, and what to do next."""
    _out("running `inv lock` to pin them ...")
    try:
        code = cmd_lock(argparse.Namespace(rulefile=str(path), check=False, update_artifacts=False, lock=None,
                                           accept_weakening=False, path=a.path, root=[], exclude=[]))
    except (LoadError, scanner.ScanConfigError) as e:
        _err(f"inv init: {_describe(e)}")
        code = FAILED
    except Exception as e:          # a model module failing at import, for example
        _err(f"inv init: {_describe(e, typed=True)}")
        code = FAILED
    if code == OK:
        _out("next: commit invariants.yaml and invariants.lock, and run `inv check` in CI and pre-commit "
             "(the invariants docs: start.md, \"Make the check unskippable\")")
        if not _has_path_law(path):
            drafts = (f"see the commented-out door drafts in {path.name}, or write one by hand"
                      if _has_door_draft(path) else "write one by hand")
            _out("note: no path law (a rule with a sink and a door) was written, so `inv check` will skip its "
                 f"scan step and no back door is checked yet; {drafts} (start.md, \"Path laws\")")
    else:
        _err(f"`inv lock` failed, so no lock was written; {path} is kept. Next: fix what it reports above "
             f"in {path} or in the code it names (a contract or rule `inv lock` cannot pin can be commented "
             f"out), then run `inv lock {path}`. Do not rerun `inv init`: it never overwrites {path} (to "
             f"draft it again, delete it and run `inv init`, or `inv init --no-lock` to skip the lock step).")
    return code


def cmd_check(a: argparse.Namespace) -> int:
    """Everything CI, pre-commit and an agent hook need, with one exit code.

    This process never imports the app: the reviewer's switch is read first, and everything that
    imports (the load, the pins, the guard check) runs in one child (worker.py)."""
    allow_env = getattr(a, "allow_weakening_env", None)
    if allow_env is None:
        allow_env = os.environ.get(ALLOW_WEAKENING_ENV) == "1"
    path = _rule_file(a.rulefile)
    lock_path = path.with_name(lock.LOCK_NAME)
    doc, where = read_located(path)
    raw_rules = [r for r in doc.get("rules") or [] if isinstance(r, dict)]
    importable = _importable(doc, a.imports)
    mediated = any(r.get("mediation") for r in raw_rules)
    scan = {"root": a.root, "exclude": a.exclude}
    imp = _check_imports(a, path, lock_path, doc, importable, scan)
    if importable:
        _static_guards_early(imp, path, a.path, doc)
    failed = False
    _out(f"inv check: {path}")

    def step(name: str, code: int, text: str, ok_line: str | None = None) -> None:
        nonlocal failed
        if code == OK:
            first = ok_line or (text.splitlines()[-1] if text else "ok")
            _out(f"  ok    {name}: {first}")
            return
        failed = True
        _out(f"  FAIL  {name}")
        for line in text.splitlines():
            _out(f"        {line}")

    code, text = _captured(cmd_validate, argparse.Namespace(rulefile=str(path), path=a.path, imported=imp))
    step("rules", code, text, f"{len(raw_rules)} rule(s), {len(doc.get('contracts') or {})} contract(s) load")
    if code == OK and NO_JSONSCHEMA in text:      # on a failure it is among the lines printed already
        _out(f"  note  rules: {NO_JSONSCHEMA}")
    if code != OK:
        _out("fail")
        return FAILED
    code, text = _lock_step(a, path, lock_path, scan, imp)
    step("lock", code, text, None if text.startswith("re-pinned") else f"{lock_path.name} matches")
    new_accepts: set[tuple[str, str]] = set()
    if a.base:
        allow = a.allow_weakening or allow_env
        base = imp.before(("base", a.base), lambda: base_read(lock_path, a.base))
        new_accepts = _new_accepts(base, raw_rules)
        code, text = _captured(lambda ns: _base_step(path, lock_path, a.base, a.path, allow,
                                                     _scan_read_first(ns, path, doc, imp), imp, base),
                               argparse.Namespace(**scan))
        step(f"since {a.base}", code, text, text.splitlines()[0] if text else None)
    if importable:
        code, text = _guards_step(imp, path, a.path, doc)
        step("guards", code, text)
    else:
        _out("  skip  guards: nothing to import (no target, edge target or python contract; a guard on a "
             "targetless rule is checked with --import MODULE)")
    if mediated:
        code, text = imp.before("scan-step", lambda: _captured(cmd_scan, argparse.Namespace(
            rulefile=str(path), root=a.root, exclude=a.exclude, format="text", output=None)))
        accepted = [line for line in text.splitlines() if line.startswith("accepted ")]
        ok_line = "no path skips a door or guard"
        fresh = [line for line in accepted if _is_new(line, new_accepts)]
        if accepted:      # reviewed sites pass, but the reader sees each one, never a bare "no path skips"
            ok_line += f"; {len(accepted)} reviewed unresolved site(s) accepted by allow_unresolved"
            ok_line += f" ({len(fresh)} new since {a.base})" if fresh else ""
        step("scan", code, text, ok_line if code == OK else None)
        if code == OK:
            for line in accepted:       # a site accepted since --base is marked: the change's own acceptance
                _out(f"        {line}" + (f"  [new since {a.base}]" if line in fresh else ""))
        else:
            bare = [line.split()[1] for line in text.splitlines()
                    if line.startswith("unresolved ") and "[sha " not in line and len(line.split()) > 1]
            if bare:
                _out(f"        hint: {unresolved_hint(sorted(set(bare)), where.short_doors)}")
    else:
        _out("  skip  scan: no rule declares a sink or door")
    _out("fail" if failed else "pass")
    return FAILED if failed else OK


def _scan_read_first(a: argparse.Namespace, path: pathlib.Path, doc: dict[str, Any],
                     imp: Imported) -> dict[str, Any] | None:
    """The scan settings the lock step pinned, read from pyproject.toml before the child ran (never
    again after it: the imported app could have rewritten pyproject.toml back to the base's settings)."""
    scan, scan_error = imp.before(("scan", tuple(a.root), tuple(a.exclude)), lambda: _scan_for_lock(a, path, doc))
    if scan_error is not None:
        raise scan_error
    return scan


def _check_imports(a: argparse.Namespace, path: pathlib.Path, lock_path: pathlib.Path, doc: dict[str, Any],
                   importable: list[str], scan: dict[str, Any]) -> Imported:
    """The one child `inv check` runs: the load, and (when the rule file passes the checks that import
    nothing) the pins, the clause types against the stored lock, and the guard check."""
    imp = Imported(path, a.path, doc)
    imp.want_load()
    try:
        clean = not static_problems(doc)[0]
    except Exception:
        clean = False
    if not clean:
        return imp
    # what the verdict reads is read now, before the child runs: the stored lock, the lock at --base,
    # the scan settings and the scan itself (the child could rewrite a source file)
    _lock_inputs(argparse.Namespace(check=True, **scan), path, lock_path, doc, imp)
    if a.base:
        base = imp.before(("base", a.base), lambda: base_read(lock_path, a.base))
        if isinstance(base, tuple) and base[0] is not None:
            imp.want_old(base[0])
            if "scan" not in base[0] and lock.mediated(doc):
                with contextlib.suppress(Exception):
                    base_scan_default(imp, path, a.base)
    if importable:
        imp.want_verify(a.imports)
    if any(isinstance(r, dict) and r.get("mediation") for r in doc.get("rules") or []):
        imp.before("scan-step", lambda: _captured(cmd_scan, argparse.Namespace(
            rulefile=str(path), root=a.root, exclude=a.exclude, format="text", output=None)))
    return imp


def _static_guards(imp: Imported, path: pathlib.Path, extra: Sequence[str], doc: dict[str, Any]) -> tuple[int, list[str]]:
    """The guard check read from the source (static_guards), in this process, before the child runs."""
    got: tuple[int, list[str]] = imp.before("static-guards", lambda: guard_problems(path, doc, extra))
    return got


def _static_guards_early(imp: Imported, path: pathlib.Path, extra: Sequence[str], doc: dict[str, Any]) -> None:
    """Read the guards from the source before the child runs (it could rewrite a source file); an
    error is kept and reported by the guards step."""
    with contextlib.suppress(Exception):
        _static_guards(imp, path, extra, doc)


def _guards_step(imp: Imported, path: pathlib.Path, extra: Sequence[str], doc: dict[str, Any]) -> tuple[int, str]:
    """`inv check`'s guards step: the decorators read from the source in this process, and the child's
    `inv verify`; it passes only when both do. The child's verdict alone is never enough: the code it
    imports could have replaced the check that computed it."""
    try:
        _, problems = _static_guards(imp, path, extra, doc)
    except Exception as e:
        problems = [f"the guards could not be read from the source ({_describe(e, typed=True)}); "
                          f"`inv check` fails closed"]
    code, text = imp.verify()
    if problems:
        lines = ["source: FAIL", *(f"  {p}" for p in problems)]
        return FAILED, "\n".join([*lines, *([text] if text and code != OK else [])])
    if code != OK:
        return code, text
    return OK, text


def _importable(doc: dict[str, Any], imports: Sequence[str]) -> list[str]:
    """What `inv verify` imports from this rule file, in order: the targets, rule targets and edge
    targets it resolves, the python, schema-class (dataclass, pydantic or schema `module:Class`) and
    openapi-app contract modules, and the --import modules. Verify checks guards and registry verifiers
    (an installed FastAPI app) only in what is imported, so with none of these it can find nothing."""
    targets = doc.get("targets")
    keys: list[Any] = list(targets) if isinstance(targets, dict) else []
    for r in doc.get("rules") or []:
        if not isinstance(r, dict):
            continue
        subject = r.get("subject")
        keys.append(subject.get("target") if isinstance(subject, dict) else None)
        keys.extend(e.get("target") for e in r.get("enforcers") or [] if isinstance(e, dict))
    return [*(k for k in keys if isinstance(k, str) and k), *_contract_modules(doc), *imports]


def _lock_step(a: argparse.Namespace, path: pathlib.Path, lock_path: pathlib.Path,
               scan: dict[str, Any], imp: Imported) -> tuple[int, str]:
    """`inv check`'s lock step: `inv lock --check`, and with --relock a re-pin that refuses a weakening."""
    code, text = _captured(cmd_lock, argparse.Namespace(rulefile=str(path), check=True, update_artifacts=False,
                                                        lock=None, accept_weakening=False, path=a.path,
                                                        imported=imp, **scan))
    if code != OK and a.relock and lock_path.exists():
        code, text = _captured(cmd_lock, argparse.Namespace(rulefile=str(path), check=False,
                                                            update_artifacts=False, lock=None,
                                                            accept_weakening=False, path=a.path,
                                                            imported=imp, **scan))
        if code == OK:
            text = f"re-pinned {lock_path.name}: no change weakens a rule (review the lock diff)"
            if a.fail_on_repin:
                code, text = FAILED, f"{lock_path.name} re-pinned; review and `git add {lock_path.name}`"
    return code, text


def cmd_mcp(a: argparse.Namespace) -> int:
    from .. import mcp
    # the rule file is named from the current directory, as for every `inv` command; inv-mcp's own
    # --rules is relative to --root, so it is handed over absolute (never joined to --root twice)
    rules = str(pathlib.Path(a.rulefile).absolute()) if a.rulefile else None
    return mcp.main(["--root", a.root, *(["--rules", rules] if rules else [])])
