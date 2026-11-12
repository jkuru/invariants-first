"""A linear-time regex matcher for the subset backend's matches(): RE2 syntax, no backtracking.

Patterns compile to a small NFA program that runs as a Pike VM: every input position is visited
once with the set of live states, so the time is O(len(text) * len(program)) whatever the pattern.
Each state step is counted; the caller turns the count into CEL cost. Stdlib only.

Supported (as RE2): literals and escapes, `.`, classes `[...]` with ranges, negation, escapes and
`[:alpha:]` names, `\\d \\w \\s` (ASCII, as RE2) and their negations, `\\pL`/`\\p{Lu}` general
categories, `^ $ \\A \\z \\b \\B`, groups `(...)`, `(?:...)`, `(?P<n>...)`, `(?<n>...)`, flags
`(?imsU)` and `(?i:...)`, `|`, and `* + ? {n} {n,} {n,m}` (lazy forms match the same for a search).
Lookaround and backreferences are not RE2 syntax and are refused.
"""
from __future__ import annotations

import unicodedata
from typing import Callable

MAX_REPEAT = 1000            # RE2's limit on {n,m}
MAX_PROGRAM = 5000           # instructions after {n,m} expansion

Pred = Callable[[str], bool]


class RegexError(ValueError):
    """The pattern is not valid (supported) RE2 syntax."""


def _ascii_word(c: str) -> bool:
    return c == "_" or ("0" <= c <= "9") or ("a" <= c <= "z") or ("A" <= c <= "Z")


_PERL: dict[str, Pred] = {
    "d": lambda c: "0" <= c <= "9",
    "s": lambda c: c in "\t\n\f\r ",
    "w": _ascii_word,
}
_POSIX: dict[str, Pred] = {
    "alnum": lambda c: c.isascii() and c.isalnum(), "alpha": lambda c: c.isascii() and c.isalpha(),
    "ascii": lambda c: c.isascii(), "blank": lambda c: c in " \t", "cntrl": lambda c: ord(c) < 32 or c == "\x7f",
    "digit": lambda c: "0" <= c <= "9", "graph": lambda c: "!" <= c <= "~", "lower": lambda c: "a" <= c <= "z",
    "print": lambda c: " " <= c <= "~", "upper": lambda c: "A" <= c <= "Z", "word": _ascii_word,
    "punct": lambda c: ("!" <= c <= "~") and not c.isalnum(), "space": lambda c: c in " \t\n\r\f\v",
    "xdigit": lambda c: c in "0123456789abcdefABCDEF",
}
_SIMPLE_ESC = {"a": "\a", "f": "\f", "t": "\t", "n": "\n", "r": "\r", "v": "\v"}


def _not(p: Pred) -> Pred:
    return lambda c: not p(c)


def _category(name: str) -> Pred:
    if name == "Any":
        return lambda c: True
    if not (1 <= len(name) <= 2 and name[0] in "LMNPSZC"):
        raise RegexError(f"unsupported Unicode class \\p{{{name}}} (general categories only)")
    return lambda c: unicodedata.category(c).startswith(name)


class _Set:
    """A character class: ranges and predicates, maybe negated, maybe case-folded."""

    __slots__ = ("ranges", "preds", "neg", "fold")

    def __init__(self, ranges: list[tuple[str, str]], preds: list[Pred], neg: bool, fold: bool) -> None:
        self.ranges, self.preds, self.neg, self.fold = ranges, preds, neg, fold

    def _has(self, c: str) -> bool:
        return any(lo <= c <= hi for lo, hi in self.ranges) or any(p(c) for p in self.preds)

    def __call__(self, c: str) -> bool:
        hit = self._has(c) or (self.fold and (self._has(c.lower()) or self._has(c.upper())))
        return hit != self.neg


class _Parser:
    """Recursive descent over the pattern; builds a tree of tuples."""

    def __init__(self, src: str) -> None:
        self.s, self.i = src, 0
        self.flags = {"i": False, "s": False, "m": False}

    def more(self) -> bool:
        return self.i < len(self.s)

    def peek(self) -> str:
        return self.s[self.i] if self.more() else ""

    def take(self) -> str:
        if not self.more():
            raise RegexError("unexpected end of pattern")
        c = self.s[self.i]
        self.i += 1
        return c

    def parse(self) -> tuple:
        node = self.alt()
        if self.more():
            raise RegexError(f"unexpected {self.peek()!r} at {self.i}")
        return node

    def alt(self) -> tuple:
        saved = dict(self.flags)
        branches = [self.cat()]
        while self.peek() == "|":
            self.i += 1
            branches.append(self.cat())
        self.flags = saved
        return branches[0] if len(branches) == 1 else ("alt", branches)

    def cat(self) -> tuple:
        items: list[tuple] = []
        while self.more() and self.peek() not in "|)":
            atom = self.atom()
            if atom is None:
                continue
            items.append(self.repeat(atom))
        return ("cat", items)

    def repeat(self, atom: tuple) -> tuple:
        node, quantified = atom, False
        while self.more():
            c, start = self.peek(), self.i
            if c in "*+?":
                self.i += 1
                lo, hi = {"*": (0, None), "+": (1, None), "?": (0, 1)}[c]
            elif c == "{":
                got = self.braces()
                if got is None:
                    return node
                lo, hi = got
            else:
                return node
            if quantified:
                raise RegexError(f"bad repetition operator at {start}")
            if self.peek() == "?":
                self.i += 1                              # lazy: same answer for a yes/no search
            node, quantified = ("rep", node, lo, hi), True
        return node

    def braces(self) -> tuple[int, int | None] | None:
        """{n}, {n,} or {n,m}; None (and no input used) if this { is a literal."""
        j, s = self.i + 1, self.s
        k = j
        while k < len(s) and s[k].isdigit():
            k += 1
        if k == j:
            return None
        lo = int(s[j:k])
        hi: int | None = lo
        if k < len(s) and s[k] == ",":
            m = k + 1
            while m < len(s) and s[m].isdigit():
                m += 1
            hi = int(s[k + 1:m]) if m > k + 1 else None
            k = m
        if k >= len(s) or s[k] != "}":
            return None
        if lo > MAX_REPEAT or (hi is not None and (hi > MAX_REPEAT or hi < lo)):
            raise RegexError(f"bad repetition count in {s[self.i:k + 1]}")
        self.i = k + 1
        return lo, hi

    def atom(self) -> tuple | None:
        c = self.take()
        if c == "(":
            return self.group()
        if c == "[":
            return ("char", self.klass())
        if c == ".":
            dotall = self.flags["s"]
            return ("char", (lambda ch: True) if dotall else (lambda ch: ch != "\n"))
        if c == "^":
            return ("assert", "bol" if self.flags["m"] else "bot")
        if c == "$":
            return ("assert", "eol" if self.flags["m"] else "eot")
        if c in "*+?":
            raise RegexError(f"missing argument to repetition operator {c}")
        if c == "\\":
            return self.escape()
        return self.lit(c)

    def lit(self, c: str) -> tuple:
        if self.flags["i"] and c.lower() != c.upper():
            return ("char", _Set([(c, c)], [], False, True))
        return ("char", _Set([(c, c)], [], False, False))

    def group(self) -> tuple | None:
        if self.peek() != "?":
            return self.close(self.alt())
        self.i += 1
        c = self.peek()
        if c in "=!" or self.s.startswith("<=", self.i) or self.s.startswith("<!", self.i):
            raise RegexError("lookaround is not RE2 syntax")
        if c == "P" or c == "<":
            self.i += 1 if c == "<" else 2 if self.s.startswith("P<", self.i) else 0
            if c == "P" and self.s[self.i - 1] != "<":
                raise RegexError("bad named group")
            end = self.s.find(">", self.i)
            name = self.s[self.i:end] if end > 0 else ""
            if not name or not all(_ascii_word(x) for x in name):
                raise RegexError("bad named group name")
            self.i = end + 1
            return self.close(self.alt())
        on = True
        changed: dict[str, bool] = {}
        while True:
            c = self.take()
            if c in "imsU":
                if c != "U":
                    changed[c] = on
            elif c == "-" and on:
                on = False
            elif c == ":":
                saved = dict(self.flags)
                self.flags.update(changed)
                node = self.alt()
                self.flags = saved
                return self.close(node)
            elif c == ")":
                self.flags.update(changed)
                return None
            else:
                raise RegexError(f"bad flag group near {c!r}")

    def close(self, node: tuple) -> tuple:
        if self.take() != ")":
            raise RegexError("missing )")
        return node

    def escape(self, in_class: bool = False) -> tuple:
        """An escape after a backslash: ('char', pred), ('assert', kind) or ('quote', text)."""
        c = self.take()
        if c in _SIMPLE_ESC:
            return self.lit(_SIMPLE_ESC[c])
        if c.lower() in _PERL:
            p = _PERL[c.lower()]
            return ("char", p if c.islower() else _not(p))
        if c in "pP":
            if self.peek() == "{":
                end = self.s.find("}", self.i)
                if end < 0:
                    raise RegexError("missing } in \\p{...}")
                name, self.i = self.s[self.i + 1:end], end + 1
            else:
                name = self.take()
            neg = name.startswith("^")
            p = _category(name.lstrip("^"))
            return ("char", p if (c == "p") != neg else _not(p))
        if c == "x":
            if self.peek() == "{":
                end = self.s.find("}", self.i)
                digits, self.i = self.s[self.i + 1:end] if end > 0 else "", end + 1
            else:
                digits = self.s[self.i:self.i + 2]
                self.i += 2
            try:
                cp = int(digits, 16)
                ch = chr(cp)
            except (ValueError, OverflowError):
                raise RegexError("bad \\x escape") from None
            return self.lit(ch)
        if c == "0":
            j = self.i
            while j < len(self.s) and j < self.i + 2 and self.s[j] in "01234567":
                j += 1
            ch, self.i = chr(int("0" + self.s[self.i:j], 8)), j
            return self.lit(ch)
        if c.isdigit():
            raise RegexError("backreferences are not RE2 syntax")
        if not in_class:
            if c in "bB":
                return ("assert", "wordb" if c == "b" else "nwordb")
            if c == "A":
                return ("assert", "bot")
            if c == "z":
                return ("assert", "eot")
            if c == "Q":
                end = self.s.find("\\E", self.i)
                text = self.s[self.i:] if end < 0 else self.s[self.i:end]
                self.i = len(self.s) if end < 0 else end + 2
                return ("cat", [self.lit(x) for x in text])
        if c.isascii() and (c.isalnum() or c == "_"):
            raise RegexError(f"invalid escape sequence \\{c}")
        return self.lit(c)

    def klass(self) -> _Set:
        neg = self.peek() == "^"
        if neg:
            self.i += 1
        ranges: list[tuple[str, str]] = []
        preds: list[Pred] = []
        first = True
        while True:
            if not self.more():
                raise RegexError("missing ]")
            c = self.peek()
            if c == "]" and not first:
                self.i += 1
                break
            first = False
            if self.s.startswith("[:", self.i):
                end = self.s.find(":]", self.i + 2)
                name = self.s[self.i + 2:end] if end > 0 else ""
                negp = name.startswith("^")
                p = _POSIX.get(name.lstrip("^"))
                if p is None:
                    raise RegexError(f"bad POSIX class [:{name}:]")
                preds.append(_not(p) if negp else p)
                self.i = end + 2
                continue
            lo = self.class_char(preds)
            if lo is None:
                continue
            if self.peek() == "-" and self.i + 1 < len(self.s) and self.s[self.i + 1] != "]":
                self.i += 1
                hi = self.class_char(preds)
                if hi is None or hi < lo:
                    raise RegexError("bad character class range")
                ranges.append((lo, hi))
            else:
                ranges.append((lo, lo))
        return _Set(ranges, preds, neg, self.flags["i"])

    def class_char(self, preds: list[Pred]) -> str | None:
        """One class member: a char (returned) or an escape class (added to preds, None)."""
        c = self.take()
        if c != "\\":
            return c
        node = self.escape(in_class=True)
        p = node[1]
        if isinstance(p, _Set) and len(p.ranges) == 1 and not p.preds and p.ranges[0][0] == p.ranges[0][1]:
            return p.ranges[0][0]
        preds.append(p)
        return None


# ---- compile to a Pike VM program --------------------------------------------------------------

class _Prog:
    """Instructions: ('char', pred), ('split', a, b), ('jmp', a), ('assert', kind), ('match',)."""

    def __init__(self) -> None:
        self.code: list[list] = []

    def emit(self, *ins: object) -> int:
        if len(self.code) >= MAX_PROGRAM:
            raise RegexError("pattern too large")
        self.code.append(list(ins))
        return len(self.code) - 1

    def gen(self, n: tuple) -> None:
        k = n[0]
        if k in ("char", "assert"):
            self.emit(k, n[1])
        elif k == "cat":
            for x in n[1]:
                self.gen(x)
        elif k == "alt":
            jumps = []
            for x in n[1][:-1]:
                sp = self.emit("split", 0, 0)
                self.code[sp][1] = len(self.code)
                self.gen(x)
                jumps.append(self.emit("jmp", 0))
                self.code[sp][2] = len(self.code)
            self.gen(n[1][-1])
            for j in jumps:
                self.code[j][1] = len(self.code)
        elif k == "rep":
            _, x, lo, hi = n
            for _ in range(lo):
                self.gen(x)
            if hi is None:
                sp = self.emit("split", 0, 0)
                self.code[sp][1] = len(self.code)
                self.gen(x)
                self.emit("jmp", sp)
                self.code[sp][2] = len(self.code)
            else:
                ends = []
                for _ in range(hi - lo):
                    sp = self.emit("split", 0, 0)
                    self.code[sp][1] = len(self.code)
                    ends.append(sp)
                    self.gen(x)
                for sp in ends:
                    self.code[sp][2] = len(self.code)


def compile_pattern(pattern: str) -> list[list]:
    """Compile a RE2 pattern to a program. Raises RegexError."""
    tree = _Parser(pattern).parse()
    prog = _Prog()
    prog.gen(tree)
    prog.emit("match")
    return prog.code


def search(code: list[list], text: str, max_steps: int) -> tuple[bool, int]:
    """(found, steps): is there a match anywhere in text. Raises StepLimit past max_steps."""
    n, steps = len(text), 0
    clist: list[int] = []
    for pos in range(n + 1):
        prev = text[pos - 1] if pos else ""
        cur = text[pos] if pos < n else ""
        seen: set[int] = set()
        nlist: list[int] = []
        work = clist + [0]                       # live threads plus a fresh start here (order is moot for yes/no)
        while work:
            pc = work.pop()
            if pc in seen:
                continue
            seen.add(pc)
            steps += 1
            if steps > max_steps:
                raise StepLimit(steps)
            ins = code[pc]
            op = ins[0]
            if op == "char":
                if cur and ins[1](cur):
                    nlist.append(pc + 1)
            elif op == "match":
                return True, steps
            elif op == "jmp":
                work.append(ins[1])
            elif op == "split":
                work.append(ins[2])
                work.append(ins[1])
            else:
                kind = ins[1]
                if kind == "bot":
                    ok = pos == 0
                elif kind == "eot":
                    ok = pos == n
                elif kind == "bol":
                    ok = pos == 0 or prev == "\n"
                elif kind == "eol":
                    ok = pos == n or cur == "\n"
                else:
                    edge = _ascii_word(prev) if prev else False
                    nxt = _ascii_word(cur) if cur else False
                    ok = (edge != nxt) == (kind == "wordb")
                if ok:
                    work.append(pc + 1)
        clist = nlist
    return False, steps


class StepLimit(Exception):
    """The search used more steps than allowed."""


__all__ = ["compile_pattern", "search", "RegexError", "StepLimit", "MAX_REPEAT", "MAX_PROGRAM"]
