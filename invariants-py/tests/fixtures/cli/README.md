Two small example apps for the `inv` CLI tests. Each is a self-contained project with its own
rule file; the library never imports them, and the tests only import them in a subprocess.

- `clean/` (package `ledger`): every write to `store.write_balance` goes through the guarded door
  `service.withdraw`. `inv scan` must pass it and `inv status` reports L-1 held.
- `bypass/` (package `fleet`): `retry.py` calls the sink `store.set_size` directly, and `ops.py`
  uses forbidden idioms (getattr on the sink name, raw SQL on `pools`, `__wrapped__`) plus calls
  the scan cannot resolve. `inv scan` must catch every one.
