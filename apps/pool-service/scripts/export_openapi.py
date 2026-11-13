"""Print the app's OpenAPI document (or write it to the file given), for reading.

The pool_api contract reads the live app (pool_service.openapi_app:app), so no file is pinned and none
needs regenerating: run `inv lock` after an API change.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

from pool_service.api import create_app


def render() -> str:
    with tempfile.TemporaryDirectory() as d:
        app = create_app(pathlib.Path(d) / "openapi.db", load=False)
        return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    if len(sys.argv) > 1:
        pathlib.Path(sys.argv[1]).write_text(render())
    else:
        sys.stdout.write(render())
