"""fact-pipeline: text documents in, cited facts out, guarded by invariants.yaml."""
from __future__ import annotations

import os
import pathlib

__version__ = "0.1.0"


def rules_path() -> pathlib.Path:
    """The rule file: $FACT_PIPELINE_RULES, else invariants.yaml at the project root."""
    env = os.environ.get("FACT_PIPELINE_RULES")
    return pathlib.Path(env) if env else pathlib.Path(__file__).resolve().parents[2] / "invariants.yaml"
