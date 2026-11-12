Deliberate R-23 / R-20 bypasses. Not part of the package and never imported by it.

The clean scan in ci.sh uses `--root src` only, so this directory is excluded.
`tests/test_scan.py` scans `--root src --root bypass` and asserts every finding below.
Module names resolve as `pool_service.rogue`, so the scan sees these as calls into the real package.
