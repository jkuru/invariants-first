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
"""
from __future__ import annotations

from .catalog import _GETTERS, ARITY, CONVERSIONS, FUNCTIONS, INV_ARITY, arity_error
from .convert import _convert
from .cost import DEFAULT_BUDGET, STEPS_PER_COST, CostExceeded
from .data import check_int, map_key
from .equality import _eq, lookup, whole
from .evaluator import evaluate
from .lexer import INT_MAX, INT_MIN, UINT_MAX, decode_string, tokenize
from .operators import ieee_div_zero
from .parser import Node, parse
from .program import SubsetBackend, SubsetProgram
from .tree import ELEM, SIZE, _macro_parts, free_names, kids, reads, references

__all__ = ["CostExceeded", "parse", "evaluate", "references", "reads", "ELEM", "SIZE", "free_names", "SubsetProgram", "SubsetBackend", "FUNCTIONS",
           "ARITY", "arity_error"]
