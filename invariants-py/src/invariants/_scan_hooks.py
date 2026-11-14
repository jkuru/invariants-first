"""The scan's view of ablation and confinement, wired by the package (`invariants/__init__.py`), so
`invariants.scan` never imports the registry or the confinement: this module and scan/ import nothing
else of the package.

ablated(): the rules turned off for an ablation run (INV_ABLATE when armed, and in-process
ablated_everywhere). reached(rule_id): note in the ablation trace that the scan skipped that rule.
confine_root(): the directory the walk of the source roots is confined to (confinement.confine_root),
or None. Importing any `invariants` submodule runs the package first, so the wiring is always in place;
were it not, the defaults ablate nothing, so every rule is scanned, and confine nothing.
"""
from __future__ import annotations

import pathlib
from typing import Callable


def _none() -> set[str]:
    return set()


def _ignore(rule_id: str) -> None:
    return None


ablated: Callable[[], set[str]] = _none
reached: Callable[[str], None] = _ignore


def _unconfined() -> pathlib.Path | None:
    return None


confine_root: Callable[[], pathlib.Path | None] = _unconfined
