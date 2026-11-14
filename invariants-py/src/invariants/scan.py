"""The static scan, as `invariants.scan`: the public API of the invariants-scan library
(`invariants_scan`), re-exported. The scan is its own distribution: import `invariants_scan` to use it
without the rest of invariants-first (`inv scan` is the command over it)."""
from invariants_scan import *  # noqa: F401,F403
from invariants_scan import __all__ as _names

__all__ = list(_names)
