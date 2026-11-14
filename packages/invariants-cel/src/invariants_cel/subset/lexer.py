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
                  r'(0[xX][0-9a-fA-F]+|\d+)([uU]?)|"((?:[^"\\\n\r]|\\.)*)"|\'((?:[^\'\\\n\r]|\\.)*)\'|'
                  # a raw string: its text as written; as in cel-cpp, a backslash may not precede the quote
                  r'[rR]"((?:[^"\\\n\r]|\\[^"\n\r])*)"|[rR]\'((?:[^\'\\\n\r]|\\[^\'\n\r])*)\'|'
                  r'([A-Za-z_][A-Za-z0-9_]*)|(==|!=|<=|>=|&&|\|\||[-+*/%<>!()\[\]{},.:?]))',
                  re.ASCII)       # digits and spaces are ASCII only, as in CEL's grammar
UINT_MAX = 2**64 - 1

# CEL's escapes: \a \b \f \n \r \t \v \\ \' \" \` \?, \xHH, \uHHHH, \UHHHHHHHH and octal \[0-3][0-7][0-7];
# any other backslash is a syntax error on every backend (cel-cpp: "Illegal escape sequence")
_ESC = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-3][0-7]{2}|.)", re.S)
_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "a": "\a",
           "\\": "\\", "'": "'", '"': '"', "`": "`", "?": "?"}


_SURROGATE = re.compile("[\ud800-\udfff]")


def decode_string(raw: str) -> str:
    """Decode the escapes in a CEL string literal body (non-ASCII text is kept as is). A literal
    holding a surrogate code point (U+D800..U+DFFF: '\\ud800' or the character itself) or an escape
    past U+10FFFF is not text: CelSyntaxError, as cel-cpp refuses it."""
    def one(m: re.Match[str]) -> str:
        e = m.group(1)
        if e[0] in "xuU" and len(e) > 1:
            n = int(e[1:], 16)
            if n > 0x10FFFF:
                raise CelSyntaxError(f"invalid string literal: \\{e} is past U+10FFFF")
            return chr(n)
        if len(e) == 3 and e.isdigit():
            return chr(int(e, 8))
        if e not in _SIMPLE:
            raise CelSyntaxError(f"invalid string literal: \\{e} is not a CEL escape (CEL has \\n, \\t, \\\\, \\', "
                                 f"\\\", \\xHH, \\uHHHH, octal \\ooo, ...); for a regex such as \\d+ write a raw "
                                 f"string r'\\d+' or double the backslash: '\\\\d+'")
        return _SIMPLE[e]
    out = _ESC.sub(one, raw)
    if not out.isascii() and _SURROGATE.search(out):
        raise CelSyntaxError("invalid string literal: it holds a surrogate code point (U+D800 to U+DFFF), "
                             "which is not valid Unicode text")
    return out


def tokenize(src: str) -> list[tuple[str, Any]]:
    """Split rule text into tokens."""
    out: list[tuple[str, Any]] = []
    pos, src = 0, src.strip()
    while pos < len(src):
        m = _TOK.match(src, pos)
        if not m or m.end() == pos:
            raise CelSyntaxError(f"bad character at {pos}: {src[pos:pos + 10]!r}")
        f, i, u, s1, s2, r1, r2, ident, op = m.groups()
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
        elif r1 is not None or r2 is not None:
            raw = r1 if r1 is not None else r2
            if not raw.isascii() and _SURROGATE.search(raw):
                raise CelSyntaxError("invalid string literal: it holds a surrogate code point (U+D800 to U+DFFF), "
                                     "which is not valid Unicode text")
            out.append(("lit", raw))
        elif ident:
            if ident in ("true", "false", "null"):
                out.append(("lit", {"true": True, "false": False, "null": None}[ident]))
            else:
                out.append(("id", ident))
        else:
            out.append(("op", op))
        pos = m.end()
    return out + [("end", None)]
