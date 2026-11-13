"""ASGI entrypoint: `uvicorn pool_service.main:app` (POOL_SERVICE_DB, POOL_SERVICE_RULES from the env).

APP_MODEL (fake by default; anthropic, record or replay with APP_TRACE) picks triage's model: see llm.py.
"""
from . import llm, triage
from .api import create_app

triage.set_client(llm.from_env())
app = create_app()
