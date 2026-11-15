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
approvals, then external clients and gateways (Stripe, an HTTP client: a write that leaves the process),
less a rule whose findings are mostly unresolved sites; at most `limit` are written and the rest are listed.
"""
from __future__ import annotations

import ast
import collections
import dataclasses
import json
import pathlib
import re
from typing import Any, Iterable

import invariants_scan as scanner
from invariants_scan.contracts import extracted_on_paths

# Names that suggest an invariant someone cares about: money, quotas, approvals, inventory.
IMPORTANT = ("balance", "ledger", "payment", "pay", "refund", "invoice", "credit", "debit", "charge", "transfer",
             "payout", "account", "wallet", "fund", "quota", "usage", "limit", "budget", "allowance", "approv",
             "permission", "role", "grant", "order", "stock", "inventory", "subscription", "billing", "price",
             "lease", "pool", "seat", "token", "expense")
# A class or module that names an external client or gateway: its writes leave the process (a charge, a
# request), so a door around them is a crown jewel. "api" counts in a class name only (an api module is
# usually the routes)
EXTERNAL = frozenset({"gateway", "gateways", "client", "stripe", "http", "https", "httpx", "requests", "paypal",
                      "braintree", "adyen", "plaid", "twilio", "sendgrid", "psp"})
EXTERNAL_BONUS = 2                # as two telling names
UNRESOLVED_PENALTY = 2            # a rule whose findings are mostly unresolved sites: review before it protects
LIST_CAP = 5                      # back doors and unresolved sites listed per rule; `inv scan` lists them all
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
    # only unresolved sites are left: written active (each is a warning for `inv check`), but commented
    # out with this reason under `inv init --strict-scan` (apply_strict), where each one would fail
    why_strict: str = ""
    score: int = 0
    # fix round 30: each sink -> its own doors, when the writes have different doors (drafted as a sink
    # mapping, so a write moved into another write's door is a bypass); None: one set of doors for all
    pairs: dict[str, list[str]] | None = None
    # round 32: with pairs, each frozen back door -> the sinks it reaches without a door (drafted as the
    # allow map); None with pairs: each in allow reaches every sink
    allow_to: dict[str, list[str]] | None = None

    def rule(self) -> dict[str, Any]:
        if self.pairs is not None:
            out: dict[str, Any] = {"id": self.rid, "sink": {f"py:{s}": [f"py:{d}" for d in self.pairs[s]]
                                                            for s in self.sinks}}
        else:
            out = {"id": self.rid,
                   "door": [f"py:{d}" for d in self.doors] if len(self.doors) > 1 else f"py:{self.doors[0]}",
                   "sink": [f"py:{s}" for s in self.sinks] if len(self.sinks) > 1 else f"py:{self.sinks[0]}"}
        if self.allow and self.pairs is not None:
            to = self.allow_to if self.allow_to is not None else {a: list(self.sinks) for a in self.allow}
            out["allow"] = {f"py:{a}": [f"py:{s}" for s in to[a]] for a in self.allow}
        elif self.allow:
            out["allow"] = [f"py:{a}" for a in self.allow]
        return out


@dataclasses.dataclass
class DoorsFound:
    doors: list[Door]
    dropped: list[Door]                      # found, but beyond the limit
    no_door: dict[str, list[str]]            # repository -> its write functions, when no shared door exists
    files: int
    graph: str | None = None                 # what was read from a Graphify graph, when one was
    strict: bool = False                     # `inv init --strict-scan`: allow_unresolved entries are drafted


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


def _external(*names: str) -> bool:
    """True when a dotted class or module name names an external client or gateway (StripeGateway,
    HTTPClient, stripe_client, PaymentApi)."""
    for name in names:
        for seg in name.split("."):
            words = set(re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])", r"\1\3_\2\4",
                                                        seg).lower()))
            if words & EXTERNAL or (seg[:1].isupper() and "api" in words):
                return True
    return False


def _dominated(unresolved: int, other: int) -> bool:
    """A rule dominated by unresolved sites: more of them than back doors."""
    return unresolved > other


def _demote_unresolved(found: list[Door], g: scanner.Graph, roots: list[pathlib.Path],
                       exclude: list[str]) -> None:
    """Lower the score of each drafted rule whose findings, before anything is frozen, are mostly unresolved
    sites: one scan of every candidate at once (the graph is shared), so ranking costs a single scan."""
    from .loader import expand_short_forms
    for i, d in enumerate(found):
        d.rid = f"RANK-{i}"                  # for this scan only: the rule ids are given after ranking
    report = scanner.scan(expand_short_forms({"rules": [d.rule() for d in found]}), roots, exclude, g=g)
    for d in found:
        fs = report.for_rule(d.rid)
        unresolved = sum(1 for f in fs if f.kind == "unresolved")
        if _dominated(unresolved, len(fs) - unresolved):
            d.score -= UNRESOLVED_PENALTY


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


def discover(roots: Iterable[pathlib.Path], exclude: Iterable[str] = (), limit: int = 3,
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
        per = {w: _doors_for(w, callers, real, inside, community, routes) for w in sorted(members)}
        for w in sorted(members):
            for door in per[w]:
                if door not in door_list:
                    door_list.append(door)
        if not door_list:
            no_door[res] = sorted(members)
            continue
        repo = _resource(g, sorted(members)[0]) == res
        # a repository's write methods are all sinks: nobody but a door may call any of them. Outside a
        # repository, only the writers that have a door are (the rest are listed with no door).
        sinks = sorted(members) if repo else sorted(w for w in members if per[w])
        via = sorted({c for d in door_list for c in callers[d]} - set(door_list))
        # fix round 30: writes whose doors differ get a sink mapping, each write with its own doors (a
        # repository write no door calls keeps every door, as in the list form)
        pairs = ({w: per[w] or list(door_list) for w in sinks}
                 if len({tuple(sorted(per[w])) for w in sinks if per[w]}) > 1 else None)
        owners = [res, *(g.idx.funcs[s].cls or "" for s in sinks)]     # a class's name holds its module's
        score = _score(res, *door_list, *sinks) + (EXTERNAL_BONUS if _external(*owners) else 0)
        found.append(Door("", res, door_list, sinks, via, score=score, pairs=pairs))
    if len(found) > 1:
        _demote_unresolved(found, g, roots, exclude)
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
    if d.pairs is not None and len(d.sinks) > 1:
        # round 32: frozen sink by sink, so each back door is drafted with the sinks it reaches (allow is a
        # map there: an entry excuses only the sinks it lists)
        to: dict[str, list[str]] = {}
        for s in d.sinks:
            sub = Door(d.rid, d.resource, list(d.pairs[s]), [s], [], pairs={s: d.pairs[s]})
            sub_report = _check(sub, g, roots, exclude)
            if any(f.kind != "unresolved" for f in sub_report.for_rule(d.rid)) \
                    or extracted_on_paths(sub_report.on_paths, d.rid) - keep:
                _freeze_rounds(sub, g, roots, exclude, progress, keep, sub_report)
            for a in sub.allow:
                to.setdefault(a, []).append(s)
        d.allow, d.allow_to = sorted(to), to
        report = _check(d, g, roots, exclude)
    else:
        report = _freeze_rounds(d, g, roots, exclude, progress, keep, report)
    found = report.for_rule(d.rid)
    d.unresolved = _unresolved(found)
    if found and all(f.kind == "unresolved" for f in found):
        # diff-first: written active, its unresolved sites listed; `inv check` warns about each (with --base
        # only a new one fails); with --strict-scan each would fail, so the rule waits for review (apply_strict)
        d.why_strict = (f"{len(found)} unresolved site(s) need review first: read each one; when it cannot "
                        f"reach the sink, accept it in allow_unresolved (drafted below: write why), else fix "
                        f"it; then uncomment the rule")
    elif report.rule_result(d.rid) != "pass":
        d.active = False
        d.why_inactive = ("the scan still fails with the back doors frozen; review the findings by hand" if d.allow
                          else "every finding is inside a door or sink, which allow cannot freeze; review the "
                               "findings by hand")


def _freeze_rounds(d: Door, g: scanner.Graph, roots: list[pathlib.Path], exclude: list[str], progress: Any,
                   keep: set[str], report: scanner.Report) -> scanner.Report:
    """Freeze in d.allow the functions on each door-free path, re-scan, and repeat until the rule passes
    (at most MAX_FREEZE_ROUNDS); the last report."""
    found = report.for_rule(d.rid)
    frozen: set[str] = set()
    for r in range(MAX_FREEZE_ROUNDS):
        if progress is not None:
            progress(f"freezing the back doors of drafted rule {d.rid}, round", r + 1, MAX_FREEZE_ROUNDS)
        # (an unresolved site is reviewed, not frozen: fix round 28)
        # fix round 30: only the functions on an extracted door-free path; one on a name-matched path alone
        # holds an inferred-path site, which stays an unresolved finding (drafted in allow_unresolved)
        more = ({n for f in found if f.kind != "unresolved" for n in [f.owner, *f.path] if n}
                | extracted_on_paths(report.on_paths, d.rid)) - keep - frozen
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
    return report


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
    multi = _shared_sites(found)
    for d in found.doors:
        lines.append(f"  # {d.resource}: written by {', '.join(d.sinks)}")
        lines.append(f"  #   {len(d.callers)} caller(s) go through {' or '.join(d.doors)}")
        if d.back_doors:
            verb = "frozen in allow (fix each one, then delete it from allow)" if d.active else "found"
            lines.append(f"  # {len(d.back_doors)} back door(s) {verb}:")
            lines += _capped([f"  #   {b}" for b in d.back_doors], "  #   ")
        r = d.rule()
        if d.pairs is not None:                    # each write with its own doors (fix round 30)
            lines.append("  # each write has its own door(s): a door of one write is no door for another")
            body = [f"- id: {r['id']}", "  sink:"]
            body += [f"    {json.dumps(s)}: {_yaml_list(ds)}" for s, ds in r["sink"].items()]
        else:
            body = [f"- id: {r['id']}",
                    f"  door: {json.dumps(r['door']) if isinstance(r['door'], str) else _yaml_list(r['door'])}",
                    f"  sink: {json.dumps(r['sink']) if isinstance(r['sink'], str) else _yaml_list(r['sink'])}"]
        if d.allow and isinstance(r["allow"], dict):  # each back door with the sinks it reaches (round 32)
            body.append("  allow:   # each back door may reach only the sinks it lists")
            body += [f"    {json.dumps(a)}: {_yaml_list(ss)}" for a, ss in r["allow"].items()]
        elif d.allow:
            body.append("  allow:")
            body += [f"    - {json.dumps(a)}" for a in r["allow"]]
        if d.unresolved:
            lines.append(f"  # {len(d.unresolved)} unresolved site(s) to review (the scan cannot follow them):")
            lines += _capped([f"  #   {at} {msg}" if at else f"  #   {msg}" for at, _, _, msg in d.unresolved],
                             "  #   ")
            # a site seen under several drafted rules is drafted once, in the top-level allow_unresolved
            sites = [u for u in d.unresolved if u[0] and u[2] and (u[0], u[1], u[2]) not in multi]
            if found.strict and len(sites) < len([u for u in d.unresolved if u[0] and u[2]]):
                lines.append("  #   (a site seen under other drafted rules too is in allow_unresolved below)")
            if sites and found.strict:             # why is empty: the rule does not load until each says why
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


def _capped(lines: list[str], prefix: str) -> list[str]:
    """At most LIST_CAP of the lines, then one that says how many more there are."""
    if len(lines) <= LIST_CAP:
        return lines
    return lines[:LIST_CAP] + [f"{prefix}… and {len(lines) - LIST_CAP} more (`inv scan` lists them all)"]


def _files(names: Iterable[str]) -> str:
    """The first LIST_CAP file names, then how many more."""
    xs = sorted(set(names))
    more = f", … and {len(xs) - LIST_CAP} more" if len(xs) > LIST_CAP else ""
    return ", ".join(xs[:LIST_CAP]) + more


def _shared_sites(found: DoorsFound) -> dict[tuple[str, str, str], list[Door]]:
    """(at, code, sha) -> the drafted rules that see the site, for each unresolved site two or more of them
    see (fix round 30: drafted once, in the file's top-level allow_unresolved, with rules: [...])."""
    by: dict[tuple[str, str, str], list[Door]] = {}
    for d in found.doors:
        for at, code, sha, _ in d.unresolved:
            if at and sha and d not in by.setdefault((at, code, sha), []):
                by[(at, code, sha)].append(d)
    return {k: ds for k, ds in by.items() if len(ds) > 1}


def render_shared(found: DoorsFound) -> list[str]:
    """The file's top-level allow_unresolved: one drafted entry per unresolved site that several drafted
    rules see, listing them. Commented out while any rule it lists is (a shared entry names rules that
    exist); why is empty, so it does not load until each says why."""
    shared = _shared_sites(found)
    if not shared or not found.strict:            # drafted only under `inv init --strict-scan`
        return []
    lines = ["",
             "# Unresolved sites the scan sees under several drafted door rules: each entry is one reviewed",
             "# site, accepted once under every rule it lists. Read each site; when it cannot reach a sink,",
             "# write why (else fix it), and uncomment this with the rules it lists."]
    live = all(d.active for ds in shared.values() for d in ds)
    body = ["allow_unresolved:"]
    body += [f"  - {{at: {json.dumps(at)}, code: {code}, sha: {json.dumps(sha)}, why: \"\", "
             f"rules: [{', '.join(sorted(d.rid for d in ds))}]}}" for (at, code, sha), ds in shared.items()]
    return lines + (body if live else [f"# {x}" for x in body])


def apply_strict(found: DoorsFound) -> None:
    """`inv init --strict-scan`: draft allow_unresolved entries, and comment out each rule only unresolved
    sites are left in (with its reason), since `inv check --strict-scan` fails every one until reviewed."""
    found.strict = True
    for d in found.doors:
        if d.active and d.why_strict:
            d.active, d.why_inactive = False, d.why_strict


def summary(found: DoorsFound) -> list[str]:
    out = [found.graph] if found.graph else []
    for d in found.doors:
        state = "" if d.active else " (commented out: see the file)"
        paired = ", each write through its own door(s)" if d.pairs is not None else ""
        out.append(f"door {d.rid}: {d.resource} through {' or '.join(d.doors)} ({len(d.callers)} "
                   f"caller(s){paired}){state}")
        if d.back_doors:
            out.append(f"  found {len(d.back_doors)} back door(s) in {_files(b.split(':', 1)[0] for b in d.back_doors)}"
                       + ("; frozen in allow, so check passes and a new one fails" if d.active else ""))
        if d.unresolved:
            files = _files(u[0].split(":", 1)[0] for u in d.unresolved if u[0])
            out.append(f"  found {len(d.unresolved)} unresolved site(s) to review" + (f" in {files}" if files else ""))
    if not found.doors:
        # the rule _doors_for applies: a service-layer caller, a layer between two others in the graph,
        # a caller only route handlers call, else one at least MIN_CALLERS callers go through
        out.append("found no door (no write is called from a service function (a *Service, *Manager or similar class, "
                   "or a services module), "
                   + ("from a function between two layers of the Graphify graph, " if found.graph else "")
                   + f"from a function only route handlers call, or from a function {MIN_CALLERS} or more "
                     f"callers go through)")
    return out


# The run-time door check lives in doors_runtime; `inv.doors.install(...)` names it through this module.
from .doors_runtime import (  # noqa: E402
    BypassError as BypassError, DoorsHandle as DoorsHandle, active as active, expected as expected,
    install as install, installed as installed,
)
