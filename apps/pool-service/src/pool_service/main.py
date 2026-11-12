"""ASGI entrypoint: `uvicorn pool_service.main:app` (POOL_SERVICE_DB, POOL_SERVICE_RULES from the env)."""
from .api import create_app

app = create_app()
