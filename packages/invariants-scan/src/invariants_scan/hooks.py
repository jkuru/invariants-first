"""What the scan reads from its host: ablation and confinement. The defaults ablate nothing (every rule
is scanned) and confine nothing; a host replaces them. invariants-first wires them in
`invariants/__init__.py`, so the scan never imports its registry or confinement.

ablated(): the rules turned off for an ablation run (in invariants-first: INV_ABLATE when armed, and
in-process ablated_everywhere). reached(rule_id): note that the scan skipped that rule (the ablation
trace). confine_root(): the directory the walk of the source roots is confined to, or None. The scan
calls them on every run, so a host may set them at any time before it scans.
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
