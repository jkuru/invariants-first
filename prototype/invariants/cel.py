"""A small, safe CEL *subset* evaluator (fallback when no real CEL engine is installed).

NOT full CEL. No eval/exec: a tokenizer, a recursive-descent parser to tuples, and a
tree-walking evaluator with a cost budget. Supported: int/double/string/bool/null literals,
lists, maps, field access, indexing, ! - * / % + -, comparisons, in, && || (CEL's
commutative error absorption), ?:, has(e.f), size(), contains/startsWith/endsWith, the
macros all/exists/exists_one/map/filter, and the guide's fixed extensions inv.sum,
inv.zip, inv.unique_by. No static type-checker: load checks syntax and free names only.
"""
import re

class CelSyntaxError(Exception): """Rule text does not parse: a load-time (build) failure."""
class CelError(Exception): """Evaluation error: becomes outcome rule_error, never violation."""

_TOK = re.compile(r'\s*(?:(\d+\.\d+)|(\d+)|"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|'
                  r'([A-Za-z_][A-Za-z0-9_]*)|(==|!=|<=|>=|&&|\|\||[-+*/%<>!()\[\]{},.:?]))')
_MACROS = {"all", "exists", "exists_one", "map", "filter"}

def tokenize(src):
    out, pos, src = [], 0, src.strip()
    while pos < len(src):
        m = _TOK.match(src, pos)
        if not m or m.end() == pos:
            raise CelSyntaxError(f"bad character at {pos}: {src[pos:pos + 10]!r}")
        f, i, s1, s2, ident, op = m.groups()
        if f: out.append(("lit", float(f)))
        elif i: out.append(("lit", int(i)))
        elif s1 is not None or s2 is not None:
            out.append(("lit", (s1 if s1 is not None else s2).encode().decode("unicode_escape")))
        elif ident: out.append(("lit", {"true": True, "false": False, "null": None}[ident])
                               if ident in ("true", "false", "null") else ("id", ident))
        else: out.append(("op", op))
        pos = m.end()
    return out + [("end", None)]

class _Parser:
    def __init__(self, src): self.t, self.i = tokenize(src), 0
    def peek(self, v=None): k, x = self.t[self.i]; return (k == "op" and x == v) if v else (k, x)
    def eat(self, v):
        if not self.peek(v): raise CelSyntaxError(f"expected {v!r}, got {self.t[self.i][1]!r}")
        self.i += 1
    def parse(self):
        e = self.expr()
        if self.t[self.i][0] != "end": raise CelSyntaxError(f"trailing input at {self.t[self.i][1]!r}")
        return e
    def expr(self):
        c = self.binary(0)
        if self.peek("?"):
            self.eat("?"); a = self.expr(); self.eat(":"); return ("cond", c, a, self.expr())
        return c
    _LEVELS = [("||",), ("&&",), ("==", "!=", "<", "<=", ">", ">=", "in"), ("+", "-"), ("*", "/", "%")]
    def binary(self, lvl):
        if lvl == len(self._LEVELS): return self.unary()
        left = self.binary(lvl + 1)
        while True:
            k, x = self.peek()
            op = x if k == "op" else ("in" if (k, x) == ("id", "in") else None)
            if op not in self._LEVELS[lvl]: return left
            self.i += 1
            left = ("bin", op, left, self.binary(lvl + 1))
    def unary(self):
        if self.peek("!"): self.i += 1; return ("un", "!", self.unary())
        if self.peek("-"): self.i += 1; return ("un", "-", self.unary())
        return self.member()
    def args(self, close):
        out = []
        while not self.peek(close):
            out.append(self.expr())
            if not self.peek(close): self.eat(",")
        self.eat(close); return out
    def member(self):
        e = self.primary()
        while True:
            if self.peek("."):
                self.i += 1; k, name = self.peek()
                if k != "id": raise CelSyntaxError("expected field name after '.'")
                self.i += 1
                if self.peek("("):
                    self.i += 1; a = self.args(")")
                    e = ("call", "inv." + name, None, a) if e == ("id", "inv") else ("call", name, e, a)
                    if name in _MACROS and (len(a) != 2 or a[0][0] != "id"):
                        raise CelSyntaxError(f"{name}() needs (var, expr)")
                else: e = ("sel", e, name)
            elif self.peek("["): self.i += 1; ix = self.expr(); self.eat("]"); e = ("idx", e, ix)
            else: return e
    def primary(self):
        k, x = self.peek(); self.i += 1
        if k == "lit": return ("lit", x)
        if k == "id":
            if self.peek("("):
                self.i += 1; a = self.args(")")
                if x == "has" and (len(a) != 1 or a[0][0] != "sel"):
                    raise CelSyntaxError("has() takes a field selection, e.g. has(a.b)")
                return ("call", x, None, a)
            return ("id", x)
        if (k, x) == ("op", "("): e = self.expr(); self.eat(")"); return e
        if (k, x) == ("op", "["): return ("list", self.args("]"))
        if (k, x) == ("op", "{"):
            items = []
            while not self.peek("}"):
                key = self.expr(); self.eat(":"); items.append((key, self.expr()))
                if not self.peek("}"): self.eat(",")
            self.eat("}"); return ("map", items)
        raise CelSyntaxError(f"unexpected {x!r}")

def compile(src):
    return _Parser(src).parse()

def _kids(n):
    for c in n[1:]:
        for x in (c if isinstance(c, list) else [c] if isinstance(c, tuple) else []):
            yield from (x if isinstance(x[0], tuple) else [x])   # map items are (key, value) pairs

def free_names(n, bound=frozenset()):
    """Identifiers an expression reads (for the load-time check against bind names)."""
    if n[0] == "id": return set() if n[1] in bound else {n[1]}
    if n[0] == "call" and (n[1] in _MACROS or n[1] == "inv.unique_by"):
        tgt, (var, body) = (n[2], n[3]) if n[2] is not None else (n[3][0], n[3][1:])
        return free_names(tgt, bound) | free_names(body, bound | {var[1]})
    return set().union(set(), *(free_names(c, bound) for c in _kids(n)))

def _num(v): return isinstance(v, (int, float)) and not isinstance(v, bool)
def _ty(v): return "num" if _num(v) else type(v).__name__

def _eq(a, b):
    if _num(a) and _num(b): return a == b
    if _ty(a) != _ty(b): return False            # CEL heterogeneous equality is false, not error
    if isinstance(a, (list, tuple)): return len(a) == len(b) and all(map(_eq, a, b))
    return a == b

def _select(o, f, test=False):
    if isinstance(o, dict): ok, v = f in o, o.get(f)
    elif o is not None and not f.startswith("_") and hasattr(o, f) and not callable(getattr(o, f)):
        ok, v = True, getattr(o, f)
    else: ok, v = False, None
    if test: return ok and v is not None
    if not ok: raise CelError(f"no such field {f!r} on {type(o).__name__}")
    return v

def _arith(op, a, b):
    if op == "+" and isinstance(a, str) and isinstance(b, str): return a + b
    if op == "+" and isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)): return list(a) + list(b)
    if not (_num(a) and _num(b)) or type(a) is not type(b):
        raise CelError(f"no such overload: {_ty(a)} {op} {_ty(b)}")
    if op in "/%" and b == 0: raise CelError("division by zero")
    if op == "/" and isinstance(a, int): q = abs(a) // abs(b); return q if (a >= 0) == (b > 0) else -q
    if op == "%" and isinstance(a, int): return a - b * _arith("/", a, b)
    return a + b if op == "+" else a - b if op == "-" else a * b if op == "*" else a / b if op == "/" else a % b

def _bool(v, what):
    if not isinstance(v, bool): raise CelError(f"{what} needs bool, got {_ty(v)}")
    return v

def evaluate(n, env, budget=None):
    budget = budget if budget is not None else [10_000]
    budget[0] -= 1
    if budget[0] < 0: raise CelError("cost limit exceeded")
    ev = lambda x, e=env: evaluate(x, e, budget)
    k = n[0]
    if k == "lit": return n[1]
    if k == "id":
        if n[1] not in env: raise CelError(f"undeclared reference {n[1]!r}")
        return env[n[1]]
    if k == "list": return [ev(x) for x in n[1]]
    if k == "map": return {ev(a): ev(b) for a, b in n[1]}
    if k == "sel": return _select(ev(n[1]), n[2])
    if k == "idx":
        o, i = ev(n[1]), ev(n[2])
        if isinstance(o, dict):
            if i not in o: raise CelError(f"no such key {i!r}")
            return o[i]
        if not isinstance(o, (list, tuple)) or not isinstance(i, int) or isinstance(i, bool):
            raise CelError(f"cannot index {_ty(o)} with {_ty(i)}")
        if not 0 <= i < len(o): raise CelError(f"index {i} out of range")
        return o[i]
    if k == "cond": return ev(n[2]) if _bool(ev(n[1]), "?:") else ev(n[3])
    if k == "un":
        v = ev(n[2])
        if n[1] == "!": return not _bool(v, "!")
        if not _num(v): raise CelError(f"no such overload: -{_ty(v)}")
        return -v
    if k == "bin":
        op = n[1]
        if op in ("&&", "||"):                     # CEL: error absorbed if the other side decides
            res = []
            for side in (n[2], n[3]):
                try: res.append(_bool(ev(side), op))
                except CelError as e: res.append(e)
            decider = op == "||"
            if decider in res: return decider
            for r in res:
                if isinstance(r, CelError): raise r
            return not decider
        a, b = ev(n[2]), ev(n[3])
        if op == "==": return _eq(a, b)
        if op == "!=": return not _eq(a, b)
        if op == "in":
            if isinstance(b, (list, tuple)): return any(_eq(a, x) for x in b)
            if isinstance(b, dict): return a in b
            raise CelError(f"no such overload: in {_ty(b)}")
        if op in ("<", "<=", ">", ">="):
            if not ((_num(a) and _num(b)) or _ty(a) == _ty(b) in ("str", "bool")):
                raise CelError(f"no such overload: {_ty(a)} {op} {_ty(b)}")
            return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        return _arith(op, a, b)
    if k == "call": return _call(n[1], n[2], n[3], env, budget)
    raise CelError(f"unknown node {k}")

def _iter(v, name):
    if isinstance(v, dict): return list(v)
    if isinstance(v, (list, tuple)): return list(v)
    raise CelError(f"{name}() on {_ty(v)}")

def _call(name, tgt, args, env, budget):
    ev = lambda x, e=env: evaluate(x, e, budget)
    if name in _MACROS or name == "inv.unique_by":
        if name == "inv.unique_by": tgt, args = args[0], args[1:]
        items, var, body = _iter(ev(tgt), name), args[0][1], args[1]
        vals = [ev(body, {**env, var: x}) for x in items] if name in ("map", "inv.unique_by") else None
        if name == "map": return vals
        if name == "inv.unique_by":
            keys = []
            for v in vals:
                if any(_eq(v, k) for k in keys): return False
                keys.append(v)
            return True
        hits = [_bool(ev(body, {**env, var: x}), name) for x in items]
        return {"all": all(hits), "exists": any(hits), "exists_one": sum(hits) == 1,
                "filter": [x for x, h in zip(items, hits) if h]}[name]
    if name == "has": return _select(ev(args[0][1]), args[0][2], test=True)
    vals = ([ev(tgt)] if tgt is not None else []) + [ev(a) for a in args]
    if name == "size":
        if len(vals) != 1 or not isinstance(vals[0], (str, list, tuple, dict)):
            raise CelError("size() needs one string, list or map")
        return len(vals[0])
    if name in ("contains", "startsWith", "endsWith"):
        if len(vals) != 2 or not all(isinstance(v, str) for v in vals): raise CelError(f"{name}() needs strings")
        s, t = vals
        return {"contains": t in s, "startsWith": s.startswith(t), "endsWith": s.endswith(t)}[name]
    if name == "inv.sum":
        if len(vals) != 1 or not isinstance(vals[0], (list, tuple)) or not all(_num(v) for v in vals[0]):
            raise CelError("inv.sum() needs a list of numbers")
        return sum(vals[0])
    if name == "inv.zip":
        if len(vals) != 2 or not all(isinstance(v, (list, tuple)) for v in vals):
            raise CelError("inv.zip() needs two lists")
        if len(vals[0]) != len(vals[1]):        # prototype choice; the guide does not say (README)
            raise CelError(f"inv.zip(): length mismatch {len(vals[0])} vs {len(vals[1])}")
        return [[a, b] for a, b in zip(*vals)]
    raise CelError(f"unknown function {name}()")

__all__ = ["compile", "evaluate", "free_names", "CelError", "CelSyntaxError"]
