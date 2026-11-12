Deliberate R-23 / R-20 bypasses. Not part of the package and never imported by it.

The clean scan in ci.sh uses `--root src` only, so this directory is excluded.
`tests/test_scan.py` scans `--root src --root bypass` and asserts every finding below.
Raw SQL to the tables (an `UPDATE pools` or a `DROP TRIGGER`) is below the API: no rule names it,
so it is not here. The database's own permissions are what stop it.
Module names resolve as `pool_service.rogue`, so the scan sees these as calls into the real package.
