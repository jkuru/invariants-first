"""The CEL engine, as `invariants.cel` (`inv.cel`): the public API of the invariants-cel library
(`invariants_cel`), re-exported. The engine is its own distribution: import `invariants_cel` to use it
without the rest of invariants-first."""
from invariants_cel import *  # noqa: F401,F403
from invariants_cel import __all__ as _names

__all__ = list(_names)
