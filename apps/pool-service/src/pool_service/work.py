"""Split a batch of jobs across workers in proportion to their capacity (R-1, a pure function)."""
from __future__ import annotations

import invariants as inv


class NotEnoughCapacity(ValueError):
    """total_jobs is more than the workers can take."""


def _allocate(total_jobs: int, capacities: list[int]) -> list[int]:
    """Largest-remainder apportionment, capped by each capacity. Exact integer arithmetic."""
    cap = sum(capacities)
    if total_jobs < 0 or any(c < 0 for c in capacities):
        raise ValueError("total_jobs and capacities must be non-negative")
    if total_jobs > cap:
        raise NotEnoughCapacity(f"{total_jobs} jobs but only {cap} slots")
    if cap == 0:
        return [0] * len(capacities)
    shares = [total_jobs * c // cap for c in capacities]
    rems = [total_jobs * c % cap for c in capacities]
    left = total_jobs - sum(shares)
    for i in sorted(range(len(capacities)), key=lambda i: (-rems[i], i))[:left]:
        shares[i] += 1
    return shares


@inv.guard("R-1")
def split_work(total_jobs: int, capacities: list[int]) -> list[int]:
    """One share per worker; shares sum to total_jobs and never exceed a worker's capacity."""
    return _allocate(total_jobs, capacities)
