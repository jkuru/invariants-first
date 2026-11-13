"""Optional integrations: pydantic (closed schemas, contracts), ASGI/FastAPI (HTTP status mapping),
OpenTelemetry (spans), Anthropic (a real model for model_call bodies, with replay and recording) and
exporters (BufferedExporter: a host-started thread hands events to a slow exporter).

Each submodule imports its framework only when it is imported itself.
"""
from __future__ import annotations

__all__ = ["pydantic", "asgi", "fastapi", "otel", "anthropic", "exporters"]
