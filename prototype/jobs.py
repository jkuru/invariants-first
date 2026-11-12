"""Example target for R-1 (guide 4.2): split_work, a pure function, so its post-effect guard enforces."""
import invariants as inv

def _allocate(total, caps):
    """Largest-remainder split proportional to capacity, in integer arithmetic."""
    cap_sum = sum(caps)
    if not 0 <= total <= cap_sum: raise ValueError("total_jobs must be within [0, sum(capacities)]")
    if cap_sum == 0: return [0] * len(caps)
    shares = [total * c // cap_sum for c in caps]
    order = sorted(range(len(caps)), key=lambda i: -(total * caps[i] % cap_sum))
    for i in order[: total - sum(shares)]: shares[i] += 1
    return shares

def _allocate_rounding_bug(total, caps):
    """Seeded mutant: round() each share independently ("a rounding bug", R-1's why)."""
    cap_sum = sum(caps)
    return [round(total * c / cap_sum) for c in caps] if cap_sum else [0] * len(caps)

@inv.guard("R-1")
def split_work(total_jobs: int, capacities: list[int]) -> list[int]:
    return _allocate(total_jobs, capacities)

RESTARTED = []

@inv.guard("P-2")
def restart_job(job_id: str) -> None:
    """effects: external (targets: map), so only pre-effect rules may enforce here (2.2)."""
    RESTARTED.append(job_id)
