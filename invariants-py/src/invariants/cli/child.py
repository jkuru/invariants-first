"""The child process worker.py runs: it loads the rule file, takes the pins and runs the guard
check, importing the app, and writes what it computed as JSON for the parent, which never imports
the app. Everything here runs in the child only."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
from typing import Any, Iterator, Sequence

import invariants_scan as scanner

from .. import lock
from ..errors import LoadError
from ..loader import _type_env, load, read_document
from ..registry import isolated
from .commands import cmd_verify, contract_problem, contract_problems
from .common import Usage, _captured, _describe
from ..project import _import_path, outside_roots


@contextlib.contextmanager
def _capture() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _exc(e: BaseException) -> dict[str, str]:
    if isinstance(e, Usage):
        return {"cls": "usage", "text": str(e)}
    if isinstance(e, (LoadError, scanner.ScanConfigError)):
        return {"cls": "load", "text": _describe(e)}
    return {"cls": "other", "text": _describe(e, typed=True)}


def _identities() -> list[Any]:
    """What the guard check runs, by identity: verify's command, the registry's check and its error,
    and the decorators it checks for (invariants.guard, model_call, edge)."""
    import sys
    import invariants
    from .. import errors, registry
    from ..guard import decorators
    from ..registry import coverage
    here = sys.modules[__name__]
    return [getattr(here, "cmd_verify", None),                  # [0]: what _verify runs getattr(sys.modules[cmd_verify.__module__], "cmd_verify", None),
            getattr(registry.Registry, "verify", None), getattr(registry.Registry, "_verify", None),
            getattr(registry.Registry, "_guard_problems", None), getattr(coverage, "_target_problems", None),
            getattr(errors, "CoverageError", None), getattr(registry, "CoverageError", None),
            getattr(sys.modules[cmd_verify.__module__], "CoverageError", None), _captured,
            *(getattr(m, n, None) for m in (invariants, decorators) for n in ("guard", "model_call", "edge")),
            getattr(decorators, "_guard", None), getattr(coverage, "WRAPPED", None)]


def serve(req: dict[str, Any]) -> None:
    """The child: compute what `req["want"]` lists and write it as JSON to `req["out"]`."""
    _SEEN[:] = _identities()                     # before anything imports the app
    path, extra, want = pathlib.Path(req["rulefile"]), req["extra"], req["want"]
    res: dict[str, Any] = {}
    loaded = True
    if want.get("load"):
        res["load"] = _load(path, extra)
        loaded = "notes" in res["load"]
    if want.get("build") is not None:
        res["build"] = _build(path, extra, want["build"], want.get("olds") or []) if loaded else {
            "raised": {"cls": "load", "text": "the rule file does not load"}}
    if want.get("verify") is not None:
        res["verify"] = _verify(path, extra, want["verify"]) if loaded else [1, ""]
    if want.get("contracts") is not None:
        res["contracts"] = _contracts(path, extra, want["contracts"])
    pathlib.Path(req["out"]).write_text(json.dumps(res), encoding="utf-8")


def _load(path: pathlib.Path, extra: Sequence[str]) -> dict[str, Any]:
    """`inv validate`'s load step."""
    out: dict[str, Any] = {}
    with _capture() as buf:
        with _import_path(path, extra), isolated():
            try:
                out["notes"] = list(load(path).notes)
            except LoadError as e:
                out["problem"] = f"load: {_describe(e)}"
            except Exception as e:
                out["raised"] = _exc(e)
    out["output"] = buf.getvalue()
    return out


def _build(path: pathlib.Path, extra: Sequence[str], spec: dict[str, Any], olds: list[Any]) -> dict[str, Any]:
    """`inv lock`'s pins: the fresh lock, the clause types each comparison with an old lock reads,
    and the warnings for modules imported from outside the checkout."""
    out: dict[str, Any] = {}
    with _capture() as buf:
        try:
            doc = read_document(path)
            with _import_path(path, extra), isolated() as reg:
                load(path)
                if spec.get("scan_error"):
                    out["scan_error"] = True                 # the parent raises its own Usage
                    raise _Stop
                try:
                    fresh = lock.build(doc, path.resolve().parent, spec.get("scan"))
                except (LoadError, Usage, scanner.ScanConfigError):
                    raise
                except Exception as e:
                    unpinnable = contract_problems(doc, path.resolve().parent)
                    if not unpinnable:
                        raise
                    raise LoadError("; ".join(unpinnable) + f" (comment out or delete it in {path.name}, or fix "
                                    f"the model, then run `inv lock` again)") from e
                types = lock.clause_types(reg, _type_env)
                out["warnings"] = outside_roots(path, doc, extra)
            out["fresh"] = json.loads(json.dumps(fresh))
            out["types"] = _record(types, olds, out["fresh"])
        except _Stop:
            pass
        except Exception as e:
            out["raised"] = _exc(e)
    out["output"] = buf.getvalue()
    return out


class _Stop(Exception):
    pass


def _record(types: dict[str, Any], olds: list[Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """Each rule's typer, as the table of the calls a comparison with each old lock makes."""
    tables: dict[str, dict[str, Any] | None] = {rid: (None if t is None else {}) for rid, t in types.items()}

    def recorder(rid: str, t: Any) -> Any:
        def typer(raw: Any) -> Any:
            got = t(raw)
            tab = tables[rid]
            if tab is not None:
                tab[repr(raw)] = None if got is None else sorted(got)
            return got
        return typer
    wrapped = {rid: (None if t is None else recorder(rid, t)) for rid, t in types.items()}
    for rules in olds:
        try:
            lock.changes({"rules": rules}, {"rules": fresh.get("rules") or {}}, wrapped)
        except Exception:
            pass                                         # the parent reports a malformed old lock
    return {rid: (None if tab is None else sorted(tab.items())) for rid, tab in tables.items()}


def _verify(path: pathlib.Path, extra: Sequence[str], imports: list[str]) -> list[Any]:
    """`inv check`'s guards step: `inv verify`, its output captured. Refused when the app has replaced
    what runs the check (defence in depth: the parent decides the guards from the source too)."""
    if not _unchanged():
        return [1, "verify: FAIL\n  the imported code replaced the guard check (invariants' verify) in the "
                   "process that runs it"]
    code, text = _captured(_SEEN[0], argparse.Namespace(rulefile=str(path), path=list(extra), imports=imports,
                                                          json=False))
    if not _unchanged():
        return [1, "verify: FAIL\n  the guard check was replaced while it ran"]
    return [code, text]


_SEEN: list[Any] = []


def _unchanged() -> bool:
    now = _identities()
    return len(now) == len(_SEEN) and all(a is b for a, b in zip(now, _SEEN))


def _contracts(path: pathlib.Path, extra: Sequence[str], records: list[Any]) -> list[str | None]:
    """`inv init`: why each drafted contract cannot be pinned (None when it can)."""
    with _capture():
        with _import_path(path, extra), isolated():
            return [contract_problem(n, r, path.resolve().parent) for n, r in records]
