"""A deterministic stand-in for a model API. It never calls a network.

The client classifies a log excerpt by keywords and answers in JSON, the way a model in
structured-output mode would. `inject(...)` queues bad replies for the next calls, and
`always(...)` makes every reply bad, so tests and demos can drive each failure path.
"""
from __future__ import annotations

import json
import threading
from typing import Any

LOG_MARKER = "--- log ---"
FAULTS = ("malformed", "extra_field", "wrong_enum", "missing_field", "contradict", "abstain")

_KEYWORDS = (
    ("OUT_OF_MEMORY", ("oom-kill", "out of memory", "killed process", "memoryerror")),
    ("TIMEOUT", ("deadline exceeded", "timed out", "timeout")),
    ("BAD_INPUT", ("invalid input", "parse error in input", "malformed record")),
)


def classify(log_excerpt: str) -> str:
    """The cause an honest model would give for this log (only the text after a '--- log ---' marker counts)."""
    text = log_excerpt.rpartition(LOG_MARKER)[2].lower()
    for cause, words in _KEYWORDS:
        if any(w in text for w in words):
            return cause
    return "CANNOT_COMPLY"


class FakeModelClient:
    """Deterministic fake. Thread-safe; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[str] = []
        self._always: str | None = None
        self._lock = threading.Lock()

    def inject(self, *faults: str) -> None:
        """Make the next len(faults) replies bad, one fault each, in order."""
        for f in faults:
            if f not in FAULTS:
                raise ValueError(f"unknown fault {f!r}; pick from {FAULTS}")
        with self._lock:
            self._queue.extend(faults)

    def always(self, fault: str | None) -> None:
        """Make every reply bad with this fault (None turns it off)."""
        if fault is not None and fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}")
        self._always = fault

    def reset(self) -> None:
        with self._lock:
            self.calls, self._queue, self._always = [], [], None

    def complete(self, prompt: str, feedback: str | None = None) -> str:
        """Answer one prompt with a JSON reply (or a deliberately bad one)."""
        with self._lock:
            fault = self._queue.pop(0) if self._queue else self._always
            self.calls.append({"prompt": prompt, "feedback": feedback, "fault": fault})
        cause = classify(prompt)
        reply: dict[str, Any] = {"cause": cause, "summary": f"log points to {cause.lower()}"}
        if fault == "malformed":
            return "Sure! The cause is " + cause
        if fault == "extra_field":
            reply["confidence"] = 0.9
        elif fault == "wrong_enum":
            reply["cause"] = "DISK_FULL"
        elif fault == "missing_field":
            del reply["summary"]
        elif fault == "contradict":
            reply["cause"] = "TIMEOUT" if cause == "OUT_OF_MEMORY" else "OUT_OF_MEMORY"
        elif fault == "abstain":
            reply["cause"] = "CANNOT_COMPLY"
        return json.dumps(reply)
