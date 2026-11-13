"""Raw SQL write statements found in a string (`inv init` reads them; the scan's checks never do)."""
from __future__ import annotations

import re

from .contracts import SqlWrite

# a table reference: optional schema qualifier, optional quotes, and a name that may hold placeholders
_TABLE = r"""(?:["`\[]?\w+["`\]]?\.)?["`\[]?((?:\w|\{\w*\}|%(?:\(\w+\))?s)+|\?|:\w+)["`\]]?"""
_SQL_FORMS: tuple[tuple[str, frozenset[str], re.Pattern[str]], ...] = tuple(
    (verb, frozenset(ops), re.compile(pat + _TABLE + tail, re.IGNORECASE)) for verb, ops, pat, tail in (
        ("INSERT", {"insert"},
         r"\bINSERT(?:\s+(?:OR\s+\w+|IGNORE|LOW_PRIORITY|DELAYED|HIGH_PRIORITY))*(?:\s+INTO)?\s+", ""),
        ("REPLACE", {"insert"}, r"\bREPLACE(?:\s+(?:LOW_PRIORITY|DELAYED))?\s+INTO\s+", ""),
        ("MERGE", {"insert", "update", "delete"}, r"\bMERGE\s+(?:INTO\s+)?", ""),
        ("UPDATE", {"update"}, r"\bUPDATE(?:\s+(?:OR\s+\w+|ONLY|LOW_PRIORITY|IGNORE))*\s+",
         r"(?:\s+(?:AS\s+)?(?!SET\b)\w+)?\s+SET\b"),
        ("DELETE", {"delete"}, r"\bDELETE(?:\s+(?:LOW_PRIORITY|QUICK|IGNORE))*\s+FROM(?:\s+ONLY)?\s+", ""),
        ("TRUNCATE", {"delete"}, r"\bTRUNCATE(?:\s+TABLE)?(?:\s+ONLY)?\s+", ""),
        ("DROP", {"ddl"}, r"\bDROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+", ""),
        ("ALTER", {"ddl"}, r"\bALTER\s+TABLE(?:\s+IF\s+EXISTS)?(?:\s+ONLY)?\s+", ""),
    ))
_SQL_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)
_KEYWORDS = frozenset({"into", "from", "table", "only", "set", "if", "select", "values", "or", "using"})


def sql_writes(text: str) -> list[SqlWrite]:
    """The write statements in a string, after SQL comments are removed. A table name holding a
    placeholder ({}, {name}, {0}, %s, %(name)s, ?, :name) is computed (table None)."""
    if not text or len(text) < 6:
        return []
    clean = _SQL_COMMENT.sub(" ", text)
    out: list[SqlWrite] = []
    seen: set[tuple[str, str | None, int]] = set()
    for verb, ops, rx in _SQL_FORMS:
        for m in rx.finditer(clean):
            name = m.group(1)
            computed = name == "?" or name.startswith(":") or "{" in name or "%" in name
            if not computed and name.lower() in _KEYWORDS:
                continue
            table = None if computed else name.lower()
            key = (",".join(sorted(ops)), table, m.start(1))
            if key in seen:
                continue
            seen.add(key)
            out.append(SqlWrite(verb, ops, table))
    return out
