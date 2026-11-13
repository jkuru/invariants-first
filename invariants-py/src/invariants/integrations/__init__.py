"""Optional integrations: pydantic (closed schemas, contracts), ASGI/FastAPI (HTTP status mapping),
OpenTelemetry (spans) and Anthropic (a real model for model_call bodies, with replay and recording).

Each submodule imports its framework only when it is imported itself.
"""
from __future__ import annotations

__all__ = ["pydantic", "asgi", "fastapi", "otel", "anthropic"]
