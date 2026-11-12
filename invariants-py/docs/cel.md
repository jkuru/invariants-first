# CEL

Rules are written in [CEL](https://cel.dev), the Common Expression Language. This page covers the
three engines, how Python values reach a rule, and where the engines still differ.

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
| `celpy` | cel-python | A pure-Python interpreter, with a per-call cost budget. Needs the `cel` extra. |

Pick a backend with `INV_CEL_BACKEND=subset|cel-expr|celpy|auto`, or with
`inv.load(..., backend=...)`. `auto` picks the first one installed, in the order cel-expr, celpy,
subset.

Every backend uses the same front end, so a rule parses the same way everywhere. Rule text must
stay within that grammar: `1e3`, `1u` and `b"..."` are parse errors. The loader checks every
function a clause calls against the chosen backend: a name it does not have (a typo such as
`sizee()`, `x.foo()`, or `type()` on the subset) is a `LoadError` naming the function and the
backend, not a `rule_error` on every call. So is a known function called with the wrong number of
arguments or in a form CEL does not have (`size(x, 1)`, `x.size(1)`, `'a'.startsWith()`,
`matches('a')`, `x.has(y)`), checked against an arity table for the global and the receiver form
(`invariants.cel.subset.ARITY`), and a macro written in global form (`exists(xs, x, p)`; write
`xs.exists(x, p)`). The extensions `inv.sum`, `inv.zip` and `inv.unique_by` work on every backend.
Every backend passes the conformance suite in `tests/conformance/cel/`.

## Cost limits

An integer outside int64 is a `rule_error` on every backend. Past a cost limit, the clause is a
`rule_error` "cost limit exceeded", never a hung worker.

- **subset:** 10,000 units per evaluation, absorbed by `||` and `&&`.
- **cel-expr:** fixed, not settable: at most 9,999 comprehension iterations per clause evaluation,
  summed over every `all`, `exists`, `exists_one`, `map` and `filter` in the clause. Nested ones
  multiply: `xs.exists(x, ys.exists(y, ...))` costs |xs|·|ys|. Work outside comprehensions (string
  functions, RE2 `matches()`, `size()`, equality) is not counted and is linear in the data the
  clause reads, so bound input sizes for untrusted data. cel-cpp also has a fixed nesting limit of
  32 levels (about 30 nested parentheses or calls, or a sum of more than about 250 terms); a deeper
  clause is a `LoadError` that says so.
- **celpy:** 100,000 tree nodes per call, not absorbed by `||` and `&&`.

**Regular expressions on the subset.** The subset has `matches()` with RE2 syntax (ASCII `\d \w \s`,
`$` only at the end of text, general-category `\p{..}` classes but not scripts, no lookaround or
backreferences). It runs on a linear-time matcher, not Python `re`, so no pattern and input can
backtrack exponentially, and its steps count against the cost budget: a huge input ends in
`rule_error`, never a hung worker. The matches() step bound is 16 matcher steps per unit of the
10,000-unit evaluation budget, shared with the rest of the clause. So a small pattern on an input of
roughly 10,000 to 100,000 characters or more is a `rule_error` on the subset, where cel-expr and
celpy return a result. The bound is not scaled with input length, since that would let a caller buy
CPU time with input size. Use cel-expr or celpy, or bound the input size first, for rules that match
long text. A constant pattern the subset matcher does not support but full RE2 may (a script class
like `\p{Greek}`) still loads; on the subset it is a `rule_error` at run time.

## Functions per backend

Standard functions per backend (global `f(x)` or receiver `x.f()` form as CEL defines them; the macros `all`, `exists`, `exists_one`, `map`, `filter` and `has()` are on every backend):

| Function | `subset` | `cel-expr` | `celpy` |
|---|---|---|---|
| `size`, `contains`, `startsWith`, `endsWith`, `matches` | yes | yes | yes |
| `int`, `double`, `string`, `bool`, `dyn` | yes (see below) | yes | yes |
| `timestamp`, `duration`, `getFullYear` and the other timestamp and duration getters | yes | yes | yes |
| `uint`, `bytes`, `type` | no (`LoadError`) | yes | yes |
| string extensions (`split`, `lowerAscii`, `substring`, ...), math and list extensions | no | no | no |

## Conversions

The subset's conversions follow the CEL spec: `int()` takes an int, a bool, a double (truncated toward zero; NaN, ±Infinity and values outside int64 are errors) or base-10 text (`"42"`, `" +5 "`; not `"4.2"`, `"0x10"` or `"1_000"`); `double()` takes an int, a double or decimal text; `string()` takes a string, an int, a bool (`"true"`) or UTF-8 bytes; `bool()` takes a bool or the text `1`, `t`, `true`, `TRUE`, `True`, `0`, `f`, `false`, `FALSE`, `False`. `string()` of a double is a `rule_error` on the subset, because the engines print doubles differently (`string(2.0)` is `"2"` on cel-expr and `"2.0"` on celpy); compare the number instead. On cel-expr and celpy the adapter routes `int()`, `double()`, `bool()` and `string()` of text, of a bool and (for `int()`) of a double through the same rules, so these agree on every backend (conformance suite `types.yaml`): `double("0x10")`, `int("0x10")`, `int("1_0")`, `double("1_0")`, non-ASCII digits (`int("٣")`), `int(-9223372036854775808.0)` and `bool(1)` are `rule_error`s, and `string(true)` is `"true"`. `timestamp()` and `duration()` of text, `string()` of a timestamp or a
duration, `int()` of a timestamp and the getters follow one set of rules on every backend too
(`cel/values.py`; conformance suite `time.yaml`).

## Python to CEL types

Every backend reads Python values through one mapping (`invariants/cel/values.py`), and the
load-time type checker uses the same table, so a rule reads the same value everywhere:

| Python value | CEL value | Notes |
|---|---|---|
| `None` | `null` | `x == null` and `has(r.x)` work for an `Optional` field on every backend |
| `bool` | `bool` | |
| `int` | `int` | outside int64: `rule_error` |
| `float` | `double` | NaN or ±Infinity: `rule_error` (unless `allow_non_finite=True`) |
| `str`, `bytes` | `string`, `bytes` | |
| `Enum` member | its value | mapped by this table |
| `uuid.UUID` | `string` | the canonical lower-case hyphenated form: `args.id == "1234...-...678"` |
| `datetime` with a `tzinfo` | `timestamp` | the same instant, in UTC; microsecond precision |
| `timedelta` | `duration` | up to ±10,000 years, as in CEL |
| `list`, `tuple` / `dict`, `Mapping` | list / map | |
| dataclass, pydantic model, other object | map of its public fields | |
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
cents). Without an annotation it is a `rule_error` that names the type
and the conversion to make; a naive datetime is always one at run time (an annotation cannot tell).
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
NUL, space or other control character, or digits of another script (`"+٠٥:٣٠"`) is a
`rule_error` everywhere. Digits in timestamp, duration and ISO 8601 text (and in rule text) are
ASCII only: `duration("٥m")` and a `timedelta` reply field `"PT٥M"` are refused. On
`cel-expr`, datetime and timedelta values reach the engine as protobuf `Timestamp` and `Duration`
messages, so that backend needs the `protobuf` package (in the `cel` extra; without it a clause that
reads an annotated datetime or timedelta is a `LoadError` saying so).

## Numbers

`int` and `float` annotations are CEL `int` and `double`, and CEL never converts between
them. `==`, `!=`, `<`, `<=`, `>`, `>=` compare across int and double (`args.qty < 2.5` is fine), but
arithmetic does not: `args.price * args.qty` with `price: float` and `qty: int` (or
`args.price + 1`) is a `LoadError` that says to write `args.price * double(args.qty)` (or
`args.price + 1.0`, or `int(args.price) * args.qty`), and `%` takes ints only (`args.x % 2.0` is a
`LoadError`). Every backend refuses the mixed forms at run time too, as CEL does (conformance suite
`numeric.yaml`). A parameter annotated `float` that the caller passes an int (`f(price=100)`) is
an int to CEL: pass `100.0`, or convert in the rule.

## NaN and Infinity

Every comparison with NaN is false, so a bound written the usual way, `x < 0.0 || x > 1.0`, is
silently false for `x = NaN`. So by default a clause that reads a NaN or ±Infinity (an argument, a
result, a provider value or a field of one, on every backend) is a `rule_error`, which
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

## Strings, maps and keys

- **NUL characters on cel-expr.** cel-cpp reads a Python string or bytes value as C text, which ends
  at the first NUL, so `"ab\x00cd"` would reach a rule as `"ab"`. On cel-expr, a clause that reads a
  string or bytes value holding a NUL is a `rule_error`. The subset and celpy read it whole.
- **Map keys and indexes.** Map keys follow CEL on every backend: a numeric index matches an int key
  across int and double (`{1: 'a'}[1.0] == 'a'`; `1.5` matches nothing), a bool is never a number
  key (`{1: 'a'}[true]` is no such key, though Python's `dict` calls `True` and `1` one key), a list
  index may be an integral double (`[7, 8][1.0] == 8`), and a map literal key must be an int, bool
  or string (`{1.0: 'a'}`, `{null: 1}` and a repeated key are `rule_error`s). Python cannot hold `1`
  and `true` as two keys of one map, so a map literal with both is a `rule_error` on the subset and
  celpy (cel-expr evaluates it).

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
durations (`timedelta`) are families of their own, and `UUID` is a string.

## Known engine differences

These are the cases where the backends still answer differently. None of them is in the conformance suite's `expected_failures.yaml`, which is empty: the suite pins down the behaviour that must agree (conversions, non-finite arithmetic, `string(null)`), and the rows below are differences of capability or of budget rather than of meaning.

| Case | `subset` | `cel-expr` | `celpy` |
|---|---|---|---|
| `string(2.0)` | `rule_error` (refused) | `"2"` | `"2.0"` |
| `uint`, `bytes`, `type` | `LoadError` | yes | yes |
| `type(x) == google.protobuf.Timestamp` (protobuf type names) | `LoadError` | yes | `rule_error` |
| Nesting past 32 levels (about 30 nested parentheses or calls, or a sum of more than about 250 terms) | yes | `LoadError` naming cel-cpp's limit | yes |
| An unannotated record, list or map that holds a refused value (a `Decimal` field), read whole (`args.items == []`; not `size()` or a comprehension that reads other fields) | result (the subset reads lazily) | `rule_error` | `rule_error` |
| `matches()` on a very long input | `rule_error` past the step budget | result | result |
| Cost limit | 10,000 units, absorbed by `\|\|`/`&&` | 9,999 comprehension iterations | 100,000 tree nodes, not absorbed |
| An error on both sides of `\|\|` | first error | first error | "no matching overload for _\|\|_" (still `rule_error`) |

Keep rules off `string()` of a double, and away from the budgets, if they must run on every backend. These used to differ and now agree: celpy's computed values are celtypes again (`type(x * 2.0) == double`, `string(duration('2s') - duration('1s')) == '1s'` and `.getSeconds()` of a computed duration agree with cel-expr), and so are celpy's `duration.getMilliseconds()` (the millisecond part, not the total), `string(timestamp)` with an offset (now UTC), `int(timestamp)` before 1970 (rounded down) and `-duration`/`duration * n` (no overload, as in CEL). Also fixed, and now the same on every backend: celpy's `string(null) == "None"`, `string(true) == "True"`, `bool(1)`, lenient `int()`/`double()` text, `int(-2^63 as a double)`; cel-expr's hex `double("0x10")`; and, with `allow_non_finite=True`, celpy's `x / 0.0` (it gave +Infinity for any `x`, `0.0` and negatives included).

## Performance

Guard overhead per call, measured on an Apple M4 Max laptop, Python 3.14.7, one `pre_effect`
decorator enforcer on a pure function with no providers, with the bare call (0.04 µs) subtracted.
There were 20,000 samples per row (3,000 for celpy), and two runs whose p50s agreed within 3%; the
table shows the mean p50 and the larger p99. The 1-clause rule is `args.total < 0`. The 4-clause
rule is `args.total < 0`, `args.total > 1000000`, `args.caps.exists(c, c < 0)` and
`size(args.caps) > 64`, called with an 8-element `caps` list (the `exists` cost grows with the list
length).

| Backend | 1 clause p50 | 1 clause p99 | 4 clauses p50 | 4 clauses p99 |
|---|---|---|---|---|
| `subset` | 7.1 µs | 8.4 µs | 22.7 µs | 28.3 µs |
| `cel-expr` | 9.6 µs | 10.7 µs | 22.0 µs | 27.9 µs |
| `celpy` | 86 µs | 100 µs | 830 µs | 892 µs |

- Each clause costs a few microseconds on `subset` and `cel-expr`, and tens to a hundred and more on
  `celpy`. Use `cel-expr` (or `subset`) on hot paths and keep `celpy` for conformance runs. Keep the
  clause count low on tight loops. At HTTP scale (milliseconds per request) the overhead is noise.
- Providers add their own cost on top: every provider an enforcer reads is called on every call.
- There is no sampled mode in v0.1.
- The table's rules do no arithmetic. A clause that does arithmetic pays for the non-finite check
  once per arithmetic expression (plus once per divisor that is itself arithmetic, and per
  `double()`), about 0.1 to 0.2 µs each on `cel-expr` (`args.a * args.b + args.c - args.d / 2.0 >
  1.0` costs 0.13 µs).
- The cel-expr 4-clause row includes a per-value NUL check (about 2 µs over the earlier 19.6 µs).
- Each datetime or timedelta value a clause reads costs about 1.3 µs more on `cel-expr` (its
  conversion to a protobuf message).
