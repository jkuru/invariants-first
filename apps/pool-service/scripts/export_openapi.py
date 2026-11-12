"""Write openapi.json (the pool_api contract that invariants.lock pins). Run after any API change."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

from pool_service.api import create_app

OUT = pathlib.Path(__file__).resolve().parents[1] / "openapi.json"


def render() -> str:
    with tempfile.TemporaryDirectory() as d:
        app = create_app(pathlib.Path(d) / "openapi.db", load=False)
        return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(render())
    print(f"wrote {OUT}", file=sys.stderr)
