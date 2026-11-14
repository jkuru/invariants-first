"""A linear-time regex matcher for the subset backend's matches(): RE2 syntax, no backtracking.

Patterns compile to a small NFA program that runs as a Pike VM: every input position is visited
once with the set of live states, so the time is O(len(text) * len(program)) whatever the pattern.
Each state step is counted; the caller turns the count into CEL cost. Stdlib only.

Supported (as RE2): literals and escapes, `.`, classes `[...]` with ranges, negation, escapes and
`[:alpha:]` names, `\\d \\w \\s` (ASCII, as RE2) and their negations, `\\pL`/`\\p{Lu}` general
categories, `^ $ \\A \\z \\b \\B`, groups `(...)`, `(?:...)`, `(?P<n>...)`, `(?<n>...)`, flags
`(?imsU)` and `(?i:...)` (under `i` a class is closed under case folding, then negated, as in
RE2), `|`, and `* + ? {n} {n,} {n,m}` (lazy forms match the same for a search).
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


# ---- the pattern limit, shared by every backend ------------------------------------------------
# A pattern a rule matches with (a constant at load; one from the request data on every call) must be
# at most PATTERN_CHARS characters, nest groups at most PATTERN_DEPTH deep, and its program, with
# every {n,m} expanded (pattern_size), at most MAX_PROGRAM instructions. Past it the pattern is
# refused before any engine compiles it, so no pattern from the data can buy compile or match time
# (cel-cpp compiles a pattern from the data on every call; '[a-z]{1000}' * 50 took 0.8 s each).
PATTERN_CHARS = 4096
PATTERN_DEPTH = 64
PATTERN_LIMIT = (f"matches(): the pattern is over the pattern limit ({PATTERN_CHARS:,} characters, "
                 f"{PATTERN_DEPTH} nested groups, {MAX_PROGRAM:,} instructions with repeats expanded)")


class PatternLimit(RegexError):
    """The pattern is over the pattern limit (PATTERN_LIMIT); its str() is the reason."""


def _brace(p: str, i: int) -> tuple[int, int | None, int] | None:
    """(lo, hi, end) for a repeat count {n}, {n,} or {n,m} at p[i] == '{', else None (a literal '{')."""
    j = k = i + 1
    while k < len(p) and p[k].isdigit() and k - j < 8:
        k += 1
    if k == j:
        return None
    lo, hi = int(p[j:k]), None
    hi = lo
    if k < len(p) and p[k] == ",":
        m = k + 1
        while m < len(p) and p[m].isdigit() and m - k < 8:
            m += 1
        hi = int(p[k + 1:m]) if m > k + 1 else None
        k = m
    if k >= len(p) or p[k] != "}":
        return None
    return lo, hi, k + 1


def pattern_size(p: str) -> int:
    """PatternLimit unless p is within the pattern limit; else an upper bound on the instructions its
    program has with every {n,m} expanded (at least what compile_pattern emits). Linear in len(p),
    syntax-agnostic: a pattern it passes may still be invalid RE2 (the engine says so)."""
    if len(p) > PATTERN_CHARS:
        raise PatternLimit(f"over the pattern limit: more than {PATTERN_CHARS:,} characters")
    stack: list[list[int]] = [[0, 0, 0]]      # per open group: [size so far, last atom's size, alternatives]
    i, n = 0, len(p)

    def atom(size: int) -> None:
        f = stack[-1]
        f[0] += size
        f[1] = size

    def grow(new: int) -> None:
        f = stack[-1]
        f[0] += new - f[1]
        f[1] = new
        if f[0] > MAX_PROGRAM:
            raise PatternLimit("over the pattern limit: too large with repeats expanded")
    while i < n:
        c = p[i]
        if c == "\\":
            nxt = p[i + 1:i + 2]
            if nxt == "Q":                          # \Q...\E: each character a literal
                end = p.find("\\E", i + 2)
                end = n if end < 0 else end
                if end - i - 2 > 1:                 # a quantifier after it repeats its last character only
                    atom(end - i - 3)
                if end > i + 2:                     # an empty \Q\E is nothing: a quantifier takes the atom before
                    atom(1)
                i = end + 2
                continue
            i += 2
            if nxt in ("p", "P", "x") and p[i:i + 1] == "{":
                close = p.find("}", i)
                i = n if close < 0 else close + 1
            atom(1)
        elif c == "[":                              # a class is one instruction
            i += 1
            if p[i:i + 1] == "^":
                i += 1
            if p[i:i + 1] == "]":
                i += 1
            while i < n and p[i] != "]":
                if p[i] == "\\":
                    i += 1
                elif p.startswith("[:", i):
                    close = p.find(":]", i + 2)
                    i = i if close < 0 else close + 1
                i += 1
            i += 1
            atom(1)
        elif c == "(":
            if len(stack) > PATTERN_DEPTH:
                raise PatternLimit(f"over the pattern limit: groups nested more than {PATTERN_DEPTH} deep")
            stack.append([0, 0, 0])
            i += 1
        elif c == ")" and len(stack) > 1:
            size, _, alts = stack.pop()
            atom(size + 2 * alts)
            grow(stack[-1][1])
            i += 1
        elif c == "|":
            f = stack[-1]
            f[2] += 1
            f[1] = 0
            i += 1
        elif c in "*+?":
            last = stack[-1][1]
            grow(last + 2 if c == "*" else 2 * last + 2 if c == "+" else last + 1)
            i += 2 if p[i + 1:i + 2] == "?" else 1
        elif c == "{" and (got := _brace(p, i)) is not None:
            lo, hi, i = got
            last = stack[-1][1]
            lo, hi = min(lo, MAX_REPEAT), None if hi is None else min(hi, MAX_REPEAT)
            grow(lo * last + last + 2 if hi is None else max(hi, lo) * (last + 1))
            i += 1 if p[i:i + 1] == "?" else 0
        else:
            atom(1)
            i += 1
        if stack[-1][0] > MAX_PROGRAM:
            raise PatternLimit("over the pattern limit: too large with repeats expanded")
    total = 1
    for size, _, alts in stack:
        total += size + 2 * alts
    if total > MAX_PROGRAM:
        raise PatternLimit("over the pattern limit: too large with repeats expanded")
    return total


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


# ---- (?i): RE2's simple case folding ------------------------------------------------------------
# RE2 folds case one character at a time: two characters match under (?i) when they share a simple
# (one-character) case folding, so 's', 'S' and 'ſ' match each other, and 'K' (Kelvin) matches 'k',
# but 'ß' never matches 'SS' and 'ı' does not match 'I'. str.upper()/lower() are not that: 'ß'.upper()
# is 'SS' and 'ﬁ'.upper() is 'FI', which a range such as [A-Z] would take. The orbits are built once,
# on the first (?i) pattern, from the characters up to _FOLD_TOP (the last cased one is below it).
_FOLD_TOP = 0x1F000
_ORBITS: dict[str, tuple[str, ...]] | None = None


def _simple_fold(c: str) -> str:
    """c's simple case folding: casefold() where that is one character, else its one-character lower
    case (U+1E9E 'ẞ' -> 'ß'), else c itself (a character whose only folding is several characters)."""
    f = c.casefold()
    if len(f) == 1:
        return f
    lo = c.lower()
    return lo if len(lo) == 1 else c


def _orbits() -> dict[str, tuple[str, ...]]:
    global _ORBITS
    if _ORBITS is None:
        groups: dict[str, list[str]] = {}
        for i in range(_FOLD_TOP):
            c = chr(i)
            f = _simple_fold(c)
            if f != c:
                groups.setdefault(f, [f]).append(c)
        _ORBITS = {c: tuple(x for x in g if x != c) for g in groups.values() for c in g}
    return _ORBITS


def fold_orbit(c: str) -> tuple[str, ...]:
    """The other characters c matches under (?i) (RE2's case-folding orbit), or () if none."""
    return _orbits().get(c, ())


def _group(p: Pred, negated: bool, fold: bool) -> Pred:
    """A class such as \\w, \\pL or [:upper:] (or its negation) as RE2 builds it: under (?i) the class
    is closed under case folding first and negated after, so (?i)\\w takes 'K' (Kelvin, which folds to
    'k') and (?i)\\W refuses it, and (?i)\\p{Lu} takes 'a'."""
    if fold:
        q = p
        p = lambda c: q(c) or any(q(x) for x in fold_orbit(c))      # noqa: E731
    return _not(p) if negated else p


class _Set:
    """A character class: ranges (case-folded under (?i)) and predicates (each already as _group
    built it, so not folded again), the whole maybe negated."""

    __slots__ = ("ranges", "preds", "neg", "fold")

    def __init__(self, ranges: list[tuple[str, str]], preds: list[Pred], neg: bool, fold: bool) -> None:
        self.ranges, self.preds, self.neg, self.fold = ranges, preds, neg, fold

    def _has(self, c: str) -> bool:
        return any(lo <= c <= hi for lo, hi in self.ranges) or any(p(c) for p in self.preds)

    def __call__(self, c: str) -> bool:
        hit = self._has(c) or (self.fold and any(lo <= x <= hi for x in fold_orbit(c) for lo, hi in self.ranges))
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
            if atom[0] == "quote":                       # \Q...\E: literals, one at a time (as RE2)
                lits = atom[1]
                if not lits:                             # nothing: a quantifier after it takes the item before
                    if items and self.more() and self.peek() in "*+?{":
                        # RE2 refuses only adjacent operators (a**): a*\Q\E+ repeats a* again
                        items.append(self.repeat(items.pop()))
                    continue                             # with nothing before, atom() refuses the quantifier
                items.extend(lits[:-1])
                atom = lits[-1]
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
        if c == "{":
            self.i -= 1
            if self.braces() is not None:              # {2} with nothing before it: an error in RE2
                raise RegexError("missing argument to repetition operator {")
            self.i += 1
        if c == "\\":
            return self.escape()
        return self.lit(c)

    def lit(self, c: str) -> tuple:
        if self.flags["i"] and fold_orbit(c):
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
            return ("char", _group(_PERL[c.lower()], c.isupper(), self.flags["i"]))
        if c in "pP":
            if self.peek() == "{":
                end = self.s.find("}", self.i)
                if end < 0:
                    raise RegexError("missing } in \\p{...}")
                name, self.i = self.s[self.i + 1:end], end + 1
            else:
                name = self.take()
            neg = name.startswith("^")
            return ("char", _group(_category(name.lstrip("^")), (c == "P") != neg, self.flags["i"]))
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
        if c in "01234567":
            # RE2's octal escape: \0 and up to two more octal digits; \1 to \7 only with at least one
            # more (\101 is A, \12 a newline); a lone \1 to \7 would be a backreference
            if c != "0" and not (self.more() and self.peek() in "01234567"):
                raise RegexError("backreferences are not RE2 syntax")
            j = self.i
            while j < len(self.s) and j < self.i + 2 and self.s[j] in "01234567":
                j += 1
            ch, self.i = chr(int(c + self.s[self.i:j], 8)), j
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
                return ("quote", [self.lit(x) for x in text])
        if not c.isascii() or c.isalnum():           # RE2 takes an escaped ASCII punctuation mark (\_ too)
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
                preds.append(_group(p, negp, self.flags["i"]))
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
    """Compile a RE2 pattern to a program. Raises RegexError (PatternLimit over the pattern limit)."""
    pattern_size(pattern)
    tree = _Parser(pattern).parse()
    prog = _Prog()
    prog.gen(tree)
    prog.emit("match")
    return prog.code


def search(code: list[list], text: str, max_steps: int) -> tuple[bool, int]:
    """(found, steps): is there a match anywhere in text. Raises StepLimit past max_steps."""
    n, steps = len(text), 0
    clist: list[int] = []
    mid_checked = False
    for pos in range(n + 1):
        prev = text[pos - 1] if pos else ""
        cur = text[pos] if pos < n else ""
        if not mid_checked and cur > "\x7f":
            mid_checked = True                   # once: every position inside a character is alike
            found, steps = _mid_char(code, steps, max_steps)
            if found:
                return True, steps
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


def _mid_char(code: list[list], steps: int, max_steps: int) -> tuple[bool, int]:
    """Whether a match starts inside a multi-byte character, as RE2 finds it: RE2 searches UTF-8 bytes,
    so its unanchored search also starts between the bytes of one character. There both neighbours
    are non-word bytes, so \\B holds and \\b does not; no ^ $ \\A \\z holds, and nothing consumes a
    continuation byte: only a pattern that can reach its end through \\B alone matches there
    ('aéa'.matches('\\B') is true)."""
    seen: set[int] = set()
    work = [0]
    while work:
        pc = work.pop()
        if pc in seen:
            continue
        seen.add(pc)
        steps += 1
        if steps > max_steps:
            raise StepLimit(steps)
        ins = code[pc]
        if ins[0] == "match":
            return True, steps
        if ins[0] == "jmp":
            work.append(ins[1])
        elif ins[0] == "split":
            work += [ins[2], ins[1]]
        elif ins[0] == "assert" and ins[1] == "nwordb":
            work.append(pc + 1)
    return False, steps


class StepLimit(Exception):
    """The search used more steps than allowed."""


__all__ = ["compile_pattern", "search", "RegexError", "StepLimit", "MAX_REPEAT", "MAX_PROGRAM", "PatternLimit",
           "pattern_size", "PATTERN_LIMIT", "PATTERN_CHARS", "PATTERN_DEPTH"]
