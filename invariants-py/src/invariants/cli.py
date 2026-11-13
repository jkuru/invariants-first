"""The `inv` command line: init, check, hook, validate, lock, verify, scan, status.

Exit codes: 0 ok; 1 a check failed (invalid rule file, stale lock, verify or scan failure);
2 usage error; 3 scan found nothing failing but some calls are unresolved (not a pass);
4 scan skipped an enforce rule because it is ablated (INV_ABLATE; not a pass).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import pathlib
import re
import sys
from typing import Any, Callable, Iterator, Sequence

from . import lockfile, scan as scanner, status_report
from .errors import LoadError
from .loader import load, read_document, schema_path
from .registry import Registry, isolated

OK, FAILED, USAGE, UNRESOLVED, ABLATED = 0, 1, 2, 3, 4
DEFAULT_FILES = ("invariants.yaml", "invariants.yml", "invariants.json")


class Usage(Exception):
    """A usage error (exit 2)."""


def _out(text: str) -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _rule_file(arg: str | None) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg)
    for name in DEFAULT_FILES:
        if pathlib.Path(name).is_file():
            return pathlib.Path(name)
    raise Usage(f"no rule file given and none of {', '.join(DEFAULT_FILES)} in the current directory")


def project_root(rule_file: pathlib.Path) -> pathlib.Path:
    """The nearest directory at or above the rule file's that holds a pyproject.toml, else the rule
    file's own directory."""
    here = rule_file.resolve().parent
    return next((d for d in (here, *here.parents) if (d / "pyproject.toml").is_file()), here)


def _pyproject(root: pathlib.Path) -> dict[str, Any]:
    py = root / "pyproject.toml"
    if not py.is_file():
        return {}
    import tomllib
    try:
        return tomllib.loads(py.read_text())
    except tomllib.TOMLDecodeError as e:
        raise Usage(f"{py}: {e}") from e


def _str_list(v: Any) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def import_roots(rule_file: pathlib.Path, extra: Sequence[str] = ()) -> list[pathlib.Path]:
    """Where `inv` imports the app from, ahead of site-packages: every --path, then this checkout's
    source directories (the project's src/, its setuptools `packages.find.where` and
    [tool.invariants.scan] roots), the rule file's directory and the project root. So the lock pins
    the source beside the rule file, not an installed copy or another checkout sharing the venv."""
    proj = project_root(rule_file)
    tool = _pyproject(proj).get("tool") or {}
    where = ((tool.get("setuptools") or {}).get("packages") or {})
    where = (where.get("find") or {}).get("where") if isinstance(where, dict) else None
    scan_roots = ((tool.get("invariants") or {}).get("scan") or {}).get("roots")
    srcs = [proj / r for r in [*_str_list(where), *_str_list(scan_roots), "src"]]
    out: list[pathlib.Path] = []
    for d in [*(pathlib.Path(p).resolve() for p in extra), *(s.resolve() for s in srcs if s.is_dir()),
              rule_file.resolve().parent, proj]:
        if d not in out:
            out.append(d)
    return out


def source_dirs(rule_file: pathlib.Path) -> list[pathlib.Path]:
    """The source directories of the project whose pyproject.toml sits beside the rule file (the
    one scan_config reads): setuptools `packages.find.where` and src/, if present. These are the
    directories import_roots puts first for `inv lock`; [] when the rule file has no pyproject.toml
    beside it, so a rule file nested in a bigger project never scans that project's src/."""
    proj = rule_file.resolve().parent
    if not (proj / "pyproject.toml").is_file():
        return []
    tool = _pyproject(proj).get("tool") or {}
    where = ((tool.get("setuptools") or {}).get("packages") or {})
    where = (where.get("find") or {}).get("where") if isinstance(where, dict) else None
    out: list[pathlib.Path] = []
    for r in [*_str_list(where), "src"]:
        d = (proj / r).resolve()
        if d.is_dir() and d != proj and d not in out:
            out.append(d)
    return out


@contextlib.contextmanager
def _import_path(rule_file: pathlib.Path, extra: Sequence[str]) -> Iterator[None]:
    """Put --path entries and this checkout's source roots (import_roots) first on sys.path for the block."""
    entries = [str(d) for d in import_roots(rule_file, extra)]
    saved = list(sys.path)
    sys.path[:0] = [e for e in entries if e not in sys.path]
    try:
        yield
    finally:
        sys.path[:] = saved


def _app_modules(doc: dict[str, Any]) -> list[str]:
    """The modules a rule file pins: target, provider and python-contract modules (top-level names)."""
    refs: list[str] = list((doc.get("targets") or {}).keys())
    for raw in doc.get("rules") or []:
        if isinstance(raw, dict):
            refs.append(str((raw.get("subject") or {}).get("target") or ""))
            refs += [str(e.get("target") or "") for e in raw.get("enforcers") or [] if isinstance(e, dict)]
    refs += [str(p.get("from") or "") for p in (doc.get("providers") or {}).values() if isinstance(p, dict)]
    mods = {r[3:].split(".")[0].split(":")[0] for r in refs if r.startswith("py:")}
    mods |= {m.split(".")[0] for m in _contract_modules(doc)}
    return sorted(m for m in mods if m)


def outside_roots(rule_file: pathlib.Path, doc: dict[str, Any], extra: Sequence[str] = ()) -> list[str]:
    """Warnings for each pinned module that was imported from outside this checkout (an installed
    copy, or another checkout sharing the venv): the lock would pin that copy's types, not this one's."""
    roots = [project_root(rule_file), *(pathlib.Path(p).resolve() for p in extra)]
    out = []
    for name in _app_modules(doc):
        mod = sys.modules.get(name)
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        where = pathlib.Path(f).resolve()
        if not any(where.is_relative_to(r) for r in roots):
            out.append(f"warning: module {name} was imported from {where}, outside this checkout "
                       f"({roots[0]}); the pins describe that copy. Pass --path DIR to import this "
                       f"checkout's source.")
    return out


def _contract_modules(doc: dict[str, Any]) -> list[str]:
    mods = []
    for c in (doc.get("contracts") or {}).values():
        if isinstance(c, dict) and c.get("type") in lockfile.PY_CONTRACTS and isinstance(c.get("ref"), str):
            mods.append(c["ref"].partition(":")[0])
        elif isinstance(c, dict) and c.get("type") == "openapi" and isinstance(c.get("app"), str):
            mods.append(c["app"].partition(":")[0])
    return mods


def _load_app(rule_file: pathlib.Path, imports: Sequence[str]) -> Registry:
    """Load the rule file into the current (isolated) registry and import the app's modules."""
    reg = load(rule_file)
    for mod in [*_contract_modules(read_document(rule_file)), *imports]:
        try:
            importlib.import_module(mod)
        except ImportError as e:
            raise LoadError(f"cannot import {mod}: {e}") from e
    return reg


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


# --- subcommands --------------------------------------------------------------------------------
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


def _print_changes(changes: list[lockfile.Change], err: bool = False) -> None:
    emit = _err if err else _out
    for c in changes:
        emit(f"  {c}")


def _compare(fn: Callable[[], list[lockfile.Change]], lock_path: pathlib.Path) -> list[lockfile.Change]:
    """Run a lock comparison; a stored lock malformed below the shapes read() checks is a LockError."""
    try:
        return fn()
    except (TypeError, AttributeError, KeyError, ValueError) as e:
        raise lockfile.LockError(f"{lock_path}: malformed lock: cannot compare it with the rule file "
                                 f"({type(e).__name__}: {e}); re-create it with `inv lock`") from e


def cmd_lock(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    if a.check and a.update_artifacts:
        raise Usage("--update-artifacts writes the rule file; it cannot be combined with --check")
    if a.check and a.accept_weakening:
        raise Usage("--accept-weakening re-pins the lock; it cannot be combined with --check")
    if a.update_artifacts:
        updated = update_artifacts(path)
        for c in updated:
            _out(f"updated {c}")
        if updated and not a.accept_weakening:
            _out("note: an artifact digest is a possible weakening; the lock is re-pinned only with "
                 "--accept-weakening once the artifact and its conformance test are reviewed")
    lock_path = pathlib.Path(a.lock) if a.lock else path.with_name(lockfile.LOCK_NAME)
    doc = read_document(path)
    with _import_path(path, a.path), isolated() as reg:
        load(path)
        fresh = lockfile.build(doc, path.resolve().parent,
                               scan_settings(a, path) if lockfile.mediated(doc) else None)
        types = lockfile.clause_types(reg)
        for w in outside_roots(path, doc, a.path):
            _err(w)
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
    if stored is not None:
        version = lockfile.check_version(stored, lock_path)
        if version < lockfile.LOCK_VERSION:
            if a.check:
                _err(f"stale: {lock_path} is lock version {version}; this invariants writes version "
                     f"{lockfile.LOCK_VERSION}, which pins resolved types and stores each schema once "
                     f"without prose. Run `inv lock` once to migrate it (it refuses if a rule was "
                     f"weakened since the old pin).")
                return FAILED
            changes = _compare(lambda: lockfile.migrate_v1(stored, doc) if version == 1
                               else lockfile.migrate_v2(stored, fresh, types), lock_path)
            header = (f"migrating {lock_path} from lock version {version} to {lockfile.LOCK_VERSION}: "
                      + ("rule hashes compared" if version == 1 else
                         "rule semantics, effects, signatures and provider fields compared; resolved "
                         "types and schemas are re-pinned in the new form (review the lock diff)"))
        else:
            damaged = lockfile.schema_problems(stored)
            if damaged and a.check:
                _err(f"stale: {lock_path}: the schemas table is damaged ({len(damaged)} problem(s)):")
                for d in damaged:
                    _err(f"  {d}")
                _err("run `inv lock` to rewrite it (it still compares rules against the stored lock)")
                return FAILED
            if damaged:
                _out(f"note: {lock_path}: the schemas table is damaged and is rewritten; changes to the "
                     f"schemas it held cannot be shown:")
                for d in damaged:
                    _out(f"  {d}")
            old = lockfile.without_damaged_schemas(stored) if damaged else stored
            default = (scan_settings(argparse.Namespace(root=[], exclude=[]), path)
                       if "scan" not in old and "scan" in fresh else None)
            changes = _compare(lambda: lockfile.changes(old, fresh, types, default), lock_path)
            header = f"{lock_path}: {len(changes)} change(s) against the stored lock"
    else:
        changes, header = [], ""
    weak = [c for c in changes if c.weakened]
    if a.check:
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


def scan_config(path: pathlib.Path) -> dict[str, Any]:
    """[tool.invariants.scan] (roots, exclude) from the pyproject.toml beside the rule file, if any."""
    py = path.resolve().parent / "pyproject.toml"
    if not py.is_file():
        return {}
    import tomllib
    try:
        cfg = tomllib.loads(py.read_text()).get("tool", {}).get("invariants", {}).get("scan", {})
    except tomllib.TOMLDecodeError as e:
        raise Usage(f"{py}: {e}") from e
    for k in ("roots", "exclude"):
        if k in cfg and not (isinstance(cfg[k], list) and all(isinstance(x, str) for x in cfg[k])):
            raise Usage(f"{py}: [tool.invariants.scan] {k} must be a list of strings")
    return cfg


DEFAULT_SCAN_EXCLUDE = lockfile.DEFAULT_SCAN_EXCLUDE


def _exclude(a: argparse.Namespace, path: pathlib.Path) -> list[str]:
    cfg = scan_config(path)
    return [*cfg.get("exclude", DEFAULT_SCAN_EXCLUDE), *a.exclude]


def _roots(a: argparse.Namespace, path: pathlib.Path) -> list[pathlib.Path]:
    base = path.resolve().parent
    # By default: the project's source directories (src/, setuptools `where`), as `inv lock` imports
    # from, and the rule file's directory for everything else; a file is named by its deepest root.
    roots = ([pathlib.Path(r) for r in a.root] or [base / r for r in scan_config(path).get("roots", [])]
             or [*source_dirs(path), base])
    for r in roots:
        if not r.exists():
            raise Usage(f"source root {r} does not exist")
    return roots


def scan_settings(a: argparse.Namespace, path: pathlib.Path) -> dict[str, list[str]]:
    """The roots (relative to the rule file) and excludes `inv check` scans with, as the lock pins them."""
    base = path.resolve().parent
    roots = [pathlib.Path(os.path.relpath(r.resolve(), base)).as_posix() for r in _roots(a, path)]
    return lockfile.scan_pin(roots, _exclude(a, path))


def cmd_scan(a: argparse.Namespace) -> int:
    path = _rule_file(a.rulefile)
    doc = read_document(path)
    report = scanner.scan(doc, _roots(a, path), _exclude(a, path))
    if a.format == "sarif":
        from . import __version__
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
    from . import bootstrap
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
        from . import doors
        # the same roots and excludes `inv check` will scan, so what init freezes is what check sees
        from . import graphify
        graph = None if a.no_graphify else graphify.find(project, a.graphify)
        try:
            found.doors = doors.discover(_roots(argparse.Namespace(root=a.root), path),
                                         _exclude(argparse.Namespace(exclude=a.exclude), path), limit=a.max_doors,
                                         graph=graph, project=project)
        except graphify.GraphifyError as e:
            raise Usage(f"--graphify: {e}") from e
    path.write_text(bootstrap.render(found))
    _out(f"wrote {path}")
    for line in bootstrap.summary(found):
        _out(f"  {line}")
    if a.no_lock:
        _out("next: review it, run `inv lock`, and commit both files")
        return OK
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


ALLOW_WEAKENING_ENV = "INV_ALLOW_WEAKENING"


def _captured(fn: Callable[[argparse.Namespace], int], ns: argparse.Namespace) -> tuple[int, str]:
    """Run a subcommand with its output captured; LoadError and the like become exit 1 with the message."""
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = fn(ns)
        except Usage as e:
            print(e)
            code = USAGE
        except (LoadError, scanner.ScanConfigError) as e:
            print(e)
            code = FAILED
        except Exception as e:                       # an app module failing at import, for example
            print(f"{type(e).__name__}: {e}")
            code = FAILED
    return code, buf.getvalue().strip()


def _git(cwd: pathlib.Path, *args: str) -> tuple[int, str]:
    import subprocess
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, p.stdout if p.returncode == 0 else p.stderr.strip()


class BaseUnavailable(Exception):
    """`--base` cannot compare with the base branch. A reviewer may accept it (--allow-weakening) when
    the base lock exists but cannot be read or compared; never when the base ref itself is missing."""

    def __init__(self, why: str, acceptable: bool = False) -> None:
        super().__init__(why)
        self.acceptable = acceptable


def _symlinked(p: pathlib.Path, top: pathlib.Path) -> pathlib.Path | None:
    """The first symlink on the way from the repository root `top` down to `p` (p itself included)."""
    for q in [p, *p.parents]:
        if q.resolve() == top:
            return None
        if q.is_symlink():
            return q
    return None


def _ls_tree(top: pathlib.Path, ref: str, flags: Sequence[str] = (),
             paths: Sequence[str] = ()) -> list[tuple[str, str, str]]:
    """(mode, type, path) of each entry `git ls-tree` lists at `ref`."""
    code, text = _git(top, "ls-tree", "--full-tree", "-z", *flags, ref, "--", *paths)
    if code != 0:
        raise BaseUnavailable(f"cannot list {ref} ({text})")
    out = []
    for rec in text.split("\0"):
        head, _, name = rec.partition("\t")
        parts = head.split()
        if len(parts) == 3:
            out.append((parts[0], parts[1], name))
    return out


def _repo_root(where: pathlib.Path) -> pathlib.Path:
    code, top = _git(where, "rev-parse", "--show-toplevel")
    if code != 0:
        raise BaseUnavailable(f"not a git checkout ({top}); --base needs the repository and the base ref")
    return pathlib.Path(top.strip()).resolve()


def _base_lock(lock_path: pathlib.Path, ref: str) -> tuple[dict[str, Any] | None, str]:
    """The lock committed at git `ref` and its name there, or (None, name) when `ref` has no lock at
    the lock's path and no lock that this change moved or removed. The path is the lock's own: a
    symlinked lock, or one under a symlinked directory, is refused, never followed."""
    where = lock_path.parent.resolve()
    root = _repo_root(where)
    if _git(where, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] != 0:
        raise BaseUnavailable(f"base ref {ref} not found; fetch it first (for example `git fetch origin main`)")
    link = _symlinked(pathlib.Path(os.path.abspath(lock_path)), root)
    if link is not None:
        raise BaseUnavailable(f"{link} is a symlink; the lock must be a regular file at its own path, so "
                              f"--base compares the lock committed there")
    rel = (where / lock_path.name).relative_to(root).as_posix()
    name = f"{ref}:{rel}"
    entry = next((e for e in _ls_tree(root, ref, paths=[rel]) if e[2] == rel), None)
    if entry is None:
        gone = [n for _, kind, n in _ls_tree(root, ref, ["-r"])
                if n.rsplit("/", 1)[-1] == lockfile.LOCK_NAME and kind == "blob"
                and (_symlinked(root / n, root) is not None or not (root / n).is_file())]
        if gone:
            raise BaseUnavailable(f"{ref} has {', '.join(gone)}, which this change moved or removed: not a "
                                  f"first adoption, and the rules it pinned are not compared", acceptable=True)
        return None, name
    if entry[0] == "120000" or entry[1] != "blob":
        raise BaseUnavailable(f"{name} is not a regular file at {ref} (git mode {entry[0]})", acceptable=True)
    code, text = _git(root, "show", name)
    try:
        if code != 0:
            raise ValueError(text)
        stored = json.loads(text)
        if not isinstance(stored, dict):
            raise ValueError("not a JSON object")
        version = lockfile.check_version(stored, name)
        lockfile.validate(stored, name)
    except (ValueError, LoadError) as e:
        raise BaseUnavailable(f"{name} is not a lock this invariants reads ({e})", acceptable=True) from e
    if version != lockfile.LOCK_VERSION:
        raise BaseUnavailable(f"{name} is lock version {version}; this invariants compares version "
                              f"{lockfile.LOCK_VERSION} only, so weakenings since {ref} cannot be shown "
                              f"(migrate the lock on {ref} first)", acceptable=True)
    return stored, name


def base_changes(path: pathlib.Path, lock_path: pathlib.Path, ref: str, extra: Sequence[str],
                 scan: dict[str, Any] | None = None) -> tuple[list[lockfile.Change] | None, str]:
    """The changes between the lock committed at git `ref` and the rule file as it is now, or (None, why)
    when `ref` has no lock (a first adoption, reviewed as a whole). BaseUnavailable when there is no
    git checkout, no such ref, a symlinked lock, a lock the change moved or removed, or a lock at
    `ref` that cannot be read or compared."""
    stored, name = _base_lock(lock_path, ref)
    if stored is None:
        return None, f"no {name}: first adoption of invariants, nothing to compare (review the whole lock)"
    doc = read_document(path)
    with _import_path(path, extra), isolated() as reg:
        load(path)
        fresh = lockfile.build(doc, path.resolve().parent, scan)
        types = lockfile.clause_types(reg)
    default = _base_scan_default(path, ref) if "scan" not in stored and "scan" in fresh else None
    return _compare(lambda: lockfile.changes(stored, fresh, types, default), lock_path), ""


def _base_scan_default(path: pathlib.Path, ref: str) -> dict[str, list[str]]:
    """The roots and excludes the scan used at git `ref` with no --root or --exclude: [tool.invariants.scan]
    of the pyproject.toml beside the rule file there, else its src/ (and setuptools `where`) and the
    rule file's directory, as scan_settings gives them in a checkout of `ref`."""
    import posixpath
    import tomllib
    where = path.parent.resolve()
    root = _repo_root(where)
    rel = where.relative_to(root).as_posix()
    pre = "" if rel in ("", ".") else rel + "/"
    tree = {n: kind for _, kind, n in _ls_tree(root, ref, ["-r", "-t"])}
    has_py = tree.get(pre + "pyproject.toml") == "blob"
    tool: dict[str, Any] = {}
    if has_py:
        code, text = _git(root, "show", f"{ref}:{pre}pyproject.toml")
        try:
            tool = (tomllib.loads(text).get("tool") or {}) if code == 0 else {}
        except tomllib.TOMLDecodeError:
            tool = {}
    cfg = (tool.get("invariants") or {}).get("scan") or {}
    roots = [posixpath.normpath(r) for r in _str_list(cfg.get("roots"))]
    if not roots:
        finder = (tool.get("setuptools") or {}).get("packages") or {}
        found = (finder.get("find") or {}).get("where") if isinstance(finder, dict) else None
        dirs = [posixpath.normpath(d) for d in [*_str_list(found), "src"]] if has_py else []
        roots = [*(d for d in dirs if d != "." and tree.get(posixpath.normpath(pre + d)) == "tree"), "."]
    exclude = _str_list(cfg["exclude"]) if isinstance(cfg.get("exclude"), list) else list(DEFAULT_SCAN_EXCLUDE)
    return lockfile.scan_pin(roots, exclude)


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


def _base_step(path: pathlib.Path, lock_path: pathlib.Path, ref: str, extra: Sequence[str], allow: bool,
               scan: dict[str, Any] | None = None) -> int:
    try:
        changes, why = base_changes(path, lock_path, ref, extra, scan)
    except BaseUnavailable as e:
        if allow and e.acceptable:
            _out(f"{e} (allowed by a reviewer)")
            return OK
        _out(f"cannot compare with {ref}: {e}")
        if e.acceptable:
            _out(f"a person may accept this: --allow-weakening or {ALLOW_WEAKENING_ENV}=1, set by a reviewer")
        return FAILED
    if changes is None:
        _out(why)
        return OK
    weak = [c for c in changes if c.weakened]
    if not weak:
        _out(f"{len(changes)} change(s) since {ref}, none weakens a rule")
        return OK
    _out(f"{len(weak)} change(s) since {ref} weaken a rule" + (" (allowed by a reviewer)" if allow else ":"))
    _print_changes(weak)
    if allow:
        return OK
    _out(f"a person must accept these: re-run with --allow-weakening or {ALLOW_WEAKENING_ENV}=1, set by a reviewer "
         f"(for example from a pull-request label), never by the change's author or agent")
    return FAILED


WEAKENING_FLAGS = ("--accept-weakening", "--allow-weakening")
WEAKENING_SWITCHES = (*WEAKENING_FLAGS, ALLOW_WEAKENING_ENV)


PROTECTED = (lockfile.LOCK_NAME, "pyproject.toml")
# what a shell command may do with a protected file and still pass the hook: read it
READ_COMMANDS = frozenset({"cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "jq", "diff",
                           "ls", "stat", "cmp", "cd", "pushd", "popd", "pwd", "echo", "printf", "true", "false",
                           "test", "[", "which", "sha256sum", "shasum", "md5", "md5sum", "file"})
READ_GIT = frozenset({"diff", "show", "log", "status", "blame", "add"})   # add stages the file as it is
READ_INV = frozenset({"check", "status", "scan", "validate", "verify"})
GREP_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg"})              # their -o is --only-matching
# commands that write the files they name: with a computed name ($f) under the lock's directory, blocked
WRITE_COMMANDS = frozenset({"cp", "mv", "tee", "dd", "rm", "ln", "install", "rsync", "unlink", "truncate", "shred",
                            "touch", "sort", "patch", "sed", "gsed", "perl", "ruby", "awk", "gawk", "ditto", "scp",
                            "curl", "wget", "tar", "unzip", "chmod", "chown"})
# a command that runs other code the hook cannot read: blocked when it mentions the lock or pyproject
OPAQUE = re.compile(r"\$\(|`|\$\{|<\(|>\(|\beval\b|\bpython[0-9.]*\s+-[c\s]|\bpython[0-9.]*\s+-$"
                    r"|\b(?:ba|z|da|k)?sh\s+-c\b|\bxargs\b|\b(?:perl|ruby|node)\s+-e\b|\bGIT_[A-Z_]*=|\bgit\s+config\b")
SUSPECT = re.compile(r"lock|pyproject|toml|invariants", re.IGNORECASE)
_GLOB = re.compile(r"[*?\[{]")


_BRACE_LIMIT = 256


def _brace_alternatives(body: str) -> list[str] | None:
    """What the inside of one {...} expands to: its top-level comma alternatives or an a..b range;
    None when the braces stay literal."""
    parts: list[str] = []
    depth, cur = 0, ""
    for c in body:
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        depth += (c == "{") - (c == "}")
        cur += c
    if parts:
        return [*parts, cur]
    m = re.fullmatch(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?", body)
    if m:
        a, b, step = int(m[1]), int(m[2]), abs(int(m[3] or 1)) or 1
        if abs(b - a) // step > _BRACE_LIMIT:
            return None
        return [str(i) for i in range(a, b + (1 if b >= a else -1), step if b >= a else -step)]
    m = re.fullmatch(r"(.)\.\.(.)", body)
    if m:
        lo, hi = sorted((ord(m[1]), ord(m[2])))
        return [chr(i) for i in range(lo, hi + 1)] if hi - lo <= _BRACE_LIMIT else None
    return None


def _expand_braces(word: str) -> list[str] | None:
    """The words a shell brace expansion makes of one word (invariants{,}.lock -> two, x{1..3} ->
    three); None when that is more than the hook will list."""
    todo, done = [word], []
    while todo:
        w = todo.pop()
        expanded = False
        start = 0
        while not expanded and (i := w.find("{", start)) >= 0:
            depth, j = 0, i
            for j in range(i, len(w)):
                depth += (w[j] == "{") - (w[j] == "}")
                if depth == 0:
                    break
            if depth:
                break                               # no closing brace: literal from here on
            alts = _brace_alternatives(w[i + 1:j])
            if alts is None:
                start = i + 1                       # a literal {...}: look for the next one
                continue
            todo.extend(w[:i] + a + w[j + 1:] for a in alts)
            expanded = True
        if not expanded:
            done.append(w)
        if len(todo) + len(done) > _BRACE_LIMIT:
            return None
    return done


def _ansi_c(command: str) -> str:
    """The command with each $'...' (bash's ANSI-C quoting, $'\\x69nvariants.lock') decoded into a
    plain single-quoted word."""
    import codecs
    import shlex

    def decode(m: re.Match[str]) -> str:
        body = re.sub(r"\\c(.)", lambda c: chr(ord(c[1].upper()) ^ 64), m[1])
        body = re.sub(r"\\([0-7]{1,3})", lambda o: f"\\x{int(o[1], 8) & 0xFF:02x}", body)
        body = re.sub(r"\\([eE])", r"\\x1b", body)
        body = re.sub(r"\\(['\"?])", r"\1", body)
        try:
            text = codecs.decode(body.encode("latin-1", "backslashreplace"), "unicode_escape")
        except (UnicodeDecodeError, ValueError):
            text = body
        return shlex.quote(text)
    return re.sub(r"\$'((?:[^'\\]|\\.)*)'", decode, command)


def _mentions(command: str, name: str) -> bool:
    """The text names the file, or a glob that could match it (invariants.l*, pyproject.*), in any
    letter case (a case-insensitive filesystem opens INVARIANTS.LOCK as the lock) and after brace
    expansion (invariants{,}.lock)."""
    text, stem = command.casefold(), name.partition(".")[0]
    if name in text or re.search(re.escape(stem) + r"\.\w*[*?\[{]", text) is not None:
        return True
    for chunk in re.findall(r"\S*\{\S*", text):
        words = _expand_braces(chunk)
        if words is None:
            if stem in chunk:
                return True                         # too many to list: any word with the stem
        elif words != [chunk] and any(_mentions(w, name) for w in words):
            return True
    return False


def _names(word: str, name: str) -> bool:
    """A shell word (unquoted) that may name the file: the name, a glob matching it, or text holding it,
    in any letter case and after brace expansion."""
    import fnmatch
    folded = word.casefold()
    if _mentions(folded, name):
        return True
    for w in _expand_braces(folded) or [folded]:
        base = w.rstrip("/").rsplit("/", 1)[-1]
        if bool(_GLOB.search(base)) and fnmatch.fnmatchcase(name, base):
            return True
    return False


def _tokens(command: str) -> list[str] | None:
    """The command as shell words and operators (quotes removed), or None when it does not parse."""
    import shlex
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Simple commands: their words and their (redirect operator, target) pairs."""
    out: list[tuple[list[str], list[tuple[str, str]]]] = []
    words: list[str] = []
    redirects: list[tuple[str, str]] = []
    it = iter(tokens)
    for t in it:
        if t and all(c in ";|&()" for c in t):
            out.append((words, redirects))
            words, redirects = [], []
        elif t and all(c in "<>&|" for c in t) and ("<" in t or ">" in t):
            redirects.append((t, next(it, "")))
        else:
            words.append(t)
    out.append((words, redirects))
    return [s for s in out if s[0] or s[1]]


def _writes_redirect(redirects: list[tuple[str, str]]) -> bool:
    for op, target in redirects:
        if ">" not in op or target == "/dev/null" or (op.endswith("&") and target.isdigit()):
            continue
        return True
    return False


def _reads_only(words: list[str], redirects: list[tuple[str, str]]) -> bool:
    """A simple command that only reads files: no redirect into a file, no --output, a reading command."""
    if _writes_redirect(redirects):
        return False
    assigned: list[str] = []
    while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])
                     or (words[0] == "env" and len(words) > 1)
                     or (assigned and words[0].startswith("-") and "env" in assigned)):
        assigned.append("env" if words[0] == "env" else words[0].partition("=")[0])
        words = words[1:]                           # VAR=value (or env VAR=value) before the command
    if not words:
        return True                                 # only assignments: they write no file
    cmd, rest = pathlib.PurePath(words[0]).name, words[1:]
    if any(w == "--output" or w.startswith("--output=") or (w == "-o" and cmd not in GREP_COMMANDS)
           for w in rest):
        return False                                # writes the file it names
    if cmd in READ_COMMANDS:
        return not (cmd == "rg" and any(w.partition("=")[0] in ("--pre", "--pre-glob") for w in rest))
    if cmd == "git":
        if assigned:
            return False                            # GIT_EXTERNAL_DIFF=..., GIT_PAGER=... run code
        while rest[:1] == ["-C"] or rest[:1] == ["--no-pager"]:
            rest = rest[2:] if rest[0] == "-C" else rest[1:]
        return bool(rest) and rest[0] in READ_GIT   # -c, -p and other options are not read-only
    if cmd == "inv" and rest:
        return rest[0] in READ_INV or (rest[0] == "lock" and "--check" in rest)
    return False


def _computed_target(words: list[str], redirects: list[tuple[str, str]]) -> bool:
    """A simple command that writes a file whose name the shell computes ($f, `...`): a redirect, or
    a write-capable command with $ or a backtick in a path it writes."""
    if any(">" in op and any(c in t.rstrip("/").rsplit("/", 1)[-1] for c in "$`") for op, t in redirects):
        return True
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]
    while words and words[0] in ("env", "sudo", "command", "exec", "nice", "nohup", "time", "builtin"):
        words = words[1:]
        while words and (words[0].startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])):
            words = words[1:]
    if not words:
        return False
    cmd, args = pathlib.PurePath(words[0]).name, words[1:]
    if cmd not in WRITE_COMMANDS:
        return False
    if cmd in ("sed", "gsed", "perl", "ruby") and not any(a.startswith("-i") or a == "--in-place" for a in args):
        return False
    paths = [a for a in args if not a.startswith("-") or "=" in a]
    if cmd in ("sed", "gsed", "perl", "ruby", "awk", "gawk") and paths and not any(
            a in ("-e", "-f", "--expression", "--file") for a in args):
        paths = paths[1:]                           # the script, not a path
    bases = [a.rstrip("/").rsplit("/", 1)[-1] for a in paths]    # "$TMPDIR/out.txt" names out.txt
    return any("$" in b or "`" in b for b in bases)


def _lock_dir(cwd: str | None) -> bool:
    """The directory, or one above it, holds invariants.lock."""
    if not cwd:
        return False
    here = pathlib.Path(cwd)
    return any((d / lockfile.LOCK_NAME).exists() for d in (here, *here.parents))


def _shell_writes(command: str, cwd: str | None = None) -> str | None:
    """The protected file a shell command may write: one it names (quoted, escaped, as a glob, in any
    letter case, behind a brace expansion or $'...') in a command that is not clearly read-only. A
    command the hook cannot read (it does not parse, runs code from a string, or expands a name) is
    blocked when it mentions the lock or pyproject, and, under a directory that holds the lock, so is
    a write to a file whose name the shell computes ($f, a backtick)."""
    command = _ansi_c(command)
    lowered = command.lower()
    named = [n for n, word in zip(PROTECTED, ("lock", "pyproject")) if word in lowered]
    tokens = _tokens(command)
    if tokens is None or OPAQUE.search(command):
        hit = next((n for n in PROTECTED if _mentions(command, n)), None) or (named[0] if named else None)
        if hit:
            return hit
        if tokens is None:
            return None
    segments = _segments(tokens or [])
    for name in PROTECTED:
        for words, redirects in segments:
            texts = [*words, *(t for _, t in redirects)]
            if any(_names(w, name) for w in texts) and not _reads_only(words, redirects):
                return name
    if SUSPECT.search(command):
        for words, redirects in segments:
            texts = [*words, *(t for _, t in redirects)]
            if any("$" in w for w in texts) and not _reads_only(words, redirects):
                return named[0] if named else lockfile.LOCK_NAME
    if _lock_dir(cwd) and any(_computed_target(words, redirects) for words, redirects in segments):
        return lockfile.LOCK_NAME
    return None


def _weakening_switch(command: str) -> str | None:
    """A switch that accepts a weakening: the flags, the environment variable, or an abbreviation of a
    flag (5 characters or more; older argparse took them) the command passes."""
    hit = next((s for s in WEAKENING_SWITCHES if s in command), None)
    if hit:
        return hit
    command = _ansi_c(command)
    runs_inv = re.search(r"\binv\b|invariants", command) is not None
    words = _tokens(command) or command.split()
    for w in words:                                 # after unquoting: env INV_ALLOW_WEAK''ENING=1, export, X=1 cmd
        if any(s in w for s in WEAKENING_SWITCHES):
            return next(s for s in WEAKENING_SWITCHES if s in w)
    for w in words:
        flag = w.partition("=")[0]
        if len(flag) >= 5 and any(f.startswith(flag) for f in WEAKENING_FLAGS) and (flag != "--all" or runs_inv):
            return w
    return None


def _invariants_table(text: str) -> Any:
    """[tool.invariants] of a pyproject.toml text; the text itself when it does not parse."""
    import tomllib
    try:
        return (tomllib.loads(text).get("tool") or {}).get("invariants")
    except tomllib.TOMLDecodeError:
        return text if "invariants" in text else None


def _edited_text(tool_input: dict[str, Any], before: str) -> str | None:
    """The file's text after an Edit, MultiEdit or Write tool call, or None when it cannot be told."""
    if isinstance(tool_input.get("content"), str):
        return str(tool_input["content"])
    given = tool_input.get("edits")
    edits: list[Any] = given if isinstance(given, list) else [tool_input]
    text = before
    for e in edits:
        old, new = (e.get("old_string"), e.get("new_string")) if isinstance(e, dict) else (None, None)
        if not (isinstance(old, str) and isinstance(new, str)):
            return None
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


def _touches_invariants_table(path: pathlib.Path, tool_input: dict[str, Any]) -> bool:
    """An edit of pyproject.toml that changes its [tool.invariants] tables (the scan roots and excludes)."""
    try:
        before = path.read_text()
    except OSError:
        before = ""
    after = _edited_text(tool_input, before)
    if after is None:
        return "invariants" in json.dumps(tool_input)
    return not lockfile.same(_invariants_table(before), _invariants_table(after))


def _pretool(event: dict[str, Any]) -> int:
    given = event.get("tool_input")
    tool_input: dict[str, Any] = given if isinstance(given, dict) else {}
    cwd = str(event.get("cwd") or os.getcwd())
    command = str(tool_input.get("command") or "")
    files = [pathlib.Path(os.path.realpath(os.path.join(cwd, str(tool_input[k]))))
             for k in ("file_path", "path", "notebook_path") if tool_input.get(k)]
    written = _shell_writes(command, cwd)
    lock, pyproject = lockfile.LOCK_NAME.casefold(), "pyproject.toml"   # a case-insensitive filesystem
    if any(f.name.casefold() == lock for f in files) or written == lockfile.LOCK_NAME:
        _err(f"invariants: blocked a direct edit of {lockfile.LOCK_NAME}. The lock is written only by "
             f"`inv lock` or `inv check --relock`, which refuse a weakening; edit invariants.yaml or the "
             f"code, then run one of them.")
        return 2
    if written or any(f.name.casefold() == pyproject and _touches_invariants_table(f, tool_input) for f in files):
        _err("invariants: blocked an edit of the [tool.invariants] settings in pyproject.toml (or a shell "
             "command that writes pyproject.toml). The scan roots and excludes decide which code is checked "
             "for back doors; tell the user what you want to change and let them make the edit.")
        return 2
    hit = _weakening_switch(command)
    if hit:
        _err(f"invariants: blocked `{hit}`. Accepting a weakened rule is a person's decision. Tell the user "
             f"which rule your change weakens and why (run `inv check` to list it), and let them accept it.")
        return 2
    return OK


def cmd_hook(a: argparse.Namespace) -> int:
    """An AI coding agent's hook (Claude Code's JSON on stdin; exit 2 blocks and tells the agent why).

    pretool: refuse a shell command that accepts a weakening (--accept-weakening, --allow-weakening,
             INV_ALLOW_WEAKENING, or a flag abbreviated to 5+ characters), so only a person can; any
             direct edit of invariants.lock (an edit or write tool on it, symlinks resolved, or a
             shell command naming it, quoted or as a glob, that is not clearly read-only, and one the
             hook cannot read that mentions it): only `inv lock` and `inv check --relock` write the
             lock, and they refuse a weakening; and an edit of pyproject.toml's [tool.invariants]
             tables, or a shell command that writes pyproject.toml.
    stop:    refuse to finish while `inv check` fails, once per stop (stop_hook_active ends the loop).

    The hooks are an early warning against an agent's shortcuts, not the gate: a determined command
    can always get past a text check. `inv check --base` in CI is the gate."""
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    event = event if isinstance(event, dict) else {}
    if a.event == "pretool":
        return _pretool(event)
    if event.get("stop_hook_active"):
        return OK                                   # already sent back once: do not loop
    base = pathlib.Path(str(event.get("cwd") or "."))
    rule = next((base / n for n in DEFAULT_FILES if (base / n).is_file()), None)
    if a.rulefile:
        rule = pathlib.Path(a.rulefile)
    if rule is None:
        return OK
    code, text = _captured(cmd_check, argparse.Namespace(rulefile=str(rule), path=a.path, relock=True,
                                                         fail_on_repin=False, base=None, root=[], exclude=[],
                                                         allow_weakening=False, imports=[]))
    if code == OK:
        return OK
    _err("invariants: `inv check` fails, so this change is not done:")
    _err(text)
    _err("Fix the code so it keeps the rule. If the rule itself must change, say so to the user; do not "
         "accept a weakening yourself.")
    return 2


# --- parser -------------------------------------------------------------------------------------
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inv", description="Invariant-first rule files: check, lock, verify, scan.",
                                allow_abbrev=False,
                                epilog="exit codes: 0 ok, 1 check failed, 2 usage error, 3 scan unresolved, "
                                "4 scan skipped an ablated enforce rule")
    sub = p.add_subparsers(dest="command", metavar="command")

    def add(name: str, fn: Callable[[argparse.Namespace], int], help: str, imports: bool = True) -> Any:
        sp = sub.add_parser(name, help=help, description=help, allow_abbrev=False)   # no --accept-w
        sp.add_argument("rulefile", nargs="?", help="rule file (default: ./invariants.yaml|yml|json)")
        if imports:
            sp.add_argument("--path", action="append", default=[], metavar="DIR",
                            help="import the app from DIR first (repeatable; this checkout's src/, "
                                 "packages.find where and scan roots, the rule file's directory and the "
                                 "project root are always added ahead of site-packages)")
        sp.set_defaults(fn=fn)
        return sp

    add("validate", cmd_validate, "schema and every load-time check; exit 1 with readable errors")
    lk = add("lock", cmd_lock, "write invariants.lock; with --check, fail when it is stale")
    lk.add_argument("--check", action="store_true", help="compare instead of writing; exit 1 when stale")
    lk.add_argument("--update-artifacts", action="store_true",
                    help="first rewrite stale artifact sha256 digests in the rule file (review the diff)")
    lk.add_argument("--lock", metavar="FILE", help="lockfile path (default: invariants.lock beside the rule file)")
    lk.add_argument("--accept-weakening", action="store_true",
                    help="re-pin even when a change may weaken a rule (lower status or mode, removed enforcer or "
                         "clause, fail_open, a looser threshold, a changed bind, view, provider, "
                         "model_policy or artifact); without it `inv lock` refuses")
    lk.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="pin this scan root (as `inv check --root`; prefer [tool.invariants.scan] roots)")
    lk.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="pin this scan exclude (as `inv check --exclude`; prefer [tool.invariants.scan] exclude)")
    vf = add("verify", cmd_verify, "import the app and check every target is guarded and every guard named")
    vf.add_argument("--import", dest="imports", action="append", default=[], metavar="MODULE",
                    help="also import MODULE (contracts of type python are imported already)")
    vf.add_argument("--json", action="store_true")
    sc = add("scan", cmd_scan, "static call-graph check: no path to a protected sink avoids its door", imports=False)
    sc.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="source root to scan (repeatable; default: the rule file's directory)")
    sc.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip files matching GLOB, relative to each root (repeatable; also "
                         "[tool.invariants.scan] exclude in pyproject.toml)")
    sc.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    sc.add_argument("--output", metavar="FILE", help="write the report to FILE")
    it = add("init", cmd_init, "draft invariants.yaml from the pydantic constraints, doors and function "
             "preconditions the code already has, then lock it")
    it.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="source root to read (repeatable; default: src/ or the current directory)")
    it.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip files matching GLOB (tests, docs, examples and scripts are skipped already)")
    it.add_argument("--no-lock", action="store_true", help="write the rule file only; do not run `inv lock`")
    it.add_argument("--no-doors", action="store_true",
                    help="do not look for doors (the service functions every call to a repository's writes "
                         "goes through)")
    it.add_argument("--graphify", "--graph", metavar="GRAPH",
                    help="read a Graphify graph.json for door hints (default: graphify-out/graph.json when it "
                         "exists); every suggested door is still checked by the scan")
    it.add_argument("--no-graphify", action="store_true", help="do not read a Graphify graph")
    it.add_argument("--max-doors", type=int, default=5, metavar="N",
                    help="write at most N door rules, the most important first (default 5); the rest are listed")
    ck = add("check", cmd_check, "validate, lock --check, verify and scan with one exit code: for CI, "
             "pre-commit and agent hooks")
    ck.add_argument("--relock", action="store_true",
                    help="re-pin a stale lock when no change weakens a rule (for pre-commit); a weakening "
                         "still fails")
    ck.add_argument("--fail-on-repin", action="store_true",
                    help="with --relock: exit 1 after re-pinning, so the new lock is reviewed and staged (the "
                         "pre-commit hook)")
    ck.add_argument("--base", metavar="REF",
                    help="also fail when the rules weaken since the lock committed at git REF (e.g. origin/main), "
                         "even if the change re-pinned the lock with --accept-weakening")
    ck.add_argument("--allow-weakening", action="store_true",
                    help=f"with --base: report weakenings but pass (also {ALLOW_WEAKENING_ENV}=1); for a "
                         f"reviewer to set, not the change's author")
    ck.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="scan this source root (repeatable; as `inv scan --root`, also [tool.invariants.scan] roots)")
    ck.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="scan: skip files matching GLOB (as `inv scan --exclude`)")
    ck.add_argument("--import", dest="imports", action="append", default=[], metavar="MODULE")
    hk = add("hook", cmd_hook, "a hook for AI coding agents (reads Claude Code's hook JSON on stdin; exit 2 "
             "blocks): pretool refuses commands that accept a weakening, stop refuses to finish while "
             "`inv check` fails")
    hk.add_argument("event", choices=("pretool", "stop"))
    st = add("status", cmd_status, "standing per rule from verify and scan")
    st.add_argument("--root", action="append", default=[], metavar="DIR")
    st.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    st.add_argument("--import", dest="imports", action="append", default=[], metavar="MODULE")
    st.add_argument("--json", action="store_true")
    st.add_argument("--ablation", metavar="JSON",
                    help="the pytest --inv-ablate --inv-report-json file; without it no rule is reported held")
    mc = add("mcp", cmd_mcp, "read-only MCP server over stdio for coding agents (docs/agents.md)", imports=False)
    mc.add_argument("--root", default=".", metavar="DIR", help="project root (default: the current directory)")
    return p


def cmd_mcp(a: argparse.Namespace) -> int:
    from . import mcp
    return mcp.main(["--root", a.root, *(["--rules", a.rulefile] if a.rulefile else [])])


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `inv`."""
    p = parser()
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if not getattr(a, "fn", None):
        p.print_usage(sys.stderr)
        _err("inv: choose a command: init, check, validate, lock, verify, scan, status")
        return USAGE
    try:
        return a.fn(a)
    except Usage as e:
        _err(f"inv {a.command}: {e}")
        return USAGE
    except (LoadError, scanner.ScanConfigError) as e:
        _err(f"inv {a.command}: {e}")
        return FAILED
    except Exception as e:          # an app module failing at import, for example
        _err(f"inv {a.command}: {type(e).__name__}: {e}")
        return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
