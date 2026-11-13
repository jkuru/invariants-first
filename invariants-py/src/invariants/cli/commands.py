"""The `inv` subcommands: validate, lock, verify, scan, status, init, check and mcp (hook is in hooks.py)."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
from typing import TYPE_CHECKING, Any

from .. import lockfile, scan as scanner, status_report
from ..errors import LoadError
from ..loader import load, read_document, schema_path
from ..registry import isolated
from .common import (ABLATED, ALLOW_WEAKENING_ENV, DEFAULT_FILES, FAILED, OK, UNRESOLVED, Usage, _captured, _compare,
                     _err, _out, _print_changes, _rule_file)
from .git_base import _base_step
from .project import _exclude, _import_path, _load_app, _roots, outside_roots, scan_settings, source_dirs

if TYPE_CHECKING:
    from ..doors import DoorsFound


def _schema_errors(doc: Any) -> list[str] | None:
    """Messages from the bundled JSON Schema, or None when jsonschema is not installed."""
    try:
        import jsonschema
    except ImportError:
        return None
    schema = json.loads(schema_path().read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"schema: {'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errs]


def cmd_validate(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    problems: list[str] = []
    try:
        doc = read_document(path)
    except LoadError as e:
        _err(f"invalid: {e}")
        return FAILED
    schema = _schema_errors(doc)
    if schema is None:
        _err("note: jsonschema is not installed; schema check skipped (the loader checks still run)")
    else:
        problems += schema
    for check in (scanner.mediations, lockfile.expand_applies_to):
        try:
            check(doc)
        except (LoadError, scanner.ScanConfigError, KeyError, TypeError) as e:
            problems.append(f"{check.__module__.rsplit('.', 1)[-1]}: {e}")
    notes: list[str] = []
    with _import_path(path, a.path), isolated():
        try:
            reg = load(path)
            notes = list(reg.notes)
        except LoadError as e:
            problems.append(f"load: {e}")
    if problems:
        _err(f"invalid: {path}")
        for p in problems:
            _err(f"  {p}")
        return FAILED
    for n in notes:
        _out(f"note: {n}")
    ids = [r["id"] for r in doc.get("rules") or []]
    _out(f"ok: {path}: {len(ids)} rule(s) {', '.join(ids)}")
    return OK


def update_artifacts(path: pathlib.Path) -> list[str]:
    """Rewrite each stale `artifact: FILE#sha256:...` digest in the rule file in place. Returns what changed."""
    import hashlib
    text, changed = path.read_text(), []
    for raw in read_document(path).get("rules") or []:
        for enf in (raw.get("enforcers") or []) if isinstance(raw, dict) else []:
            art = enf.get("artifact") if isinstance(enf, dict) else None
            if not isinstance(art, str) or "#sha256:" not in art:
                continue
            rel, _, old = art.partition("#sha256:")
            f = path.resolve().parent / rel
            if not f.is_file():
                raise LoadError(f"{raw.get('id')}: artifact {rel} missing")
            new = hashlib.sha256(f.read_bytes()).hexdigest()
            if new != old:
                if text.count(old) != 1:
                    raise LoadError(f"{raw.get('id')}: cannot find the digest of {rel} once in {path}; edit it by hand")
                text = text.replace(old, new)
                changed.append(f"{raw.get('id')}: {rel} sha256 {old[:12]}... -> {new[:12]}...")
    if changed:
        path.write_text(text)
    return changed


def cmd_lock(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    if a.check and a.update_artifacts:
        raise Usage("--update-artifacts writes the rule file; it cannot be combined with --check")
    if a.check and a.accept_weakening:
        raise Usage("--accept-weakening re-pins the lock; it cannot be combined with --check")
    if a.update_artifacts:
        _report_updated_artifacts(path, a.accept_weakening)
    lock_path = pathlib.Path(a.lock) if a.lock else path.with_name(lockfile.LOCK_NAME)
    doc = read_document(path)
    fresh, types = _fresh_lock(a, path, doc)
    stored: dict[str, Any] | None = None
    if lock_path.exists() or a.check:
        try:
            stored = lockfile.read(lock_path)
        except lockfile.LockVersionError:
            raise
        except LoadError as e:
            if a.check:
                _err(f"stale: {e}")
                return FAILED
            raise
    changes: list[lockfile.Change] = []
    header = ""
    if stored is not None:
        version = lockfile.check_version(stored, lock_path)
        damaged = lockfile.schema_problems(stored) if version >= lockfile.LOCK_VERSION else []
        if a.check and _stale_form(version, damaged, lock_path):
            return FAILED
        changes, header = (_migration(stored, version, doc, fresh, types, lock_path)
                           if version < lockfile.LOCK_VERSION
                           else _against_stored(stored, damaged, fresh, types, path, lock_path))
    if a.check:
        return _check_result(changes, path, lock_path)
    return _write_lock(a, lock_path, stored, fresh, changes, header)


def _report_updated_artifacts(path: pathlib.Path, accept_weakening: bool) -> None:
    """`inv lock --update-artifacts`: rewrite the stale artifact digests and say what changed."""
    updated = update_artifacts(path)
    for c in updated:
        _out(f"updated {c}")
    if updated and not accept_weakening:
        _out("note: an artifact digest is a possible weakening; the lock is re-pinned only with "
             "--accept-weakening once the artifact and its conformance test are reviewed")


def _fresh_lock(a: argparse.Namespace, path: pathlib.Path,
                doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, lockfile.Typer | None]]:
    """The lock the rule file pins now and its clause types, with the app imported from this checkout."""
    with _import_path(path, a.path), isolated() as reg:
        load(path)
        fresh = lockfile.build(doc, path.resolve().parent,
                               scan_settings(a, path) if lockfile.mediated(doc) else None)
        types = lockfile.clause_types(reg)
        for w in outside_roots(path, doc, a.path):
            _err(w)
    return fresh, types


def _stale_form(version: int, damaged: list[str], lock_path: pathlib.Path) -> bool:
    """`inv lock --check` on a stored lock of an older version or with a damaged schemas table: say
    why it is stale (True), else False."""
    if version < lockfile.LOCK_VERSION:
        _err(f"stale: {lock_path} is lock version {version}; this invariants writes version "
             f"{lockfile.LOCK_VERSION}, which pins resolved types and stores each schema once "
             f"without prose. Run `inv lock` once to migrate it (it refuses if a rule was "
             f"weakened since the old pin).")
        return True
    if not damaged:
        return False
    _err(f"stale: {lock_path}: the schemas table is damaged ({len(damaged)} problem(s)):")
    for d in damaged:
        _err(f"  {d}")
    _err("run `inv lock` to rewrite it (it still compares rules against the stored lock)")
    return True


def _migration(stored: dict[str, Any], version: int, doc: dict[str, Any], fresh: dict[str, Any],
               types: dict[str, lockfile.Typer | None],
               lock_path: pathlib.Path) -> tuple[list[lockfile.Change], str]:
    """The changes from an older-version stored lock to the fresh one, and the header that shows them."""
    changes = _compare(lambda: lockfile.migrate_v1(stored, doc) if version == 1
                       else lockfile.migrate_v2(stored, fresh, types), lock_path)
    header = (f"migrating {lock_path} from lock version {version} to {lockfile.LOCK_VERSION}: "
              + ("rule hashes compared" if version == 1 else
                 "rule semantics, effects, signatures and provider fields compared; resolved "
                 "types and schemas are re-pinned in the new form (review the lock diff)"))
    return changes, header


def _against_stored(stored: dict[str, Any], damaged: list[str], fresh: dict[str, Any],
                    types: dict[str, lockfile.Typer | None], path: pathlib.Path,
                    lock_path: pathlib.Path) -> tuple[list[lockfile.Change], str]:
    """The changes from a current-version stored lock (a damaged schemas table left out) to the fresh one."""
    if damaged:
        _out(f"note: {lock_path}: the schemas table is damaged and is rewritten; changes to the "
             f"schemas it held cannot be shown:")
        for d in damaged:
            _out(f"  {d}")
    old = lockfile.without_damaged_schemas(stored) if damaged else stored
    default = (scan_settings(argparse.Namespace(root=[], exclude=[]), path)
               if "scan" not in old and "scan" in fresh else None)
    changes = _compare(lambda: lockfile.changes(old, fresh, types, default), lock_path)
    return changes, f"{lock_path}: {len(changes)} change(s) against the stored lock"


def _check_result(changes: list[lockfile.Change], path: pathlib.Path, lock_path: pathlib.Path) -> int:
    """`inv lock --check`: fresh (informational changes listed), or stale with the changes listed."""
    weak = [c for c in changes if c.weakened]
    # a prose edit (statement, why, owner, class) or a signature spelled differently is recorded in
    # the lock but changes no rule's meaning: reported, never stale on its own
    if any(not c.info for c in changes):
        _err(f"stale: {lock_path} does not match {path}: {len(changes)} change(s)"
             + (f", {len(weak)} WEAKENED" if weak else ""))
        _print_changes(changes, err=True)
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
                fresh: dict[str, Any], changes: list[lockfile.Change], header: str) -> int:
    """Show the changes and write the fresh lock, unless one weakens a rule without --accept-weakening."""
    weak = [c for c in changes if c.weakened]
    if header and (changes or stored.get("lock_version") != fresh["lock_version"]):   # type: ignore[union-attr]
        _out(header)
        _print_changes(changes)
    if weak and not a.accept_weakening:
        _err(f"refused: {len(weak)} change(s) weaken a rule; the lock was not written. Review them and re-run "
             f"`inv lock --accept-weakening` to accept them.")
        return FAILED
    if stored is not None and not changes and lockfile.same(stored, fresh):
        _out(f"fresh: {lock_path} (unchanged)")
        return OK
    lock_path.write_text(lockfile.dumps(fresh))
    _out(f"wrote {lock_path}: {len(fresh['rules'])} rule(s), {len(fresh['targets'])} target(s), "
         f"{len(fresh['providers'])} provider(s), {len(fresh['contracts'])} contract(s), "
         f"{len(fresh['schemas'])} schema(s)"
         + (f"; accepted {len(weak)} weakening(s)" if weak else ""))
    return OK


def cmd_verify(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    with _import_path(path, a.path), isolated() as reg:
        _load_app(path, a.imports)
        ok, problems = status_report.verify_result(reg)
        n = sum(1 for r in reg.rules.values() if r.target is not None)
        for w in outside_roots(path, read_document(path), a.path):
            _err(w)
    if a.json:
        _out(json.dumps({"passed": ok, "problems": problems, "targeted_rules": n}, indent=2))
    elif ok:
        _out(f"verify: pass ({n} targeted rule(s) guarded, every guard names a rule)")
    else:
        _err("verify: FAIL")
        for p in problems:
            _err(f"  {p}")
    return OK if ok else FAILED


def cmd_scan(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    doc = read_document(path)
    report = scanner.scan(doc, _roots(a, path), _exclude(a, path))
    if a.format == "sarif":
        from .. import __version__
        text = json.dumps(scanner.to_sarif(report, __version__), indent=2)
    elif a.format == "json":
        text = json.dumps(report.as_dict(), indent=2)
    else:
        text = scanner.to_text(report)
    if a.output:
        pathlib.Path(a.output).write_text(text + "\n")
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
        data = status_report.build(reg, report, ablation)
    _out(json.dumps(data, indent=2) if a.json else status_report.to_text(data))
    return OK


def cmd_init(a: argparse.Namespace) -> int:
    from .. import bootstrap
    path = pathlib.Path(a.rulefile or DEFAULT_FILES[0])
    existing = [p for p in ([path] if a.rulefile else [pathlib.Path(n) for n in DEFAULT_FILES]) if p.exists()]
    if existing:
        raise Usage(f"{existing[0]} already exists; inv init never overwrites a rule file")
    project = path.resolve().parent
    roots = [pathlib.Path(r) for r in a.root] or source_dirs(path) or [project]
    for r in roots:
        if not r.exists():
            raise Usage(f"source root {r} does not exist")
    found = bootstrap.discover(project, roots, a.exclude)
    if not a.no_doors:
        found.doors = _init_doors(a, path, project)
    path.write_text(bootstrap.render(found))
    _out(f"wrote {path}")
    for line in bootstrap.summary(found):
        _out(f"  {line}")
    if a.no_lock:
        _out("next: review it, run `inv lock`, and commit both files")
        return OK
    return _init_lock(a, path)


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


def _init_lock(a: argparse.Namespace, path: pathlib.Path) -> int:
    """`inv init`'s last step: `inv lock` on the rule file it wrote, and what to do next."""
    _out("running `inv lock` to pin them ...")
    code = cmd_lock(argparse.Namespace(rulefile=str(path), check=False, update_artifacts=False, lock=None,
                                       accept_weakening=False, path=a.path, root=[], exclude=[]))
    if code == OK:
        _out("next: commit invariants.yaml and invariants.lock, and run `inv check` in CI and pre-commit "
             "(see docs/start.md)")
    else:
        _err(f"`inv lock` failed; fix what it reports (often a contract ref that does not import), then run "
             f"`inv lock`. {path} is kept.")
    return code


def cmd_check(a: argparse.Namespace) -> int:
    """Everything CI, pre-commit and an agent hook need, with one exit code."""
    path = _rule_file(a.rulefile)
    lock_path = path.with_name(lockfile.LOCK_NAME)
    doc = read_document(path)
    raw_rules = [r for r in doc.get("rules") or [] if isinstance(r, dict)]
    targeted = any((r.get("subject") or {}).get("target") for r in raw_rules)
    mediated = any(r.get("mediation") for r in raw_rules)
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

    code, text = _captured(cmd_validate, argparse.Namespace(rulefile=str(path), path=a.path))
    step("rules", code, text, f"{len(raw_rules)} rule(s), {len(doc.get('contracts') or {})} contract(s) load")
    if code != OK:
        _out("fail")
        return FAILED
    scan = {"root": a.root, "exclude": a.exclude}
    code, text = _lock_step(a, path, lock_path, scan)
    step("lock", code, text, None if text.startswith("re-pinned") else f"{lock_path.name} matches")
    if a.base:
        allow = a.allow_weakening or os.environ.get(ALLOW_WEAKENING_ENV) == "1"
        code, text = _captured(lambda ns: _base_step(path, lock_path, a.base, a.path, allow,
                                                     scan_settings(ns, path) if mediated else None),
                               argparse.Namespace(**scan))
        step(f"since {a.base}", code, text, text.splitlines()[0] if text else None)
    if targeted:
        code, text = _captured(cmd_verify, argparse.Namespace(rulefile=str(path), path=a.path, imports=a.imports,
                                                              json=False))
        step("guards", code, text)
    else:
        _out("  skip  guards: no rule guards a function yet")
    if mediated:
        code, text = _captured(cmd_scan, argparse.Namespace(rulefile=str(path), root=a.root, exclude=a.exclude,
                                                            format="text", output=None))
        step("scan", code, text, "no path skips a door or guard" if code == OK else None)
    else:
        _out("  skip  scan: no rule declares a sink or door")
    _out("fail" if failed else "pass")
    return FAILED if failed else OK


def _lock_step(a: argparse.Namespace, path: pathlib.Path, lock_path: pathlib.Path,
               scan: dict[str, Any]) -> tuple[int, str]:
    """`inv check`'s lock step: `inv lock --check`, and with --relock a re-pin that refuses a weakening."""
    code, text = _captured(cmd_lock, argparse.Namespace(rulefile=str(path), check=True, update_artifacts=False,
                                                        lock=None, accept_weakening=False, path=a.path, **scan))
    if code != OK and a.relock and lock_path.exists():
        code, text = _captured(cmd_lock, argparse.Namespace(rulefile=str(path), check=False,
                                                            update_artifacts=False, lock=None,
                                                            accept_weakening=False, path=a.path, **scan))
        if code == OK:
            text = f"re-pinned {lock_path.name}: no change weakens a rule (review the lock diff)"
            if a.fail_on_repin:
                code, text = FAILED, f"{lock_path.name} re-pinned; review and `git add {lock_path.name}`"
    return code, text


def cmd_mcp(a: argparse.Namespace) -> int:
    from .. import mcp
    return mcp.main(["--root", a.root, *(["--rules", a.rulefile] if a.rulefile else [])])
