"""The constraints a SQL schema file declares, for the `sql` contract type.

`inv lock` pins a `{type: sql, ref: schema.sql}` contract as the sorted list of its constraints:
every CHECK, NOT NULL, UNIQUE and PRIMARY KEY in CREATE TABLE statements and in
`ALTER TABLE ... ADD [CONSTRAINT name] CHECK|UNIQUE (...)`. A constraint that disappears or changes is
a weakening (the database now accepts rows it refused); a new one is not. Everything else in the file
(indexes, comments, column order, other DDL) is not pinned, so it can change freely.

This is a small reader for the common DDL shapes, not a SQL parser: a statement it cannot read is
skipped, and `inv init` says which files it found constraints in so a person can check the list.
"""
from __future__ import annotations

import re

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_CREATE = re.compile(r"\bcreate\s+(?:temp(?:orary)?\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.\"`\[\]]+)\s*\(",
                     re.I)
_ALTER = re.compile(r"\balter\s+table\s+(?:only\s+)?(?:if\s+exists\s+)?([\w.\"`\[\]]+)\s+add\s+"
                    r"(?:constraint\s+[\w\"`\[\]]+\s+)?(check|unique|primary\s+key)\s*\(", re.I)
_NAMED = re.compile(r"^constraint\s+[\w\"`\[\]]+\s+", re.I)
_TABLE_LEVEL = re.compile(r"^(check|unique|primary\s+key)\s*\(", re.I)
_COLUMN_CHECK = re.compile(r"\bcheck\s*\(", re.I)


def _name(raw: str) -> str:
    return raw.strip().strip('"`[]').replace('"', "").replace("`", "").lower()


def _squash(text: str) -> str:
    return " ".join(text.split())


def _balanced(text: str, start: int) -> tuple[str, int] | None:
    """The text inside the parenthesis opened just before `start`, and the index after its close."""
    depth, i, quote = 1, start, ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return None


def _split_top(body: str) -> list[str]:
    parts, depth, cur, quote = [], 0, [], ""
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _kind(word: str) -> str:
    return " ".join(word.upper().split())


def constraints(text: str) -> list[str]:
    """Every constraint in a SQL schema, as sorted normalized strings ("refunds: CHECK (amount > 0)",
    "refunds.amount: NOT NULL")."""
    sql = _COMMENT.sub(" ", text)
    out: set[str] = set()
    for m in _CREATE.finditer(sql):
        table = _name(m.group(1))
        got = _balanced(sql, m.end())
        if got is None:
            continue
        for item in _split_top(got[0]):
            item = _NAMED.sub("", item)
            tl = _TABLE_LEVEL.match(item)
            if tl:
                inner = _balanced(item, tl.end())
                if inner is not None:
                    out.add(f"{table}: {_kind(tl.group(1))} ({_squash(inner[0])})")
                continue
            if item.lower().startswith(("foreign key", "exclude", "like ")):
                continue
            col, _, rest = item.partition(" ")
            col, low = _name(col), rest.lower()
            if re.search(r"\bnot\s+null\b", low):
                out.add(f"{table}.{col}: NOT NULL")
            if re.search(r"\bprimary\s+key\b", low):
                out.add(f"{table}.{col}: PRIMARY KEY")
            elif re.search(r"\bunique\b", low):
                out.add(f"{table}.{col}: UNIQUE")
            for cm in _COLUMN_CHECK.finditer(rest):
                inner = _balanced(rest, cm.end())
                if inner is not None:
                    out.add(f"{table}.{col}: CHECK ({_squash(inner[0])})")
    for m in _ALTER.finditer(sql):
        inner = _balanced(sql, m.end())
        if inner is not None:
            out.add(f"{_name(m.group(1))}: {_kind(m.group(2))} ({_squash(inner[0])})")
    return sorted(out)
