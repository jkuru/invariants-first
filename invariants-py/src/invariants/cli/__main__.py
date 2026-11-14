"""`python -m invariants.cli`: the `inv` command.

The working directory runpy put first on sys.path is dropped by invariants/__init__.py, which runs
before this module and before anything a project could shadow is imported (as `python -P` would)."""
from invariants.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
