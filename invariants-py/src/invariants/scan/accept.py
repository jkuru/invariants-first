"""Reviewed exceptions: mediation.allow_unresolved accepts an unresolved finding, one site at a time.

A person who has read an unresolved site and found it cannot reach the sink writes an entry for it:

    allow_unresolved:
      - {at: "app/plugins.py:41", code: unresolved/reflection, sha: 3f9c0a17be52d4e1,
         why: "loads the export plugins named in settings.EXPORTERS; none imports app.store"}

An entry matches a finding of that rule with the same file, code and sha. The sha (16 hex digits) is a
hash of the finding's code, the function it is in and the whole statement it is in (every line of it,
spacing normalized; for a compound statement, its header up to the colon). An unresolved/inferred-path
finding's sha also covers what reaches its site: the entrypoints from which it is reached without a door
and every name-matched call (caller -> callee) on those paths, so a new untyped caller of the helper is
a new, unaccepted finding. So an edit above the site that moves it keeps the entry (the finding is
reported as moved, with its new line), and an edit of any line of the statement does not. Each entry
accepts one finding: the one at its line first, else the nearest with the same sha (the same statement
text in the same function: an identical statement elsewhere in the function is indistinguishable from
the reviewed one moved, as an edit of the code above an unmoved site is undetectable; the move is
reported). A matched finding is shown as accepted and never fails the rule; an entry that matches
nothing is missing/allow-unresolved-unused. bypass and forbidden findings can never be accepted (the
rule file refuses such an entry). Adding or widening an entry (or adding a second entry for the same
site) is a weakening in the lock; removing one is not.
"""
from __future__ import annotations

import ast
import hashlib
import io
import keyword
import pathlib
import textwrap
import tokenize
from typing import Any, Callable

from .contracts import Accept, Finding, _Edge, _Fan

SHA_HEX = 16            # the length of a site's sha


class _Lines:
    """The source of the files findings are in, read and parsed once each."""

    def __init__(self, trees: dict[pathlib.Path, ast.Module] | None = None) -> None:
        self._files: dict[pathlib.Path, list[str]] = {}
        self._stmts: dict[pathlib.Path, list[tuple[int, int, int, int, int, ast.AST]] | None] = {}
        self._trees = trees or {}                  # the index's own trees (fix round 28: not parsed again)
        self._plain: set[pathlib.Path] = set()     # files whose lines are the parser's lines

    def _read(self, path: pathlib.Path) -> list[str]:
        if path not in self._files:
            try:
                text = path.read_bytes().decode("utf-8", "replace")
            except OSError:
                text = ""
            self._files[path] = text.splitlines()
            if len(self._files[path]) == text.count("\n") + (0 if text.endswith("\n") or not text else 1):
                self._plain.add(path)              # no \r, form feed or other separator splitlines() splits on
        return self._files[path]

    def line(self, root: str | None, file: str, line: int) -> str:
        lines = self._read(pathlib.Path(root or ".") / file)
        return lines[line - 1] if 0 < line <= len(lines) else ""

    def _spans(self, path: pathlib.Path) -> list[tuple[int, int, int, int, int, ast.AST]] | None:
        """(first line, first col, last line, last col, last line of the part hashed, node) of every
        statement (an `except` clause and a `case` count as one): a simple statement is hashed whole, a
        compound one (def, class, if, for, with, try, match) by its header (decorators included): the
        lines before its body, or through the body's first line when the body starts on the header's last
        line (`if x: y()`). None: the file does not parse."""
        if path not in self._stmts:
            lines = self._read(path)
            tree = self._trees.get(path) if path in self._plain else None
            if tree is None:
                try:
                    tree = ast.parse("\n".join(lines))
                except (SyntaxError, ValueError):
                    self._stmts[path] = None
                    return None
            out: list[tuple[int, int, int, int, int, ast.AST]] = []
            for n in ast.walk(tree):
                if isinstance(n, ast.match_case):    # a case has no position of its own: its pattern's
                    b = n.body[-1]
                    if b.end_lineno is not None and b.end_col_offset is not None:
                        own = not lines[n.body[0].lineno - 1][:n.body[0].col_offset].strip()
                        last = max(n.pattern.lineno, n.body[0].lineno - 1 if own else n.body[0].lineno)
                        out.append((n.pattern.lineno, n.pattern.col_offset, b.end_lineno, b.end_col_offset,
                                    last, n))
                    continue
                if not isinstance(n, (ast.stmt, ast.excepthandler)) or n.end_lineno is None \
                        or n.end_col_offset is None:
                    continue
                start, col = n.lineno, n.col_offset
                for d in getattr(n, "decorator_list", ()):
                    if d.lineno < start:
                        start, col = d.lineno, 0
                body = getattr(n, "body", None)
                last = n.end_lineno
                if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                    b = body[0]                  # the header: up to the line before the body, or the
                    own = not lines[b.lineno - 1][:b.col_offset].strip()    # body's line (`if x: y()`)
                    last = max(n.lineno, b.lineno - 1 if own else b.lineno)
                elif isinstance(n, ast.Match) and n.cases:
                    last = max(n.lineno, n.cases[0].pattern.lineno - 1)
                out.append((start, col, n.end_lineno, n.end_col_offset, last, n))
            self._stmts[path] = out
        return self._stmts[path]

    def _best(self, path: pathlib.Path, line: int, col: int) -> tuple[int, int, int, int, int, ast.AST] | None:
        """The innermost statement whose hashed part holds line:col (1-based col): the one that starts last."""
        pos = (line, max(col - 1, 0))
        best: tuple[int, int, int, int, int, ast.AST] | None = None
        for s in self._spans(path) or ():
            inside = (s[0], s[1]) <= pos < (s[2], s[3]) or (col <= 1 and s[0] <= line <= s[2])
            if inside and line <= s[4] and (best is None or (s[0], s[1]) >= (best[0], best[1])):
                best = s
        return best

    def statement(self, root: str | None, file: str, line: int, col: int) -> str:
        """The source of the innermost statement at line:col (1-based col): every line of a simple
        statement, the header of a compound one. A position in no statement's hashed part (an `else:`
        line, a file that does not parse) gives its physical line."""
        path = pathlib.Path(root or ".") / file
        best = self._best(path, line, col)
        if best is None:
            return self.line(root, file, line)
        return "\n".join(self._read(path)[best[0] - 1:best[4]])

    def site(self, root: str | None, file: str, line: int, col: int) -> ast.AST | str:
        """What a finding's sha hashes: the innermost statement at line:col (its node), else the physical
        line's text (an `else:` line, a file that does not parse)."""
        best = self._best(pathlib.Path(root or ".") / file, line, col)
        return best[5] if best is not None else self.line(root, file, line)


_LAYOUT = frozenset({tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT,
                     tokenize.ENDMARKER})


def _normalized(text: str) -> str:
    """The statement's tokens, one space apart: a reformat (line breaks, spacing, comments) keeps it, any
    change of a token does not. An f-string (or t-string) is one token, as written, on every Python
    version (3.12 splits it into parts). Text that does not tokenize: its words, one space apart."""
    src = text.strip() + "\n"
    rows = src.splitlines(keepends=True)
    out: list[tuple[int, str]] = []
    depth, start = 0, (0, 0)
    try:
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            name = tokenize.tok_name.get(t.type, "")
            if name in ("FSTRING_START", "TSTRING_START"):
                if depth == 0:
                    start = t.start
                depth += 1
            elif name in ("FSTRING_END", "TSTRING_END"):
                depth -= 1
                if depth == 0:                   # the source of the whole string
                    (r0, c0), (r1, c1) = start, t.end
                    out.append((tokenize.STRING, rows[r0 - 1][c0:] + "".join(rows[r0:r1 - 1]) + rows[r1 - 1][:c1]
                                if r1 > r0 else rows[r0 - 1][c0:c1]))
            elif depth == 0 and t.type not in _LAYOUT:
                out.append((t.type, t.string))
    except (tokenize.TokenError, SyntaxError):
        return " ".join(text.split())
    return " ".join(_without_magic_commas(out))


def _without_magic_commas(toks: list[tuple[int, str]]) -> list[str]:
    """The token strings, without a trailing comma right before a closing bracket (`f(a,)` is `f(a)`: black
    adds one when it splits a call over lines; fix round 28), except where the comma makes a tuple: the
    only comma in a parenthesized tuple `(x,)` or in a subscript `a[x,]`, which differ from `(x)` and
    `a[x]`."""
    out: list[str] = []
    stack: list[list[Any]] = []                    # [tuple-like bracket, commas in it]
    prev: tuple[int, str] | None = None
    for typ, s in toks:
        if typ == tokenize.OP and s in ("(", "[", "{"):
            after_value = prev is not None and (prev[1] in (")", "]", "}") or prev[0] == tokenize.STRING or (
                prev[0] == tokenize.NAME and not keyword.iskeyword(prev[1])))
            called = after_value or (prev is not None and prev[1] == "import")
            stack.append([(s == "(" and not called) or (s == "[" and after_value), 0])
        elif typ == tokenize.OP and s == "," and stack:
            stack[-1][1] += 1
        elif typ == tokenize.OP and s in (")", "]", "}"):
            if prev == (tokenize.OP, ",") and stack and not (stack[-1][0] and stack[-1][1] == 1):
                out.pop()
            if stack:
                stack.pop()
        out.append(s)
        prev = (typ, s)
    return out


# fix round 29: what a statement means, not how it is written. A formatter (black, ruff format) changes
# quotes, parentheses, number case and line breaks; none changes the syntax tree. Python's own ast.dump
# is not the key: 3.13 leaves out empty fields that 3.11 and 3.12 show.
_BLOCKS = frozenset({"body", "orelse", "finalbody", "handlers", "cases"})   # a compound statement's blocks
_UNSAID = frozenset({"ctx", "kind", "type_comment"})   # set by where a name is, u"" and `# type:` comments


def _tree(node: Any, header: bool = False) -> str:
    """The syntax tree under node without positions, the same on every Python version: a node is its class
    and its fields (an empty or None field left out, as 3.13 leaves it out); header: without the blocks of
    a compound statement (its header alone: the test, the iterable and target, the items, the decorators
    and signature, the subject, an except clause's type and name, a case's pattern and guard)."""
    if isinstance(node, ast.AST):
        fields = []
        for name in node._fields:
            if name in _UNSAID or (header and name in _BLOCKS):
                continue
            v = getattr(node, name, None)
            if v is None or (isinstance(v, list) and not v):
                continue
            fields.append(f"{name}={_tree(v)}")
        return f"{type(node).__name__}({', '.join(fields)})"
    if isinstance(node, list):
        return "[" + ", ".join(_tree(x) for x in node) + "]"
    try:
        return repr(node)
    except ValueError:                           # an int too long to print (sys.set_int_max_str_digits)
        return f"int:{node.bit_length()}:{node % (1 << 64)}"


def _key(stmt: ast.AST | str) -> str:
    """What a site's sha hashes for its statement: its syntax tree (a compound one's header), so a
    reformat never changes it and a change of any token that means something does. A statement given as
    text is parsed first; text that is not one statement (a physical line on its own) is its tokens."""
    if isinstance(stmt, str):
        try:
            body = ast.parse(textwrap.dedent(stmt.strip("\n"))).body
        except (SyntaxError, ValueError):
            body = []
        if len(body) != 1:
            return "text " + _normalized(stmt)
        stmt = body[0]
    return "tree " + _tree(stmt, header=True)


def site_sha(code: str, owner: str | None, stmt: ast.AST | str, basis: list[str] | None = None) -> str:
    """The key of an unresolved finding: its code, its function, its statement's syntax tree (_key) and,
    for an inferred path, what reaches it (basis: the entrypoints and name-matched calls)."""
    parts = [code, owner or "", _key(stmt), *(["reached by", *basis] if basis is not None else [])]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:SHA_HEX]


def _text_sha(code: str, owner: str | None, text: str, basis: list[str] | None = None) -> str:
    """The sha inv gave a site before fix round 29 (its statement's tokens): an entry that still names one
    is told the new sha of the same site, not only that it matches nothing."""
    parts = [code, owner or "", _normalized(text), *(["reached by", *basis] if basis is not None else [])]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:SHA_HEX]


Basis = Callable[[Finding], list[str]]


SHOWN = 50              # the most of what reaches an inferred path a finding lists (its sha covers all)


def defer_sha(findings: list[Finding], lines: _Lines, basis: Callable[[], Basis | None]) -> None:
    """fill_sha, run when a finding's sha (or reached_by) is first read (fix round 28): a scan whose
    output shows no sha (text, for a rule with no allow_unresolved) never hashes a statement."""
    todo = [f for f in findings if f.kind == "unresolved" and f.file and f.line > 0
            and f.__dict__.get("sha") is None]
    if not todo:
        return

    def fill() -> None:
        for f in todo:
            f.__dict__.pop("_fill", None)
        fill_sha(todo, lines, basis())
    for f in todo:
        f.__dict__["_fill"] = fill


def fill_sha(findings: list[Finding], lines: _Lines, basis: Basis | None = None, old: bool = False) -> None:
    """Give every unresolved finding in a file its sha (what an allow_unresolved entry names). basis, for
    an unresolved/inferred-path finding: what reaches its site, which its sha covers too (the finding
    lists the first SHOWN of it). old: also the sha an earlier inv gave it (_text_sha), for apply."""
    for f in findings:
        if f.kind == "unresolved" and f.file and f.line > 0 and f.sha is None:
            b = basis(f) if basis is not None and f.code == "unresolved/inferred-path" else None
            f.sha = site_sha(f.code, f.owner, lines.site(f.root, f.file, f.line, f.col), b)
            if old:
                f.__dict__["_text_sha"] = _text_sha(f.code, f.owner, lines.statement(f.root, f.file, f.line,
                                                                                     f.col), b)
            if b is not None:
                f.reached_by = b[:SHOWN] + ([f"and {len(b) - SHOWN} more"] if len(b) > SHOWN else [])


def _site(f: Finding) -> str:
    """The function an inferred-path finding's site is in (the caller of its last call)."""
    return f.path[-2] if len(f.path) >= 2 else f.owner or ""


class InferredBasis:
    """What reaches the site of an unresolved/inferred-path finding, in one rule's door-free graph with
    name-matched calls: the entrypoints from which the site's function is reached, and every name-matched
    call on a path from them through that function to the sink ("caller -> .name", one per call). succ:
    the door-free graph (caller -> its calls) of the search pass that follows name-matched calls; starts:
    the entrypoints the search starts from; goals: the live sinks; findings: the rule's findings (the
    sites of all its inferred paths are worked out together, in one pass over the graph)."""

    def __init__(self, succ: dict[str, list[_Edge | _Fan]], starts: list[str], goals: set[str],
                 findings: list[Finding]) -> None:
        self.succ, self.starts, self.goals = succ, starts, goals
        self.sites = sorted({_site(f) for f in findings if f.code == "unresolved/inferred-path"})
        self._out: dict[str, list[str]] | None = None

    def __call__(self, f: Finding) -> list[str]:
        if self._out is None:
            self._out = self._all()
        return self._out.get(_site(f), [])

    @staticmethod
    def _callees(e: _Edge | _Fan) -> tuple[str, ...]:
        return e.callees if isinstance(e, _Fan) else (e.callee,)

    def _next(self, n: str, nodes: set[str]) -> list[str]:
        return [x for e in self.succ.get(n, ()) for x in self._callees(e) if x in nodes]

    def _all(self) -> dict[str, list[str]]:
        succ, names = self.succ, self.sites
        bit = {s: 1 << i for i, s in enumerate(names)}
        stack = [s for s in self.starts if s in succ]
        seen = set(stack)                        # every function reached from an entrypoint
        known = set(succ)
        while stack:
            for x in self._next(stack.pop(), known):
                if x not in seen:
                    seen.add(x)
                    stack.append(x)
        reach = self._reach(seen, bit)           # function -> the sites it reaches (a bit per site)
        entry: dict[str, list[str]] = {s: [] for s in names}
        calls: dict[str, list[str]] = {s: [] for s in names}

        def add(to: dict[str, list[str]], m: int, text: str) -> None:
            while m:
                one = m & -m
                to[names[one.bit_length() - 1]].append(text)
                m ^= one
        for s in sorted(set(self.starts) & seen):
            add(entry, reach[s], f"entrypoint {s}")
        fans: dict[int, int] = {}                # a name's callees -> the sites they reach
        for c in sorted(seen):
            for e in succ.get(c, ()):
                if not e.inferred:
                    continue
                cs = self._callees(e)
                m = fans.get(id(cs))
                if m is None:
                    m = 0
                    for x in cs:
                        m |= reach.get(x, 0)
                    fans[id(cs)] = m
                if c in bit and any(x in self.goals for x in cs):
                    m |= bit[c]                  # the site's own name-matched call into the sink
                add(calls, m, f"{c} -> .{cs[0].rsplit('.', 1)[-1]}")
        return {s: entry[s] + sorted(calls[s]) for s in names}

    def _reach(self, nodes: set[str], bit: dict[str, int]) -> dict[str, int]:
        """For each function in nodes, the sites (bits) it reaches over succ, itself included: its strongly
        connected component (iterative Tarjan) is finished after every component it calls into."""
        reach: dict[str, int] = {}
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on: set[str] = set()
        comp: list[str] = []
        for root in sorted(nodes):
            if root in index:
                continue
            index[root] = low[root] = len(index)
            comp.append(root)
            on.add(root)
            work = [(root, iter(self._next(root, nodes)))]
            while work:
                n, it = work[-1]
                for x in it:
                    if x not in index:
                        index[x] = low[x] = len(index)
                        comp.append(x)
                        on.add(x)
                        work.append((x, iter(self._next(x, nodes))))
                        break
                    if x in on:
                        low[n] = min(low[n], index[x])
                else:
                    work.pop()
                    if work:
                        low[work[-1][0]] = min(low[work[-1][0]], low[n])
                    if low[n] == index[n]:       # n roots a component: all it calls into is done
                        members = []
                        while True:
                            y = comp.pop()
                            on.discard(y)
                            members.append(y)
                            if y == n:
                                break
                        m = 0
                        for y in members:
                            m |= bit.get(y, 0)
                            for z in self._next(y, nodes):
                                m |= reach.get(z, 0)
                        for y in members:
                            reach[y] = m
        return reach


def apply(rule: str, entries: list[Accept], findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """(the findings still reported, the accepted ones): each entry accepts one unresolved finding with
    its file, code and sha, the one at its line (and column, when given) first, else the nearest; an entry
    that matches none is a missing/allow-unresolved-unused finding. Entries at their line are matched
    before any entry is matched by nearness, so a moved entry never takes the site another entry names."""
    taken: set[int] = set()
    accepted: list[Finding] = []
    unused: list[Finding] = []
    picks: dict[int, Finding] = {}

    def cands(e: Accept) -> list[Finding]:
        return [f for f in findings if id(f) not in taken and f.kind == "unresolved" and f.file == e.file
                and f.code == e.code and f.sha == e.sha]
    for i, e in enumerate(entries):                  # first every entry whose site is where it says
        here = [f for f in cands(e) if f.line == e.line and (e.col is None or f.col == e.col)]
        if here:
            picks[i] = here[0]
            taken.add(id(here[0]))
    for i, e in enumerate(entries):                  # then the moved ones, to the nearest same statement
        if i in picks:
            continue
        pick = min(cands(e), key=lambda f: (abs(f.line - e.line), f.line, f.col), default=None)
        if pick is None:
            was = min((f for f in findings if id(f) not in taken and f.kind == "unresolved" and f.file == e.file
                       and f.code == e.code and f.__dict__.get("_text_sha") == e.sha),
                      key=lambda f: (abs(f.line - e.line), f.line, f.col), default=None)
            if was is not None:                  # fix round 29: a sha from before the syntax-tree hash
                unused.append(Finding("missing", rule, "missing/allow-unresolved-unused",
                                      f"allow_unresolved entry {e.at} ({e.code}) has a sha {e.sha} from an "
                                      f"earlier inv, which hashed the statement's text (a reformat changed it); "
                                      f"the site at {was.file}:{was.line} is the statement reviewed, unchanged: "
                                      f"write sha {was.sha} in the entry, then run `inv lock`",
                                      e.file, e.line, e.col or 1))
                continue
            more = (" (for an inferred path: also when the entrypoints or name-matched calls that reach the "
                    "site changed, such as a new untyped caller)" if e.code == "unresolved/inferred-path" else "")
            unused.append(Finding("missing", rule, "missing/allow-unresolved-unused",
                                  f"allow_unresolved entry {e.at} ({e.code}, sha {e.sha}) matches no unresolved "
                                  f"finding: the site was edited, removed or now resolves{more}; delete the entry "
                                  f"(a tightening), or review the site again and write a new one",
                                  e.file, e.line, e.col or 1))
            continue
        picks[i] = pick
        taken.add(id(pick))
    for i, e in enumerate(entries):
        if i not in picks:
            continue
        pick = picks[i]
        pick.why = e.why
        if pick.line != e.line or (e.col is not None and pick.col != e.col):
            pick.moved_from = e.line
            pick.moved_col = e.col               # an entry with a column keeps one (a dropped column weakens it)
        accepted.append(pick)
    return [f for f in findings if id(f) not in taken] + unused, accepted
