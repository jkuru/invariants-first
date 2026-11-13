"""OpenTelemetry: one span per evaluation, named invariant.<point>, with invariant.* attributes.

    from invariants.integrations import otel
    inv.configure(exporter=otel.exporter())          # needs the `otel` extra (opentelemetry-api)

Attributes: invariant.rule_id, rule_rev, outcome, mode, point, timing, action, attempts. Clause
messages and error text are not exported (they can carry values). gen_ai.* mapping is not done.

A span starts when the check began (a guard's pre- or post-effect check phase, providers
included; a model call's first attempt, so retries are inside it; an edge call) and ends when the
outcome was recorded. A violation, rule_error or parse_error sets the span status to ERROR (with
the outcome kind as its description); ok leaves it unset.
"""
from __future__ import annotations

import importlib

from typing import Any, Callable

Exporter = Callable[[str, str, dict[str, Any]], None]
FIELDS = ("rev", "outcome", "mode", "point", "timing", "action", "attempts")
ERRORS = frozenset({"violation", "rule_error", "parse_error"})


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
    trace: Any = importlib.import_module("opentelemetry.trace")
    if tracer is None:
        tracer = trace.get_tracer("invariants")

    def export(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        name = f"invariant.{event.get('point') or 'evaluate'}"
        start, end = event.get("start_ns"), event.get("end_ns")
        span = tracer.start_span(name, start_time=start) if isinstance(start, int) else tracer.start_span(name)
        try:
            span.set_attributes(attributes(event))
            if event.get("outcome") in ERRORS:
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(event.get("outcome"))))
        finally:
            if isinstance(end, int):
                span.end(end_time=end)
            else:
                span.end()
    return export


__all__ = ["exporter", "attributes"]
