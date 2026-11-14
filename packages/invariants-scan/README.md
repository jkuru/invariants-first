# invariants-scan

A static, read-only call-graph check for Python: **every call path to a protected function (the
sink) passes its door.** Import name `invariants_scan`; command `inv-scan`.

You name a sink (`py:shop.gateway.charge`, the function that moves money) and its door
(`py:shop.service.checkout`, the one function allowed to call it, which checks the order first). The
scan parses your source with `ast` (nothing is imported or run), builds the call graph across
modules, classes, inheritance, decorators, `functools.partial`, callbacks and re-exports, and reports:

- **bypass**: a path from an entry point to the sink that avoids every door, with the path;
- **forbidden**: an idiom that reaches code the scan cannot follow on purpose (`eval`, `exec`,
  `importlib` with a computed name, `getattr(obj, name)` with a computed name, ...);
- **unresolved**: a call the scan cannot follow (a receiver of unknown type); never counted as
  passed. A reviewed site can be accepted with its digest (`allow_unresolved`);
- **missing**: a sink, door or allow entry that is not in the scanned code.

Output as text, JSON or SARIF 2.1.0. Standard library only.

It is the path-law check of [invariants-first](../../invariants-py) (`inv scan`, `inv check`,
`inv init`), which reads the rule file's short forms and `pyproject.toml` settings and wires ablation
and confinement into it (`invariants_scan.hooks`). It does not need invariants-first.

## Install

```bash
pip install invariants-scan
```

From this repository: `pip install ./packages/invariants-scan`. Python 3.11 or later.

## Five minutes

```python
import pathlib
import tempfile
import textwrap

import invariants_scan

root = pathlib.Path(tempfile.mkdtemp()) / "src"
files = {
    "shop/__init__.py": "",
    "shop/gateway.py": "def charge(order_id, cents):\n    ...\n",
    "shop/service.py": textwrap.dedent("""
        from shop.gateway import charge

        def checkout(order):
            if order["total"] <= 0:
                raise ValueError("nothing to charge")
            charge(order["id"], order["total"])
        """),
    "shop/admin.py": textwrap.dedent("""
        from shop import gateway

        def refund_fix(order_id):
            gateway.charge(order_id, -500)      # around the door
        """),
}
for name, text in files.items():
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text)

rules = {"rules": [{"id": "PAY-1", "mediation": {"sink": "py:shop.gateway.charge",
                                                  "door": "py:shop.service.checkout"}}]}
report = invariants_scan.scan(rules, [root])
print(invariants_scan.to_text(report))
for f in report.findings:
    print(f.kind, f.rule, f.file, f.line, " -> ".join(f.path or []))
```

A known back door can be frozen in the rule (`"allow": ["py:shop.admin.refund_fix"]`): it passes,
and a new one still fails. `invariants_scan.to_sarif(report)` gives SARIF for code scanning.

The same check from the shell, over a JSON rule document (the `mediation` form above):

```bash
inv-scan rules.json --root src                 # text; --format json|sarif, --output FILE
```

Exit codes: 0 no finding, 1 a bypass, forbidden or missing finding, 2 a usage or rule-document
error, 3 only unresolved findings. A YAML rule file and the short forms (`door:`, `sink:`) are read by
invariants-first's `inv scan`, which runs this same scan.

## Tests

```bash
pip install './packages/invariants-scan[test]'
python -m pytest packages/invariants-scan/tests -q
```

What the scan follows, what it refuses and how to accept a reviewed site are in invariants-first's
[docs/scan.md](../../invariants-py/docs/scan.md). Licensed under Apache-2.0.
