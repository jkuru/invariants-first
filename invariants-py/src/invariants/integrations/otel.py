"""OpenTelemetry: one span per evaluation, named invariant.<point>, with invariant.* attributes.

    from invariants.integrations import otel
    inv.configure(exporter=otel.exporter())          # needs the `otel` extra (opentelemetry-api)

Attributes: invariant.rule_id, rule_rev, outcome, mode, point, timing, action, attempts. Clause
messages and error text are not exported (they can carry values). gen_ai.* mapping is not done.
"""
from __future__ import annotations

import importlib

from typing import Any, Callable

Exporter = Callable[[str, str, dict[str, Any]], None]
FIELDS = ("rev", "outcome", "mode", "point", "timing", "action", "attempts")


def attributes(event: dict[str, Any]) -> dict[str, Any]:
    """The span attributes for one event (None values left out)."""
    out: dict[str, Any] = {"invariant.rule_id": event.get("rule")}
    for k in FIELDS:
        v = event.get(k)
        if v is not None:
            out["invariant.rule_rev" if k == "rev" else f"invariant.{k}"] = v
    return out


def exporter(tracer: Any = None) -> Exporter:
    """An exporter for inv.configure(exporter=...). tracer: an OpenTelemetry tracer (default: the global one)."""
    if tracer is None:
        trace: Any = importlib.import_module("opentelemetry.trace")
        tracer = trace.get_tracer("invariants")

    def export(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        span = tracer.start_span(f"invariant.{event.get('point') or 'evaluate'}")
        try:
            span.set_attributes(attributes(event))
        finally:
            span.end()
    return export


__all__ = ["exporter", "attributes"]
