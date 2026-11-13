"""Ablation state outside any one block: INV_ABLATE (honoured only for test runs), and the rules ablated
in every registry or in this context only. The blocks that set them are Registry.ablate,
ablated_everywhere and the module-level ablated()."""
from __future__ import annotations

import collections
import contextvars
import logging
import os
import sys
from typing import TYPE_CHECKING

from .events import log
from .session import ABLATE_ARM_ENV, ABLATE_ENV, _SESSION, under_pytest

if TYPE_CHECKING:
    from . import Registry


def _has_real_handler(lg: logging.Logger) -> bool:
    """True if a record on lg reaches a handler other than a NullHandler (lastResort is off then)."""
    c: logging.Logger | None = lg
    while c is not None:
        if any(not isinstance(h, logging.NullHandler) for h in c.handlers):
            return True
        c = c.parent if c.propagate else None
    return False


def ablation_level() -> int:
    """Log level for honoured env ablation: INFO under pytest, ERROR anywhere else (guards are off)."""
    return logging.INFO if under_pytest() else logging.ERROR


def env_ablated() -> set[str]:
    """Rules named in INV_ABLATE, honoured only when INV_ABLATE_TESTING=1 too; else empty (and a warning).

    Honoured outside pytest (an `inv scan` or app subprocess of a falsifier), it logs at ERROR once
    per rule set: in production it means those rules' in-process guards are off.
    """
    names = set(filter(None, (x.strip() for x in os.environ.get(ABLATE_ENV, "").split(","))))
    if not names:
        return set()
    if os.environ.get(ABLATE_ARM_ENV) != "1":
        if _SESSION.first_unarmed():
            log.warning("invariants: %s=%s is ignored: ablation through the environment needs %s=1 "
                        "(set by the pytest plugin)", ABLATE_ENV, ",".join(sorted(names)), ABLATE_ARM_ENV)
        return set()
    key = ",".join(sorted(names))
    if not under_pytest() and _SESSION.first_armed(key):
        _warn_guards_off(key)
    return names


def _warn_guards_off(key: str) -> None:
    """Say, at ERROR (and on stderr when the app set no logging), that key's guards are off here."""
    log.error("invariants: %s=%s with %s=1: the in-process guards of these rules are OFF in this "
              "process (pid %d). This is for test runs only; unset both outside tests.",
              ABLATE_ENV, key, ABLATE_ARM_ENV, os.getpid())
    if not _has_real_handler(log) or not log.isEnabledFor(logging.ERROR):
        try:                                 # the app set no logging: say it on stderr anyway
            sys.stderr.write(f"invariants: ERROR: {ABLATE_ENV}={key} with {ABLATE_ARM_ENV}=1: the "
                             f"in-process guards of these rules are OFF in this process "
                             f"(pid {os.getpid()}). Unset both outside tests.\n")
        except Exception:
            pass


GLOBAL_ABLATED: collections.Counter[str] = collections.Counter()   # rules ablated in every registry
# (registry, rule id) pairs ablated in this context only: inv.ablated()'s default scope
_CTX_ABLATED: contextvars.ContextVar[tuple[tuple[Registry, str], ...]] = contextvars.ContextVar(
    "inv_ctx_ablated", default=())
