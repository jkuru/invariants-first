"""`inv init`: find the doors a codebase already has, and the back doors around them.

The boundary is the repository layer, in Python, whatever is behind it (Postgres through SQLAlchemy,
Django, MongoDB, DynamoDB, or an HTTP API such as Stripe):

- a *sink* is a function that writes: a write method of a Repo/Store class (or a write function in a
  repo or store module), or any function whose body writes through an ORM session, a Django manager,
  a Mongo collection, a DynamoDB table, or a client's `create` call;
- a *door* is the one function most callers go through to reach a sink: the service method that
  checks the rules before the write;
- a *back door* is any other way to the same write: a caller that reaches a sink without passing a
  door.

Discovery reads the source with the scan's own index (Python's `ast`; nothing is imported) and is
deterministic. It groups the sinks by repository (a class or a module),
picks each sink's door, and checks the proposed rule with the real scan, so the back doors it reports
are exactly what `inv check` would fail on. Known back doors are frozen in `allow:` (the way ArchUnit
freezes existing violations): today's code passes, a new back door fails, and removing an `allow`
entry once it is fixed is a tightening. Rules are ranked by names that suggest money, quotas or
approvals; at most `limit` are written and the rest are listed.
"""
from __future__ import annotations

import ast
import collections
import dataclasses
import json
import pathlib
import re
from typing import Any, Iterable

from . import scan as scanner

# Names that suggest an invariant someone cares about: money, quotas, approvals, inventory.
IMPORTANT = ("balance", "ledger", "payment", "pay", "refund", "invoice", "credit", "debit", "charge", "transfer",
             "payout", "account", "wallet", "fund", "quota", "usage", "limit", "budget", "allowance", "approv",
             "permission", "role", "grant", "order", "stock", "inventory", "subscription", "billing", "price",
             "lease", "pool", "seat", "token", "expense")
MIN_CALLERS = 2                   # a door is shared: at least two callers go through it
MAX_FREEZE_ROUNDS = 5             # freeze, re-scan: a path found later through other callers is frozen too

REPO_CLASS = re.compile(r"(Repo|Repository|Store|Storage|Dao|DAO|Gateway|Persistence|Table|Collection)$")
REPO_MODULE = frozenset({"repo", "repos", "repository", "repositories", "store", "stores", "storage", "dao", "daos",
                         "db", "persistence", "gateway", "gateways", "crud"})
WRITE_VERBS = ("save", "add", "insert", "update", "delete", "remove", "set", "create", "put", "upsert", "write",
               "record", "append", "increment", "decrement", "bump", "mark", "apply", "store", "persist", "commit",
               "grant", "revoke", "approve", "reject", "cancel", "refund", "charge", "transfer", "debit", "credit",
               "reserve", "release", "lease", "assign", "enqueue", "publish", "send", "post", "patch", "replace",
               "merge", "move", "close", "open", "lock", "unlock", "reset", "clear", "purge", "drop", "archive")
# A write call: the method name, and a receiver that names a store (session, db, objects, collection ...).
ORM_WRITES = frozenset({"add", "add_all", "delete", "merge", "execute", "executemany", "save",
                        "create", "bulk_create", "bulk_update", "update", "update_or_create", "get_or_create",
                        "insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one",
                        "delete_many", "find_one_and_update", "find_one_and_delete", "bulk_write", "put_item",
                        "update_item", "delete_item", "batch_write_item", "transact_write_items", "modify", "capture"})
STORE_RECEIVERS = frozenset({"session", "db", "conn", "connection", "cursor", "cur", "objects", "collection", "coll",
                             "table", "engine", "tx", "txn", "transaction", "uow", "client", "dynamodb", "mongo",
                             "redis", "stripe"})      # not repo/store: calling a repository is not the write


# Types whose instances are a store client: a parameter or local annotated with one (`s: Session`,
# `db: AsyncSession`, `SessionDep = Annotated[Session, Depends(get_db)]`) is a store receiver, whatever its name
STORE_TYPES = frozenset({"Session", "AsyncSession", "scoped_session", "Connection", "AsyncConnection", "Engine",
                         "AsyncEngine", "Cursor", "Collection", "AsyncIOMotorCollection", "Database", "Table",
                         "Redis", "StrictRedis", "Pool", "Transaction"})


def _names_type(ann: ast.AST | None, aliases: frozenset[str] = frozenset()) -> bool:
    """True when an annotation names a store type (Session, Optional[Session], Annotated[Session, ...],
    a string annotation, or a module alias such as SessionDep)."""
    if ann is None:
        return False
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return False
    for n in ast.walk(ann):
        name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
        if name and (name in STORE_TYPES or name in aliases):
            return True
    return False


def store_aliases(tree: ast.Module) -> frozenset[str]:
    """Module-level names that alias a store type: `SessionDep = Annotated[Session, Depends(get_db)]`."""
    out = set()
    for st in tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) \
                and _names_type(st.value):
            out.add(st.targets[0].id)
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None \
                and _names_type(st.value):
            out.add(st.target.id)
    return frozenset(out)


def _typed_stores(fn: ast.AST, aliases: frozenset[str]) -> frozenset[str]:
    """The function's parameters and locals annotated with a store type."""
    out = set()
    args = getattr(fn, "args", None)
    if isinstance(args, ast.arguments):
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if _names_type(a.annotation, aliases):
                out.add(a.arg)
    for n in ast.walk(fn):
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and _names_type(n.annotation, aliases):
            out.add(n.target.id)
    return frozenset(out)


REGISTRATION = re.compile(r"(callback|listener|handler|hook|observer|subscriber)s?$")
# names of functions that only read: never a door (a door is where a write goes through)
READ_ONLY = re.compile(r"^_*(get|list|read|find|fetch|load|query|search|count|is|has|exists|iter|lookup|describe|"
                       r"show|peek|view|select)(_|$|[A-Z])")


def _read_only(fid: str) -> bool:
    """get_session, list_sessions, isReady: a name that says the function only reads."""
    return bool(READ_ONLY.match(fid.rsplit(".", 1)[-1]))


def _is_write_name(name: str) -> bool:
    """save, add_usage, setStatus: a write verb, alone or followed by _ or a capital (not 'settle')."""
    if name.startswith("__"):
        return False
    bare = name.lstrip("_")
    low = bare.lower()
    if REGISTRATION.search(low):
        return False                       # add_done_callback, add_listener: registering a hook is no write
    return any(low == v or low.startswith(v + "_") or (low.startswith(v) and bare[len(v):len(v) + 1].isupper())
               for v in WRITE_VERBS)


def _store_call(call: ast.Call, typed: frozenset[str] = frozenset()) -> str | None:
    """'session.add' for a call that writes through a store, else None (dict.update and set.add are not).
    typed: names known to hold a store client by their annotation (`s: Session`)."""
    f = call.func
    if not isinstance(f, ast.Attribute) or f.attr not in ORM_WRITES:
        return None
    if f.attr in ("execute", "executemany"):
        # a SQL string is not read (it is below the API): only a statement built by insert(...),
        # update(...) or delete(...) (SQLAlchemy Core) is a write
        arg = call.args[0] if call.args else None
        built = arg.func if isinstance(arg, ast.Call) else None
        while isinstance(built, ast.Attribute) and isinstance(built.value, ast.Call):
            built = built.value.func                        # update(T).where(...).values(...)
        name = built.attr if isinstance(built, ast.Attribute) else built.id if isinstance(built, ast.Name) else ""
        if name not in ("insert", "update", "delete"):
            return None
    recv = ast.unparse(f.value)
    words = _words(recv)
    if words & LOG_WORDS:
        return None                                         # a logging call
    if f.attr in CONTAINER_METHODS:
        # dict.update and set.add look like session.add: the receiver itself must be the store (not
        # an item of one, d["k"].update, nor an attribute of one, session.state.update)
        if isinstance(f.value, ast.Subscript):
            return None
        own = f.value.attr if isinstance(f.value, ast.Attribute) else f.value.id if isinstance(f.value, ast.Name) \
            else None
        if own is not None and not (_words(own) & STORE_RECEIVERS or own in typed):
            return None
    root = f.value
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(root, ast.Name) and root.id in typed:
        return f"{recv}.{f.attr}"
    if words & STORE_RECEIVERS or (f.attr == "create" and recv[:1].isupper()):    # stripe.Refund.create
        return f"{recv}.{f.attr}"
    return None


CONTAINER_METHODS = frozenset({"add", "update"})            # also dict.update, set.add
LOG_WORDS = frozenset({"log", "logger", "logging"})


def _words(name: str) -> set[str]:
    """The lower-case words of a name or dotted expression (self.dbSession -> self, db, session)."""
    return set(re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower()))


def _writes_in(fn: ast.AST, aliases: frozenset[str] = frozenset()) -> list[str]:
    """The store writes in a function's own body (not in nested functions)."""
    typed = _typed_stores(fn, aliases)
    out: list[str] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call) and (w := _store_call(n, typed)):
            out.append(w)
        stack.extend(ast.iter_child_nodes(n))
    return out


@dataclasses.dataclass
class Door:
    rid: str
    resource: str                            # the repository: a class or a module
    doors: list[str]                         # module.qualname of each door
    sinks: list[str]
    callers: list[str]                       # the doors' callers (the paths that do go through them)
    allow: list[str] = dataclasses.field(default_factory=list)
    back_doors: list[str] = dataclasses.field(default_factory=list)   # "file:line message"
    # fix round 28: the unresolved sites the scan could not follow, apart from the back doors: (at, code,
    # sha, message), listed for review as allow_unresolved entries to write once each is read
    unresolved: list[tuple[str, str, str, str]] = dataclasses.field(default_factory=list)
    active: bool = True                      # written as a rule (False: commented out, with the reason)
    why_inactive: str = ""
    score: int = 0

    def rule(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.rid,
                               "door": [f"py:{d}" for d in self.doors] if len(self.doors) > 1 else f"py:{self.doors[0]}",
                               "sink": [f"py:{s}" for s in self.sinks] if len(self.sinks) > 1 else f"py:{self.sinks[0]}"}
        if self.allow:
            out["allow"] = [f"py:{a}" for a in self.allow]
        return out


@dataclasses.dataclass
class DoorsFound:
    doors: list[Door]
    dropped: list[Door]                      # found, but beyond the limit
    no_door: dict[str, list[str]]            # repository -> its write functions, when no shared door exists
    files: int
    graph: str | None = None                 # what was read from a Graphify graph, when one was


SERVICE_MODULE = re.compile(r"^(services?|managers?|use_?cases?|domain|logic|workflows?|interactors?|commands?|"
                            r"\w+_services?|\w+_managers?)$")
SERVICE_CLASS = re.compile(r"(Service|Manager|UseCase|Interactor|Workflow|Handler)$")


def _service_like(fid: str) -> bool:
    """A method of a service class, or a function in a service module (judged by its own name only, so
    a package called pool_service does not make every module in it a service)."""
    segs = fid.split(".")[:-1]
    classes = [x for x in segs if x[:1].isupper()]
    if classes:
        return bool(SERVICE_CLASS.search(classes[-1]))
    return bool(segs) and bool(SERVICE_MODULE.match(segs[-1]))


ENTRY_NAMES = frozenset({"main", "<module>", "cli", "run", "handler", "lambda_handler"})
ROUTE_DECORATOR = re.compile(r"^(get|post|put|patch|delete|head|options|route|api_route|websocket)$")


def route_handlers(g: scanner.Graph) -> set[str]:
    """Functions registered as HTTP handlers: `@router.post(...)`, `@app.get(...)`, `@bp.route(...)`."""
    out = set()
    for fid, f in g.idx.funcs.items():
        for d in getattr(f.node, "decorator_list", []):
            target = d.func if isinstance(d, ast.Call) else d
            if isinstance(target, ast.Attribute) and ROUTE_DECORATOR.match(target.attr):
                out.add(fid)
    return out


def _doors_for(w: str, callers: dict[str, set[str]], real: set[str], inside: set[str],
               community: dict[str, int] | None = None, routes: frozenset[str] = frozenset()) -> list[str]:
    """The doors of writer w. Every service-layer caller is one (the service layer is allowed to call the
    repository). With a Graphify graph, so is every caller in another community than w that is itself
    called (a function between two layers, whatever it is named). Without either, the caller the most
    other functions go through (at least MIN_CALLERS)."""
    cands = sorted(c for c in callers[w] - inside if c in real and not _read_only(c))
    svc = [c for c in cands if _service_like(c)]
    if svc:
        return svc
    if community and w in community:
        # a layer between two others: in its own community, every caller known to the graph and in a
        # third one (so neither the write's layer nor its own), and not an entry point (main, a route)
        def layer(c: str) -> bool:
            up = callers[c] - {c} - inside
            return (c in community and community[c] != community[w] and bool(up) and c not in routes
                    and c.rsplit(".", 1)[-1] not in ENTRY_NAMES
                    and all(u in community and community[u] not in (community[c], community[w]) for u in up))
        bridges = [c for c in cands if layer(c)]
        if bridges:
            return bridges
    # the layer between HTTP routes and the writer, even one-to-one: called only by route handlers
    between = [c for c in cands if c not in routes and callers[c] - {c} and callers[c] - {c} <= routes]
    if between:
        return between
    best: tuple[int, int, str] | None = None
    for cand in cands:
        n = len(callers[cand] - {cand})
        key = (n, _score(cand), cand)          # most callers, then the most telling name, then by name
        if n >= MIN_CALLERS and (best is None or key > best):
            best = key
    return [best[2]] if best else []


def _score(*names: str) -> int:
    text = " ".join(names).lower()
    return sum(1 for k in IMPORTANT if k in text)


def _rule_id(d: Door, taken: set[str]) -> str:
    label = d.resource.rsplit(".", 1)[-1]
    label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", label)
    base = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").upper() + "-DOOR"
    rid, n = base, 2
    while rid in taken:
        rid, n = f"{base}-{n}", n + 1
    taken.add(rid)
    return rid


def _resource(g: scanner.Graph, fid: str) -> str | None:
    """The repository a function belongs to, when it is one: its Repo/Store class, or its repo module."""
    f = g.idx.funcs[fid]
    if f.cls and REPO_CLASS.search(f.cls.rsplit(".", 1)[-1]):
        return f.cls
    if not f.cls and f.module.rsplit(".", 1)[-1] in REPO_MODULE:
        return f.module
    return None


def sinks_of(g: scanner.Graph) -> dict[str, set[str]]:
    """Repository -> its write functions."""
    real = set(g.functions())
    groups: dict[str, set[str]] = collections.defaultdict(set)
    # aliases are collected across modules: SessionDep is written in deps.py and imported by the routes
    aliases = frozenset().union(*(store_aliases(mod.tree) for mod in g.idx.modules.values()))
    for fid in sorted(real):
        node = g.idx.funcs[fid].node
        name = fid.rsplit(".", 1)[-1]
        res = _resource(g, fid)
        writes = node is not None and bool(_writes_in(node, aliases))
        if res and (_is_write_name(name) or writes) and not name.startswith("_"):
            groups[res].add(fid)
        elif writes and not res:
            groups[g.idx.funcs[fid].module].add(fid)
    return groups


def discover(roots: Iterable[pathlib.Path], exclude: Iterable[str] = (), limit: int = 5,
             graph: pathlib.Path | None = None, project: pathlib.Path | None = None) -> DoorsFound:
    """Door rules for the repositories in the roots: which service API every call to a repository's
    write API must pass.

    graph: a Graphify graph.json; its communities and resolved calls propose more doors (see
    graphify.py). Each proposal is still checked by the scan before it is written."""
    roots, exclude = list(roots), list(exclude)
    g = scanner.graph(roots, exclude)
    real = set(g.functions())
    callers: dict[str, set[str]] = collections.defaultdict(set)
    for e in g.edges:
        if not e.inferred and e.caller != e.callee:
            callers[e.callee].add(e.caller)
    hint = None
    if graph is not None:
        from . import graphify
        hint = graphify.hints(graph, g, [b for b in (project, *roots) if b is not None])
        for a, b in hint.calls:                  # calls the graph resolved: discovery only, never the scan
            callers[b].add(a)
    community = hint.community if hint else None
    routes = frozenset(route_handlers(g))
    groups = sinks_of(g)
    found: list[Door] = []
    no_door: dict[str, list[str]] = {}
    for res in sorted(groups):
        members = groups[res]
        inside = {f for f in real if _resource(g, f) == res} | members      # the repository's own code
        door_list: list[str] = []
        for w in sorted(members):
            for door in _doors_for(w, callers, real, inside, community, routes):
                if door not in door_list:
                    door_list.append(door)
        if not door_list:
            no_door[res] = sorted(members)
            continue
        repo = _resource(g, sorted(members)[0]) == res
        # a repository's write methods are all sinks: nobody but a door may call any of them. Outside a
        # repository, only the writers that have a door are (the rest are listed with no door).
        sinks = sorted(members) if repo else sorted(w for w in members
                                                    if _doors_for(w, callers, real, inside, community, routes))
        via = sorted({c for d in door_list for c in callers[d]} - set(door_list))
        found.append(Door("", res, door_list, sinks, via, score=_score(res, *door_list, *sinks)))
    found.sort(key=lambda d: (-d.score, -len(d.callers), d.resource))
    kept, dropped = found[:limit], found[limit:]
    taken: set[str] = set()
    for d in kept + dropped:
        d.rid = _rule_id(d, taken)
    progress = scanner._Progress()                 # a line now and then on a large tree (fix round 29)
    for k, d in enumerate(kept):
        progress(f"freezing the back doors of drafted rule {d.rid}, rule", k, len(kept))
        _freeze(d, g, roots, exclude, progress)
    return DoorsFound(kept, dropped, no_door, len(g.idx.modules), hint.summary() if hint else None)


def _check(d: Door, g: scanner.Graph, roots: list[pathlib.Path], exclude: list[str]) -> scanner.Report:
    from .loader import expand_short_forms
    doc = expand_short_forms({"rules": [d.rule()]})
    return scanner.scan(doc, roots, exclude, g=g)


def _freeze(d: Door, g: scanner.Graph, roots: list[pathlib.Path], exclude: list[str],
            progress: Any = None) -> None:
    """Run the real scan on the proposed rule; freeze what it finds in allow, and confirm it then passes."""
    report = _check(d, g, roots, exclude)
    found = report.for_rule(d.rid)
    # fix round 28: a bypass (or forbidden idiom) is a back door; an unresolved site is not, and is listed
    # for review on its own (one at module level no longer drops the draft)
    d.back_doors = [f"{f.file}:{f.line} {f.message}" if f.file else f.message for f in found if f.kind != "unresolved"]
    d.unresolved = _unresolved(found)
    if any(not f.owner for f in found if f.kind != "unresolved"):
        d.active = False
        d.why_inactive = ("a back door is not inside a function (module-level code or a missing name), so it "
                          "cannot be frozen in allow")
        return
    # a back door is frozen with the call chain that reaches it today: allow lets a path through only
    # when every function on it is allowed, so a new caller of a frozen back door is still caught
    keep = {*d.doors, *d.sinks}
    frozen: set[str] = set()
    for r in range(MAX_FREEZE_ROUNDS):
        if progress is not None:
            progress(f"freezing the back doors of drafted rule {d.rid}, round", r + 1, MAX_FREEZE_ROUNDS)
        # (an unresolved site is reviewed, not frozen: fix round 28)
        more = ({n for f in found if f.kind != "unresolved" for n in [f.owner, *f.path] if n}
                | report.on_paths.get(d.rid, set())) - keep - frozen
        if r and not more:
            break                        # the same allow scans the same (fix round 29: unresolved sites
            # left kept a rule from passing, so every round re-ran the same scan)
        frozen |= more
        d.allow = sorted(frozen)
        report = _check(d, g, roots, exclude)
        found = report.for_rule(d.rid)
        if report.rule_result(d.rid) == "pass" or any(not f.owner for f in found if f.kind != "unresolved") \
                or not d.allow:
            break
    d.unresolved = _unresolved(found)
    if found and all(f.kind == "unresolved" for f in found):
        d.active = False
        d.why_inactive = (f"{len(found)} unresolved site(s) need review first: read each one; when it cannot "
                          f"reach the sink, accept it in allow_unresolved (drafted below: write why), else fix "
                          f"it; then uncomment the rule")
    elif report.rule_result(d.rid) != "pass":
        d.active = False
        d.why_inactive = ("the scan still fails with the back doors frozen; review the findings by hand" if d.allow
                          else "every finding is inside a door or sink, which allow cannot freeze; review the "
                               "findings by hand")


def _unresolved(found: list[scanner.Finding]) -> list[tuple[str, str, str, str]]:
    return [(f"{f.file}:{f.line}:{f.col}" if f.file else "", f.code, f.sha or "", f.message)
            for f in found if f.kind == "unresolved"]


def _yaml_list(xs: list[str]) -> str:
    return "[" + ", ".join(json.dumps(x) for x in xs) + "]"


def render(found: DoorsFound) -> list[str]:
    """The rule-file lines for the doors: active rules, or commented out with the reason."""
    if not found.doors and not found.dropped and not found.no_door:
        return []
    lines = ["  # Doors: each repository below is written through a service function. `inv check` fails a new",
             "  # code path that calls a write without passing its door.",
             "  # Nothing runs in production: `inv check` reads the source. Delete a rule you do not want.",
             "  #"]
    for d in found.doors:
        lines.append(f"  # {d.resource}: written by {', '.join(d.sinks)}")
        lines.append(f"  #   {len(d.callers)} caller(s) go through {' or '.join(d.doors)}")
        if d.back_doors:
            verb = "frozen in allow (fix each one, then delete it from allow)" if d.active else "found"
            lines.append(f"  # {len(d.back_doors)} back door(s) {verb}:")
            lines += [f"  #   {b}" for b in d.back_doors]
        r = d.rule()
        body = [f"- id: {r['id']}",
                f"  door: {json.dumps(r['door']) if isinstance(r['door'], str) else _yaml_list(r['door'])}",
                f"  sink: {json.dumps(r['sink']) if isinstance(r['sink'], str) else _yaml_list(r['sink'])}"]
        if d.allow:
            body.append("  allow:")
            body += [f"    - {json.dumps(a)}" for a in r["allow"]]
        if d.unresolved:
            lines.append(f"  # {len(d.unresolved)} unresolved site(s) to review (the scan cannot follow them):")
            lines += [f"  #   {at} {msg}" if at else f"  #   {msg}" for at, _, _, msg in d.unresolved]
            sites = [u for u in d.unresolved if u[0] and u[2]]
            if sites:                              # why is empty: the rule does not load until each says why
                body.append("  allow_unresolved:   # once its site is read, write why it cannot reach the sink")
                body += [f"    - {{at: {json.dumps(at)}, code: {code}, sha: {json.dumps(sha)}, why: \"\"}}"
                         for at, code, sha, _ in sites]
        if d.active:
            lines += [f"  {x}" for x in body]
        else:
            lines.append(f"  # not written as a rule: {d.why_inactive}")
            lines += [f"  # {x}" for x in body]
        lines.append("")
    if found.dropped:
        lines.append(f"  # More doors found but not written (at most {len(found.doors)}; --max-doors raises it):")
        lines += [f"  #   {d.resource} through {' or '.join(d.doors)}" for d in found.dropped]
        lines.append("")
    shared = found.no_door
    if shared:
        lines.append("  # Writes with no shared door (each is called from one place, or straight from entrypoints):")
        lines += [f"  #   {t}: {', '.join(w)}" for t, w in shared.items()]
        lines.append("")
    return lines


def summary(found: DoorsFound) -> list[str]:
    out = [found.graph] if found.graph else []
    for d in found.doors:
        state = "" if d.active else " (commented out: see the file)"
        out.append(f"door {d.rid}: {d.resource} through {' or '.join(d.doors)} ({len(d.callers)} "
                   f"caller(s)){state}")
        if d.back_doors:
            files = sorted({b.split(":", 1)[0] for b in d.back_doors})
            out.append(f"  found {len(d.back_doors)} back door(s) in {', '.join(files)}"
                       + ("; frozen in allow, so check passes and a new one fails" if d.active else ""))
        if d.unresolved:
            files = sorted({u[0].split(":", 1)[0] for u in d.unresolved if u[0]})
            out.append(f"  found {len(d.unresolved)} unresolved site(s) to review"
                       + (f" in {', '.join(files)}" if files else ""))
    if not found.doors:
        # the rule _doors_for applies: a service-layer caller, a layer between two others in the graph,
        # a caller only route handlers call, else one at least MIN_CALLERS callers go through
        out.append("found no door (no write is called from a service function (a *Service, *Manager or similar class, "
                   "or a services module), "
                   + ("from a function between two layers of the Graphify graph, " if found.graph else "")
                   + f"from a function only route handlers call, or from a function {MIN_CALLERS} or more "
                     f"callers go through)")
    return out
