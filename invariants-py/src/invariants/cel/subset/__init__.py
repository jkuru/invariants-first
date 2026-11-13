"""A small, safe CEL subset evaluator (the stdlib fallback backend).

Not full CEL. No eval/exec: a tokenizer, a recursive-descent parser to tuples, and a
tree-walking evaluator with a cost budget. Supported: int/double/string/bool/null literals (CEL's
number grammar: hex 0x10, exponents 1e5 and .5, int literals range-checked to int64 at compile
time; a uint literal 5u parses as uint(5), which only cel-expr evaluates),
lists, maps, field access, indexing, ! - * / % + -, comparisons, in, && || (CEL's
commutative error absorption, also in all/exists), ?:, has(e.f), size(), contains/startsWith/endsWith,
matches, the conversions int(), double(), string(), bool(), dyn(), timestamp() and duration(),
timestamp and duration values (comparison, arithmetic, the getters), the macros
all/exists/exists_one/map/filter, and the fixed extensions inv.sum, inv.zip, inv.unique_by.
FUNCTIONS lists every function name it has; the loader refuses a clause that calls another.
Reading a NaN or +-Infinity is an evaluation error unless non-finite numbers are allowed.

Modules: lexer (tokens, literal ranges), catalog (function names and call shapes), parser (text to
tree), tree (references, reads), data (reading values, field selection), equality (CEL ==, map
lookup), operators (arithmetic, ordering, in), convert (the conversions), cost (the budget,
matches()), functions (the built-ins), evaluator (the tree walk, the macros), program (the backend).
This package re-exports the names the single module had.
"""
from __future__ import annotations

from .. import _regex, values
from ..errors import ALLOW_NON_FINITE, CelError, CelSyntaxError, check_finite, check_result, raised
from .catalog import _GETTERS, _MACROS, ARITY, CONVERSIONS, FUNCTIONS, INV_ARITY, arity_error
from .convert import _BOOL_TEXT, _FLOAT_TEXT, _INT_TEXT, _convert, _seconds
from .cost import _RE_CACHE, DEFAULT_BUDGET, STEPS_PER_COST, CostExceeded, _matches
from .data import (_KEY_TYPES, _NO_FIELDS, _TIME, _TY, _bool, _data, _finite, _int_range, _nty, _num, _on_map,
                   _safe_repr, _select, _ty, check_int, map_key)
from .equality import _SCALAR_TYPES, _eq, _eq_plain, _kind, lookup, whole
from .evaluator import _call, _iter, evaluate
from .lexer import _ESC, _EXP, _SIMPLE, _TOK, INT_MAX, INT_MIN, UINT_MAX, decode_string, tokenize
from .operators import _TIME_ARITH, _arith, _time_arith, ieee_div_zero
from .parser import Node, _check_inv, _Parser, parse
from .program import SubsetBackend, SubsetProgram
from .tree import _ELEMENT_MACROS, ELEM, SIZE, _macro_parts, _static_path, free_names, kids, reads, references

# The classes keep the module they had before the split: reprs, pickles and tests name it.
for _cls in (CostExceeded, _Parser, SubsetProgram, SubsetBackend):
    _cls.__module__ = __name__
del _cls

__all__ = ["CostExceeded", "parse", "evaluate", "references", "reads", "ELEM", "SIZE", "free_names", "SubsetProgram", "SubsetBackend", "FUNCTIONS",
           "ARITY", "arity_error"]
