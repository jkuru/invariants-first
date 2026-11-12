"""Imported only with `inv verify --import ledger.extra`: its guard names a rule not in the file."""
import invariants as inv


@inv.guard("L-404")
def audit(entry: str) -> None:
    pass
