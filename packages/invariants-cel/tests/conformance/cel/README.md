# CEL backend conformance cases

Language-neutral cases (guide section 4.5). Each file is a `suite` with `cases`; a case has:

- `expr`: CEL text, evaluated as a rule's `bad` clause.
- `env`: variables (plain YAML data), optional. Python values YAML cannot write are one-key maps:
  `{$timestamp: "2024-06-01T12:00:00Z"}` (an aware datetime), `{$naive: "2024-06-01T12:00:00"}`
  (a naive datetime), `{$duration: 90}` (a timedelta of that many seconds), `{$uuid: "..."}`,
  `{$date: "2024-06-01"}` and `{$decimal: "1.50"}`. The mapping they go through is the one in
  `src/invariants/cel/values.py`.
- `backends`: the backends the case runs on, optional (default: every one). Only used for
  `type()`, which the subset does not have.
- exactly one of
  - `expected`: `ok` (false), `violation` (true), `rule_error` (evaluation error or non-bool),
    or `parse_error` (does not compile);
  - `value`: the exact result (int and double are distinct; so are bool and int).

`expected_failures.yaml` lists, per backend, the cases that backend is known to get wrong,
with the reason. The runner (`tests/test_cel_conformance.py`) marks those as strict xfail, so
a fixed backend shows up as an unexpected pass.
