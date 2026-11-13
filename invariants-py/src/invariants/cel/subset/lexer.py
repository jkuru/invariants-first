"""The tokenizer: CEL's number, string, identifier and operator tokens, and the literal ranges."""
from __future__ import annotations

import re
from typing import Any

from ..errors import CelSyntaxError

INT_MIN, INT_MAX = -2**63, 2**63 - 1

# Number literals as in CEL's grammar: a double is digits.digits, digits with an exponent, or .digits
# (1.5, 1e5, 1.5E-3, .5); an int is decimal or hex (0x10); a uint is an int with a u or U suffix.
_EXP = r'[eE][+-]?\d+'
_TOK = re.compile(r'\s*(?:(\d+\.\d+(?:' + _EXP + r')?|\d+' + _EXP + r'|\.\d+(?:' + _EXP + r')?)|'
                  r'(0[xX][0-9a-fA-F]+|\d+)([uU]?)|"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|'
                  r'([A-Za-z_][A-Za-z0-9_]*)|(==|!=|<=|>=|&&|\|\||[-+*/%<>!()\[\]{},.:?]))',
                  re.ASCII)       # digits and spaces are ASCII only, as in CEL's grammar
UINT_MAX = 2**64 - 1

_ESC = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{3}|.)", re.S)
_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "a": "\a"}


def decode_string(raw: str) -> str:
    """Decode the escapes in a CEL string literal body (non-ASCII text is kept as is)."""
    def one(m: re.Match[str]) -> str:
        e = m.group(1)
        if e[0] in "xuU":
            return chr(int(e[1:], 16))
        if len(e) == 3 and e.isdigit():
            return chr(int(e, 8))
        return _SIMPLE.get(e, e)
    return _ESC.sub(one, raw)


def tokenize(src: str) -> list[tuple[str, Any]]:
    """Split rule text into tokens."""
    out: list[tuple[str, Any]] = []
    pos, src = 0, src.strip()
    while pos < len(src):
        m = _TOK.match(src, pos)
        if not m or m.end() == pos:
            raise CelSyntaxError(f"bad character at {pos}: {src[pos:pos + 10]!r}")
        f, i, u, s1, s2, ident, op = m.groups()
        if f:
            x = float(f)
            if x - x != 0.0:        # 1e400: cel-cpp reads Infinity; the library refuses non-finite doubles
                raise CelSyntaxError(f"double literal {f} is out of range (it would be Infinity)")
            out.append(("lit", x))
        elif i:
            n = int(i[2:], 16) if i[:2] in ("0x", "0X") else int(i)
            if u:
                if n > UINT_MAX:
                    raise CelSyntaxError(f"invalid uint literal {i}{u}: outside uint64")
                out.append(("uint", n))
            else:
                out.append(("int", n))  # range-checked by the parser: -9223372036854775808 is valid
        elif s1 is not None or s2 is not None:
            out.append(("lit", decode_string(s1 if s1 is not None else s2)))
        elif ident:
            if ident in ("true", "false", "null"):
                out.append(("lit", {"true": True, "false": False, "null": None}[ident]))
            else:
                out.append(("id", ident))
        else:
            out.append(("op", op))
        pos = m.end()
    return out + [("end", None)]
