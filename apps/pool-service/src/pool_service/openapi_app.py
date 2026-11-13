"""The app the pool_api contract reads (`{type: openapi, app: "pool_service.openapi_app:app"}`).

The same routes as `pool_service.main:app`, built on an in-memory database and without loading the rule
file, so `inv lock` can import it and call `app.openapi()` with no file or environment side effects.
"""
from .api import create_app

app = create_app(":memory:", load=False)
