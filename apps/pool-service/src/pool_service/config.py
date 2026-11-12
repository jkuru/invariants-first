"""Where the rule file and the database live."""
from __future__ import annotations

import os
import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parents[2]


def rules_path() -> pathlib.Path:
    """POOL_SERVICE_RULES, else invariants.yaml at the project root."""
    return pathlib.Path(os.environ.get("POOL_SERVICE_RULES") or APP_ROOT / "invariants.yaml")


def db_path() -> pathlib.Path:
    """POOL_SERVICE_DB, else pool-service.db in the working directory."""
    return pathlib.Path(os.environ.get("POOL_SERVICE_DB") or "pool-service.db")
