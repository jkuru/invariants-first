# CEL

Rules are written in [CEL](https://cel.dev), the Common Expression Language. This page covers the
two engines, how Python values reach a rule, and where the engines still differ. The engines and the
type check are the invariants-cel library (`invariants_cel`, under `packages/invariants-cel/` of the
monorepo), which `invariants.cel` re-exports and which works on its own: see its README.

- [Backends](#backends)
- [Cost limits](#cost-limits)
- [Functions per backend](#functions-per-backend)
- [Conversions](#conversions)
- [Python to CEL types](#python-to-cel-types)
- [Time and zones](#time-and-zones)
- [Numbers](#numbers)
- [NaN and Infinity](#nan-and-infinity)
- [Strings, maps and keys](#strings-maps-and-keys)
- [The load-time type check](#the-load-time-type-check)
- [Known engine differences](#known-engine-differences)
- [Performance](#performance)

## Backends

| Backend | Engine | Notes |
|---|---|---|
| `subset` (default) | stdlib | A safe tree-walking evaluator with a cost budget. No extra dependency. |
| `cel-expr` | cel-expr-python | The CEL C++ runtime. Needs the `cel` extra (it brings `protobuf` too). |

Pick a backend with `INV_CEL_BACKEND=subset|cel-expr|auto`, or with
`inv.load(..., backend=...)`. `auto` picks cel-expr if it is installed, else subset.

Every backend uses the same front end, so a rule parses the same way everywhere. Rule text must
stay within that grammar: a bytes literal (`b"..."`) is a parse error. String escapes are CEL's
(`\n`, `\t`, `\\`, `\'`, `\"`, `` \` ``, `\?`, `\a`, `\b`, `\f`, `\r`, `\v`, `\xHH`, `\uHHHH`,
`\UHHHHHHHH`, octal `\000` to `\377`); any other backslash (`'\d+'`) is a parse error on every
backend, so a regex is written as a raw string (`r'\d+'`, no escapes; a backslash may not come right
before the closing quote) or with the backslash doubled (`'\\d+'`). A string literal may not span
lines (no newline between its quotes). Exponents (`1e3`) and uint
literals (`1u`) parse; a uint literal is `uint(n)`, so it loads on cel-expr only ([Number
literals](#number-literals)). The loader checks every
function a clause calls against the chosen backend: a name it does not have (a typo such as
`sizee()`, `x.foo()`, or `type()` on the subset) is a `LoadError` naming the function and the
backend, not a `rule_error` on every call. So is a known function called with the wrong number of
arguments or in a form CEL does not have (`size(x, 1)`, `x.size(1)`, `'a'.startsWith()`,
`matches('a')`), checked against an arity table for the global and the receiver form
(`invariants.cel.subset.ARITY`), and a macro written in global form (`exists(xs, x, p)`; write
`xs.exists(x, p)`). `has()` has only the global form: `x.has(y)` does not parse ("has() has only the
global form: has(a.b)"). The extensions `inv.sum`, `inv.zip` and `inv.unique_by` work on every backend.
Every backend passes the conformance suite in `packages/invariants-cel/tests/conformance/cel/`.

## Cost limits

An integer outside int64 is a `rule_error` on every backend. Past a cost limit, the clause is a
`rule_error` "cost limit exceeded", never a hung worker.

- **subset:** 10,000 units per evaluation. A spent budget is never absorbed: `false && x`,
  `true || x` and an `exists` or `all` that has already decided are still a `rule_error` when `x`
  runs out of budget (the subset evaluates both sides, for CEL's commutative `&&` and `||`).
- **cel-expr** stops at what decides the clause, left to right: `false && x`, `true || x` and an
  `exists` or `all` that has already decided never run `x`, so they are `false` or `true` where the
  subset is a `rule_error`. A comprehension that does run past the limit is a `rule_error`, never
  absorbed by the rest of the clause (`xs.all(x, x >= 0) && false` over 10,000 elements).
- **cel-expr** budget, fixed, not settable: at most 9,999 comprehension iterations per clause evaluation,
  summed over every `all`, `exists`, `exists_one`, `map` and `filter` in the clause. Nested ones
  multiply: `xs.exists(x, ys.exists(y, ...))` costs |xs|·|ys|. In a clause that has a
  comprehension, each check the backend adds around arithmetic, `double()`, `int()`, `timestamp()`
  and `duration()` (see [NaN and Infinity](#nan-and-infinity)) costs one iteration each time it
  runs, so `xs.all(x, x * 2 >= 0)` reaches the limit at 5,000 elements, not 10,000 (the price of
  never reading a stale cel-cpp slot: see [Known engine differences](#known-engine-differences)).
  Other work (string functions, RE2 `matches()`, `size()`, equality) is not counted. Each call is
  linear in its operands, but a clause can make one per iteration, so its work is up to about the
  iteration limit times the size of the data it reads: bound input sizes for untrusted data.
  cel-cpp also has a fixed nesting limit of 32 levels (about 30 nested parentheses or calls, or a
  sum of more than about 220 terms, about 100 in a clause that also has a comprehension); a deeper
  clause is a `LoadError` that says so.
- **Nesting, on both:** the parser refuses a clause nested more than 64 levels deep (parentheses,
  brackets, call arguments, `?:` branches, unary operators) or whose tree is more than 256 levels
  deep (a chain of more than about 255 `&&`, `||`, `+` or field reads): a `LoadError` naming the
  rule, never a `RecursionError`. As in CEL's grammar, the middle of a `?:` cannot be another `?:`
  without parentheses: write `a ? (b ? c : d) : e`.
- **String functions, on both:** `contains()`, `startsWith()`, `endsWith()` and `matches()` refuse
  an operand longer than the string-size limit, 1,000,000 characters by default
  (`INV_CEL_STRING_LIMIT` at import, `invariants.cel.set_string_limit(n)` at run time): past it the
  clause is a `rule_error` on every backend. On cel-expr, `contains()`, `startsWith()` and
  `endsWith()` run in Python (cel-cpp's `contains()` is quadratic: 17 s on 2,000,000 characters);
  on the subset they cost one budget unit per 4,096 characters they scan, on top of their node's.

**Regular expressions on the subset.** The subset has `matches()` with RE2 syntax (ASCII `\d \w \s`,
`$` only at the end of text, general-category `\p{..}` classes but not scripts, no lookaround or
backreferences). It runs on a linear-time matcher, not Python `re`, so no pattern and input can
backtrack exponentially, and its steps count against the cost budget: a huge input ends in
`rule_error`, never a hung worker. The matches() step bound is 16 matcher steps per unit of the
10,000-unit evaluation budget, shared with the rest of the clause. So a small pattern on an input of
roughly 10,000 to 100,000 characters or more is a `rule_error` on the subset, where cel-expr
returns a result. The bound is not scaled with input length, since that would let a caller buy
CPU time with input size. Use cel-expr, or bound the input size first, for rules that match
long text. A constant pattern the subset matcher does not support but full RE2 may (a script class
like `\p{Greek}`) still loads; on the subset it is a `rule_error` at run time. `(?i)` folds case
one character at a time, as RE2 does: `s`, `S` and `ſ` match each other, but `ß` matches neither
`SS` nor `[A-Z]`, and `ı` does not match `I`. A class (`\w`, `\d`, `\s`, `\pL`, `\p{Lu}`, `[:upper:]`)
is closed under that folding first and negated after, as RE2 builds it: `(?i)\p{Lu}` matches `a`,
`(?i)\w` matches `K` (the Kelvin sign, which folds to `k`) and `(?i)\W` does not. An escaped ASCII
punctuation mark is that character (`\_` too); an escaped non-ASCII character (`\é`) is not RE2
syntax. An octal escape reads as RE2 reads it: `\0` and up to two more octal digits (`\0`, `\012`),
or `\1` to `\7` followed by at least one more (`\101` is `A`, `\12` a newline, `\1234` is `S`
then `4`); a lone `\1` to `\7`, `\8` or `\9` would be a backreference and is refused. `\Q...\E` is its characters one at a time, as in RE2: a quantifier after it repeats the last
one (`^\Qab\E*$` matches `abbb`, not `abab`), and an empty `\Q\E` is nothing, so a quantifier after
it takes what comes before (`a\Q\E*` is `a*`; `\Q\E?` alone is not a valid pattern). A repeat count
with nothing before it (`{2}`, `a|{2}`) is not valid RE2, not a literal. RE2 searches UTF-8 bytes,
and the subset follows it at the one place that shows: a search may start inside a multi-byte
character, where `\B` holds (its neighbours are non-word bytes) and nothing else does, so
`"aéa".matches("\\B")` is true on both backends; `\b` and every pattern that reads a character
match at character boundaries only.

**The pattern limit, on both.** A `matches()` pattern may be at most 4,096 characters, nest groups
at most 64 deep, and expand (every `{n,m}` written out) to at most 5,000 instructions. A constant
pattern past it is a `LoadError`; one from the data (`args.s.matches(args.p)`) is a `rule_error`
"matches(): the pattern is over the pattern limit" on every backend, checked before any engine
compiles it (cel-cpp compiles a pattern from the data on every call, and `[a-z]{1000}` repeated 50
times took 0.8 s each; 5,000 nested parentheses overflowed the subset's parser). On the subset a
pattern's compile is charged to the budget on every call, one unit per 16 characters and
instructions, cached or not, so the verdict never depends on the cache (256 compiled patterns, the
oldest dropped first).

## Functions per backend

Standard functions per backend (global `f(x)` or receiver `x.f()` form as CEL defines them; the macros `all`, `exists`, `exists_one`, `map`, `filter` and `has()` are on every backend):

| Function | `subset` | `cel-expr` |
|---|---|---|
| `size`, `contains`, `startsWith`, `endsWith`, `matches` | yes | yes |
| `int`, `double`, `string`, `bool`, `dyn` | yes (see below) | yes |
| `timestamp`, `duration`, `getFullYear` and the other timestamp and duration getters | yes | yes |
| `uint`, `bytes`, `type` | no (`LoadError`) | yes |
| string extensions (`split`, `lowerAscii`, `substring`, ...), math and list extensions | no | no |

`type()` is cel-expr's only, and the loader reads a bare type name in a clause as an undeclared
name: `type(x) == int` and `type(x) == google.protobuf.Timestamp` are a `LoadError` ("reads undeclared
name(s) ['int']: int is a CEL type name, which a clause cannot read: compare with the type of a value,
type(x) == type(1)") on every backend. Compare with the type of a literal instead: `type(x) ==
type(1)` (`type(0u)`, `type(1.0)`, `type('')`, `type(true)`, `type(bytes(''))`, `type([])`, `type({})`,
`type(null)`, `type(timestamp(0))`). A clause that calls `isinstance()` gets the same advice.

On bytes, on every backend: `size()` is the byte length, `<` `<=` `>` `>=` order lexicographically by
byte, `+` concatenates, and `==`, `!=` and `in` compare bytes with bytes (never with a string).

## Conversions

The subset's conversions follow the CEL spec: `int()` takes an int, a bool, a double (truncated toward zero; NaN, ±Infinity and values outside int64 are errors) or base-10 text (`"42"`, `" +5 "`; not `"4.2"`, `"0x10"` or `"1_000"`; the space around it is ASCII
space, tab, newline, vertical tab, form feed or carriage return, as absl reads it on cel-expr, so
`int("500\u00a0")` is a `rule_error` on both); `double()` takes an int, a double or decimal text; `string()` takes a string, an int, a bool (`"true"`) or UTF-8 bytes; `bool()` takes a bool or the text `1`, `t`, `true`, `TRUE`, `True`, `0`, `f`, `false`, `FALSE`, `False`. `string()` of a double is a `rule_error` on the subset, because the engines print doubles differently (`string(2.0)` is `"2"` on cel-expr and `"2.0"` on other engines); compare the number instead. On cel-expr the adapter routes `int()`, `double()`, `bool()` and `string()` of text, of a bool and (for `int()`) of a double through the same rules, so these agree on every backend (conformance suite `types.yaml`): `double("0x10")`, `int("0x10")`, `int("1_0")`, `double("1_0")`, non-ASCII digits (`int("٣")`), `int(-9223372036854775808.0)` and `bool(1)` are `rule_error`s, and `string(true)` is `"true"`. `timestamp()` and `duration()` of text, `string()` of a timestamp or a
duration, `int()` of a timestamp and the getters follow one set of rules on every backend too
(`invariants_cel/values.py`; conformance suite `time.yaml`).

## Python to CEL types

Every backend reads Python values through one mapping (`invariants_cel/values.py`), and the
load-time type checker uses the same table, so a rule reads the same value everywhere:

| Python value | CEL value | Notes |
|---|---|---|
| `None` | `null` | `x == null` and `has(r.x)` work for an `Optional` field on every backend |
| `bool` | `bool` | |
| `int` | `int` | outside int64: `rule_error` |
| `float` | `double` | NaN or ±Infinity: `rule_error` (unless `allow_non_finite=True`) |
| `str`, `bytes` | `string`, `bytes` | |
| `Enum` member | its value | mapped by this table |
| a `str`, `int`, `float` or `bytes` subclass (not an `Enum`) | its plain value | `class Role(str)` is a `string` (`args.role == 'guest'` holds for `Role('guest')`), `numpy.float64` a `double`; a `bool` stays a `bool` |
| `uuid.UUID` | `string` | the canonical lower-case hyphenated form: `args.id == "1234...-...678"` |
| `datetime` with a `tzinfo` | `timestamp` | the same instant, in UTC; microsecond precision |
| `timedelta` | `duration` | up to ±10,000 years, as in CEL |
| `list`, `tuple` / `dict`, `Mapping` | list / map | an `Enum` key is its value, as any `Enum` |
| pydantic `RootModel` | its `.root` | `RootModel[list[Line]]` is the list the API received |
| dataclass, pydantic model, other object | map of its public fields | `size()`, `in`, `[]` and the macros read it as that map, on every backend |
| other sequence or iterable (`UserList`, `deque`, `range`, a generator, any class with `__iter__`, `__len__` or `__getitem__`) | refused | pass a `list`: never read as the map of its attributes, never iterated |
| naive `datetime` (no `tzinfo`) | refused | the library never assumes a timezone: use `datetime.now(timezone.utc)` or attach a `tzinfo` |
| `datetime.date` | refused | CEL has no date type: pass an aware `datetime` (midnight UTC, say) or an ISO string `"2024-01-31"` |
| `datetime.time` | refused | pass seconds since midnight (int) or an `"HH:MM"` string |
| `decimal.Decimal` | refused | CEL has no decimal, and a double loses money precision (`0.1 + 0.2 != 0.3`): pass int minor units (cents) or a `str` |
| `Fraction`, `complex` | refused | pass an int, a float or a str |
| `set`, `frozenset` | refused | CEL has no set: pass a list |

A refused value is a `LoadError` when the clause reads a path whose annotation shows it (a
parameter `v: Decimal`, a field `d: date`, pydantic's `NaiveDatetime`), and also when it reads a
record, list or map whole whose annotation holds one, because the engines convert what a clause
reads whole (`args.items == []`, `args.items[0]`, `x in args.m`, `args.items.filter(...)`, or a
comprehension variable used whole). Only what is read is converted: a comprehension reads just the
element fields its body names, and `size()` reads only the length, so with `items: list[Item]` and
`Item.price: Decimal`, `size(args.items) > 100` and `args.items.exists(i, i.qty < 0)` load and
never touch `price` on any backend, while `args.items.exists(i, i.price > 0)` is refused (store
cents). An annotation naming a concrete list-like class (`xs: Batch` where
`Batch` subclasses `collections.abc.Sequence`, `collections.UserList`, `deque`) is refused the same
way; an abstract one (`Sequence[int]`, `Iterable[int]`) is not, since the value passed may be a list,
and a value that is not a list or tuple is then a `rule_error` at run time. Without an annotation it
is a `rule_error` that names the type and the conversion to make; a naive datetime is always one at run time (an annotation cannot tell).
The type checker knows `datetime` (and pydantic's `AwareDatetime`, `PastDatetime`, `FutureDatetime`)
as timestamp, `timedelta` as duration and `UUID` as string, so `args.deadline < 5`,
`args.deadline < args.wait`, `args.wait * 2`, `-args.wait`, `args.deadline.year` and a constant
that does not parse (`timestamp('2024-01-31')`, `duration('1d')`) or a constant timezone that is not
one (`t.getHours('+25:00')`, `t.getHours('')`) are `LoadError`s. CEL's
arithmetic holds: timestamp ± duration, duration ± duration and timestamp − timestamp; durations do
not scale or negate.

## Time and zones

Timestamp and duration text: `timestamp()` takes RFC 3339 (`2024-01-31T09:30:00Z`, an offset
`+05:30` or `+0530`, lower-case `t`/`z`, up to six fraction digits, or nine whose last three are
zero) or int Unix seconds; `duration()` takes a sign and number-unit parts with units `h`, `m`,
`s`, `ms`, `us`, `ns` (`1h30m`, `-1.5s`, `0`; no `d`); a sub-microsecond value is refused.
`string()` prints `"5400s"`, `"-1.500s"` and `"2024-01-31T09:30:00.500Z"` (UTC). The getters take
an optional IANA zone or offset (`t.getHours("America/New_York")`, `t.getHours("+05:30")`; an
offset must be between `-23:59` and `+23:59`); a zone read from data (`t.getHours(args.tz)`) is
checked by the same rules on every backend, so `""`, `"+99:00"`, an unknown name, a name with a
NUL, space or other control character, or digits of another script (`"+٠٥:٣٠"`), or null (an
`args.tz` of None: no zone, not UTC) is a `rule_error` everywhere; so is any argument to a
duration's getter (`d.getSeconds(args.tz)`). A getter whose zone moves an instant past CEL's range
(`t.getFullYear('-23:59')` on 0001-01-01, `t.getFullYear('+23:59')` on 9999-12-31) is a `rule_error`
"timestamp out of range" on every backend (cel-cpp printed year 0 and 10000). Digits in timestamp, duration and ISO 8601 text (and in rule text) are
ASCII only: `duration("٥m")` and a `timedelta` reply field `"PT٥M"` are refused. On
`cel-expr`, datetime and timedelta values reach the engine as protobuf `Timestamp` and `Duration`
messages, so that backend needs the `protobuf` package (in the `cel` extra; without it a clause that
reads an annotated datetime or timedelta is a `LoadError` saying so).

## Numbers

`int` and `float` annotations are CEL `int` and `double`, and CEL never converts between
them. `==`, `!=`, `<`, `<=`, `>`, `>=` compare across int and double (`args.qty < 2.5` is fine), but
arithmetic does not: `args.price * args.qty` with `price: float` and `qty: int` (or
`args.price + 1`) is a `LoadError` that says to convert one side (`double(args.qty)`, or
`double(1)`; write `1.0`, or `int(args.price) * args.qty`), and `%` takes ints only (`args.x % 2.0` is a
`LoadError`). Every backend refuses the mixed forms at run time too, as CEL does (conformance suite
`numeric.yaml`). A parameter annotated `float` that the caller passes an int (`f(price=100)`) is
an int to CEL: pass `100.0`, or convert in the rule.

An int and a double compare exactly, by their true values, on every backend: `2**53 + 1 >
2.0**53` and `2**63 - 1 < 2.0**63` are true, and `double(x) == x + 1` is false for `x = 2**53`; a
uint too (`uint("9007199254740993") > 9007199254740992.0`). cel-cpp rounds an int or uint to a
double first, which is exact only up to 2^53 in magnitude, so on cel-expr a clause in which an int
past 2^53 (from the data the clause reads, a literal, an arithmetic, `int()` or `uint()` result, or
`inv.sum()`) may meet a double (from the data the clause reads, a double literal or `double()`) is
evaluated exactly. Each clause decides from what it reads: a big int in another argument never
moves it. The engine still evaluates the clause first, so its comprehension budget decides as
always (a clause past 9,999 iterations is the same `rule_error`); then the subset evaluates it
exactly, with every function cel-expr has (`uint()`, `bytes()` and `type()`
included), `&&`, `||`, `all` and `exists` stopping where cel-cpp stops, and a budget that covers
9,999 iterations of the clause. Slower, same verdict, exactly compared.

## NaN and Infinity

Every comparison with NaN is false, so a bound written the usual way, `x < 0.0 || x > 1.0`, is
silently false for `x = NaN`. So by default a clause that evaluates a NaN or ±Infinity (an argument,
a result, a provider value or a field of one, on every backend) is a `rule_error`, which
`on_error: fail_closed` rejects. On the FastAPI integration, a JSON body with `NaN` (which pydantic
accepts by default) is answered 503 rather than passing. Model replies never carry one: `inv.parse`
refuses them (pydantic schemas included).

A NaN or ±Infinity the clause makes itself is a `rule_error` too, on every backend: every `+`, `-`,
`*`, `/`, `double()` and `inv.sum` result is checked, so `0.0 / 0.0`, `x / 0.0`, an overflow such as
`1e200 * 1e200` (and `inf - inf` after it) and `double("NaN")` cannot slip past a bound. Integer
division by zero is a `rule_error`, as in CEL.

`inv.configure(allow_non_finite=True)` lets rules read and make them as IEEE numbers instead
(`1.0 / 0.0` is Infinity on every backend). Then write bounds in the NaN-safe form,
`!(x >= 0.0 && x <= 1.0)`, which is true for NaN, or test `x == x` (false only for NaN). With
`allow_non_finite=True`, whole values compare by CEL's rules on every backend: a list holding a NaN
is not equal to itself. Better still, refuse non-finite numbers at the boundary (pydantic
`ConfigDict(allow_inf_nan=False)`).

A NaN inside a list that a clause never reads (only `size()` of it, or other fields of its
elements) is not noticed on any backend. A NaN anywhere in a list, map or record the clause compares
whole (`==`, `!=`, `in`) is a `rule_error` on every backend.

A NaN or ±Infinity that the clause reads but never evaluates (`true || args.f > 1.0`, or a branch
of `?:` not taken) differs: the subset reads values lazily, so CEL's error absorption gives the
clause's value (`true`), while cel-expr refuses the value on its way to the engine, so the clause is
a `rule_error`. Neither passes a NaN through a comparison: a clause that evaluates one is a
`rule_error` on both (see [Known engine differences](#known-engine-differences)).

## Strings, maps and keys

- **NUL characters on cel-expr.** cel-cpp reads a Python string or bytes value as C text, which ends
  at the first NUL, so `"ab\x00cd"` would reach a rule as `"ab"`. On cel-expr, a clause that reads a
  string or bytes value holding a NUL is a `rule_error`. The subset reads it whole. Only the clause
  that reads it: the rule's other clauses evaluate as usual, so `args.amount > 100` still fires when
  another clause reads a `note` holding a NUL (under `fail_open_and_page` too).
- **Surrogate code points.** A Python `str` can hold a lone surrogate (U+D800 to U+DFFF), which is
  not Unicode text and has no UTF-8 form: `json.loads('"\ud800"')`, a FastAPI or pydantic
  python-mode body, a tool input re-dumped with `ensure_ascii=False`. A clause that reads one (a
  value, a map key, inside a list, map or record) is a `rule_error` on every backend, with a
  message that does not quote it; cel-cpp crashed the process converting one. A rule literal
  holding one (`'\ud800'`, or the character itself) does not load. Valid text, emoji included,
  reads as before.
- **Map keys and indexes.** Map keys follow CEL on every backend: a numeric index matches an int key
  across int and double (`{1: 'a'}[1.0] == 'a'`; `1.5` matches nothing), a bool is never a number
  key (`{1: 'a'}[true]` is no such key, though Python's `dict` calls `True` and `1` one key), a list
  index may be an integral double (`[7, 8][1.0] == 8`), and a map literal key must be an int, bool
  or string (`{1.0: 'a'}`, `{null: 1}` and a repeated key are `rule_error`s). Looking up a value that
  can never be a key (bytes, `null`, a list, a map, a timestamp: `null in args.m`, `args.m[args.blob]`)
  is a `rule_error` on every backend, not `false`. Python cannot hold `1`
  and `true` as two keys of one map, so a map literal with both is a `rule_error` on the subset
  (cel-expr evaluates it).
- **Mappings.** Any `collections.abc.Mapping` (a `dict`, a `MappingProxyType`, your own `Mapping`)
  is a CEL map on every backend: field selection, indexing, `size()`, `in`, `has()` and the macros
  read its keys. An `Enum` key is its value (`{Color.RED: 'x'}` with `RED = 1` answers `m[1]`); a
  map holding both an `Enum` key and its value is a `rule_error` "repeated map key". A record is the
  map of its public fields here too (`size(r)`, `'x' in r`, `r['x']`, `r.exists(k, ...)`).

## Number literals

Both backends read rule text through one parser, with CEL's number grammar: ints in decimal or hex
(`0x10`), doubles as `1.5`, `1e5`, `1.5E-3` or `.5`, and uints with a `u` suffix (`5u`, `0x10u`).
An int literal outside int64 is a `LoadError` (`CelSyntaxError`), as in cel-cpp; a single minus sign
on the literal reaches `-9223372036854775808`, but `--9223372036854775808` and
`-(9223372036854775808)` do not load. A uint literal outside uint64 does not load either, and a
uint literal is `uint(n)`, so it needs cel-expr. A double literal that would be Infinity (`1e400`)
does not load: cel-cpp reads it as Infinity, which the library refuses everywhere else.

## The load-time type check

The loader type-checks every clause against the target's annotations (`args`, `result`), the
provider types and the model-call schema (`reply`). It refuses a clause that:
- orders, compares with `==`/`!=`, or does arithmetic across type families (`args.n > "x"` or
  `args.n == "x"` with `n: int`);
- reads a field a closed type does not have (`rec.sizee` on a dataclass, pydantic model or TypedDict
  without it, or any field on an int, string, bool or list), or tests such a field with `has()`;
- uses `in` with an element of another family than the list's elements or a map's keys
  (`args.n in ["a"]`);
- passes a constant pattern that is not valid RE2 to `matches()` (`matches("(")`);
- applies logic to a non-bool, or is not a bool;
- reads a type CEL cannot read (`Decimal`, `date`, ...): see [Python to CEL types](#python-to-cel-types).

It is conservative. Unannotated names, `Any`, plain `dict`, `self` and `old_self`, monitor rows and
other classes are not checked. A property or class attribute counts as a field (pydantic's
`BaseModel` API does not). int, uint and double compare with each other but do not mix in
arithmetic ([Numbers](#numbers)). `null` and dyn compare with anything. Timestamps (`datetime`) and
durations (`timedelta`) are families of their own, and `UUID` is a string. Annotations are read
through PEP 695 aliases (`type Cents = int`, `Page[int]`), `NewType`, `Annotated` and a TypedDict
key's `Required`/`NotRequired`/`ReadOnly`, as the lock pins them: `args.amount == "x"` with
`amount: Cents` is refused like it is with `amount: int`, and a `typing_extensions.TypedDict` is a
closed record like a `typing.TypedDict`. An alias whose value does not evaluate (`type Later =
OnlyTyping`, with `OnlyTyping` imported under `TYPE_CHECKING`) is dyn, as an annotation that does not
resolve is, and the lock pins it by its name (`{"unresolved": "Later"}`).

## Known engine differences

These are the cases where the backends still answer differently. None of them is in the conformance suite's `expected_failures.yaml`, which is empty: the suite pins down the behaviour that must agree (conversions, non-finite arithmetic, `string(null)`), and the rows below are differences of capability or of budget rather than of meaning.

| Case | `subset` | `cel-expr` |
|---|---|---|
| `string(2.0)` | `rule_error` (refused) | `"2"` |
| `uint`, `bytes`, `type` | `LoadError` | yes |
| Nesting past 32 levels (about 30 nested parentheses or calls, or a sum of more than about 220 terms, about 100 with a comprehension in the clause) | yes, up to its own limit (64 nested levels, a tree 256 deep), past which it is a `LoadError` too | `LoadError` naming cel-cpp's limit |
| `matches()` on a very long input | `rule_error` past the step budget | result |
| Cost limit | 10,000 units, never absorbed: `false && x` is a `rule_error` when `x` runs out | 9,999 comprehension iterations; a part the clause does not need (`false && x`) never runs, so it gives the clause's value |
| A string holding a NUL or a surrogate code point, a NaN or ±Infinity, or a list-like value that is not a list, that the clause references but does not need (`false && args.s == 'x'`, `true \|\| args.f > 1.0`, or an `\|\|` the other side decides) | result (the subset reads lazily, and CEL absorbs the error) | `rule_error` (refused on its way to the engine) |
| A duration past about ±292 years (cel-cpp holds int64 nanoseconds; CEL allows ±10,000 years): in the data, from `duration(text)`, or made by arithmetic (`t - u` for timestamps more than 292 years apart) | result | `rule_error` (cel-cpp would read it saturated, so `d + duration('0s') != d`), a literal included |
| Regex `\C` (any byte), `\p{Greek}` (scripts) | `rule_error` at run time | evaluated by RE2 |

In every row one backend answers and the other is a `rule_error` (or a `LoadError`); in none do the
two return different values. Keep rules off `string()` of a double, and away from the budgets, if
they must run on every backend. These used to differ and now agree: cel-expr's hex `double("0x10")`
is a `rule_error` as on the subset; `has()` on a value with no fields (`has(x.f)` with `x` null, a
number, a string or a list) is a `rule_error` on the subset as on cel-expr (on a map it tests the
key; a null field is present), and `has(x.f)` reads `x.f` whole, so a present field holding a value
the library refuses (NaN, a `Decimal`, a set, a generator, also nested inside it) is a `rule_error`
on both; arithmetic inside a `has()` argument is checked finite on cel-expr as elsewhere; a
backslash escape CEL does not have (`'\d'`) is a parse error on both (the subset read it as `d`,
and the rewritten text cel-expr evaluated then had `d` too); an int and a double past 2^53 compare exactly
([Numbers](#numbers)); a string holding a surrogate code point is a `rule_error` on both
([Strings, maps and keys](#strings-maps-and-keys)); and a constant timezone the subset refuses
(`t.getHours('+25:00')`, which cel-cpp read as a 25-hour offset) is a `rule_error` on both; text with
Unicode space around the number (`int("500\u00a0")`, `double("\u30001")`) is a `rule_error` on both
(the subset took it, and `"1\u001c"` escaped as a Python error); under `(?i)` a class such as `\w`,
`\W` or `\p{Lu}` folds case on the subset as RE2 does; and an even run of `!` or unary `-`
(`!!args.s == "x"`, `--args.s`) is a `rule_error` on a non-bool (non-number) operand on both
(cel-cpp's parser drops such a run unchecked, so the front end sends the clause re-printed as
`!(!(args.s))` whenever it has one).

**A cel-cpp defect the backend works around.** cel-cpp evaluates `cel.bind` lazily, and a
comprehension that stops early (`exists` at its first true element, `all` at its first false one)
leaves its loop variable in its slot, where a later `cel.bind` in the same slot reads it in place of
its own value. The cel-expr backend used `cel.bind` for its checks, so `args.qtys.exists(q, q > 50)
&& args.total + args.fee > 100` let `ship([60, 1], 500, 5)` through. Now a clause with a
comprehension evaluates each checked value once as the one element of a `map()`
(`[x].map(v, check(v))[0]`, an ordinary comprehension whose variable is always set from its list),
and only a clause without comprehensions, which has no stale slot, uses `cel.bind`. The backend
checks this against the engine when it starts (clauses that stop early at one, two and three
levels, then do arithmetic, `double()`, a long sum, `duration()` and `timestamp()`); if an engine
build fails the check, a clause with a comprehension and a check is a `LoadError` on cel-expr that
says to use the subset (fail closed). A differential fuzz of 128,000 such clauses (comprehensions
that stop early mixed with arithmetic, `double()`, `int()`, long sums, durations and timestamps)
finds no disagreement with the subset other than the rows above.

## Performance

Guard overhead per call, measured on an Apple M4 Max laptop, Python 3.14.7, one `pre_effect`
decorator enforcer on a pure function with no providers, with the bare call (0.04 µs) subtracted.
There were 20,000 samples per row, and two runs whose p50s agreed within 3%; the
table shows the mean p50 and the larger p99. The 1-clause rule is `args.total < 0`. The 4-clause
rule is `args.total < 0`, `args.total > 1000000`, `args.caps.exists(c, c < 0)` and
`size(args.caps) > 64`, called with an 8-element `caps` list (the `exists` cost grows with the list
length).

| Backend | 1 clause p50 | 1 clause p99 | 4 clauses p50 | 4 clauses p99 |
|---|---|---|---|---|
| `subset` | 7.1 µs | 8.4 µs | 22.7 µs | 28.3 µs |
| `cel-expr` | 9.6 µs | 10.7 µs | 22.0 µs | 27.9 µs |

- Each clause costs a few microseconds on `subset` and `cel-expr`. Keep the clause count low on tight
  loops. At HTTP scale (milliseconds per request) the overhead is noise.
- Providers add their own cost on top: every provider bound to a name a clause reads is called on
  every call (async ones one at a time, or together when they all declare `concurrent: true`).
- There is no sampled mode in v0.1.
- The table's rules do no arithmetic. A clause that does arithmetic pays for the non-finite check
  once per arithmetic expression (plus once per divisor that is itself arithmetic, and per
  `double()`), about 0.1 to 0.2 µs each on `cel-expr` (`args.a * args.b + args.c - args.d / 2.0 >
  1.0` costs 0.13 µs). In a clause that also has a comprehension each check is a one-element
  `map()` instead (see [Known engine differences](#known-engine-differences)), about 0.2 µs: a
  check inside a comprehension body costs that per element (`args.l.all(x, x * 2 >= 0)` over 100
  elements: 62 µs, against 41 µs with the earlier, unsafe `cel.bind`).
- The cel-expr 4-clause row includes a per-value NUL check (about 2 µs over the earlier 19.6 µs).
- Each datetime or timedelta value a clause reads costs about 1.3 µs more on `cel-expr` (its
  conversion to a protobuf message).
