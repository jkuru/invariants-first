"""Optional integrations: pydantic (closed schemas, contracts) and ASGI/FastAPI (HTTP status mapping).

Each submodule imports its framework only when it is imported itself.
"""
from __future__ import annotations

__all__ = ["pydantic", "asgi", "fastapi"]
