"""A deterministic stand-in for the extraction and answer model. It never calls a real API.

The "model" reads a sentence with a few fixed patterns. A fault switch makes it return bad
replies: each injected fault fires on the next calls it applies to, `times` times.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any, Protocol

NAME = r"[A-Z][A-Za-z]+(?: (?:Corp|Ltd|Inc|Group))?"
PERSON = r"[A-Z][a-z]+ [A-Z][a-z]+"
_ACQ = re.compile(rf"(?P<s>{NAME})(?: said \w+(?: it)?)? completed its acquisition of (?P<o>{NAME})")
_PRICE = re.compile(r"for (?P<q>about |up to |at least )?\$(?P<n>\d+(?:\.\d+)?) (?P<u>billion|million)")
_DENY = re.compile(rf"(?P<s>{NAME}) had denied talks(?: in (?P<y>\d{{4}}))?")
_CEO = re.compile(rf"(?P<p>{PERSON}) (?:is|became) (?:the )?CEO of (?P<o>{NAME})(?: (?:in|since) (?P<y>\d{{4}}))?")
_HQ = re.compile(rf"(?P<s>{NAME}) is headquartered in (?P<o>[A-Z][a-z]+)")
_QUAL = {"about ": "APPROX", "up to ": "UPPER_BOUND", "at least ": "LOWER_BOUND", "": "EXACT"}
_UNIT = {"billion": 1_000_000_000, "million": 1_000_000}

EXTRACT_FAULTS = ("misquote", "drop_qualifier", "model_id", "bad_predicate", "garbage", "inverted_interval",
                  "empty_quote", "abstain")
ANSWER_FAULTS = ("uncited_chunk", "answer_garbage", "no_citation")
FAULTS = EXTRACT_FAULTS + ANSWER_FAULTS


class ModelClient(Protocol):
    """What the pipeline needs from a model: a JSON reply for a task, optionally with parser feedback."""

    def complete(self, task: dict[str, Any], feedback: str | None = None) -> str: ...


def _fact(subject: str, predicate: str, obj: str, kind: str, quote_span: tuple[int, int], text: str,
          qualifier: str = "NONE", polarity: str = "ASSERTED", valid_from: str | None = None,
          valid_to: str | None = None) -> dict[str, Any]:
    s, e = quote_span
    return {"subject": subject, "predicate": predicate, "object": obj, "object_kind": kind, "qualifier": qualifier,
            "polarity": polarity, "quote": text[s:e], "start": s, "end": e, "valid_from": valid_from,
            "valid_to": valid_to}


def read_facts(text: str) -> list[dict[str, Any]]:
    """Every fact the patterns find in one sentence, in reading order."""
    out: list[tuple[int, dict[str, Any]]] = []
    acq = _ACQ.search(text)
    if acq:
        out.append((acq.start(), _fact(acq["s"], "acquired", acq["o"], "entity", (acq.start(), acq.end()), text)))
        price = _PRICE.search(text, acq.end())
        if price:
            q = price["q"] or ""
            value = int(round(float(price["n"]) * _UNIT[price["u"]]))
            start = price.start("q") if q else price.start() + len("for ")
            out.append((price.start(), _fact(acq["o"], "acquisition_price", f"USD {value}", "literal",
                                             (start, price.end()), text, qualifier=_QUAL[q])))
    for m in _DENY.finditer(text):
        y = m["y"]
        out.append((m.start(), _fact(m["s"], "in_talks", "acquisition talks", "literal", (m.start(), m.end()), text,
                                     polarity="DENIED", valid_from=f"{y}-01-01" if y else None,
                                     valid_to=f"{y}-12-31" if y else None)))
    for m in _CEO.finditer(text):
        y = m["y"]
        out.append((m.start(), _fact(m["o"], "ceo_of", m["p"], "entity", (m.start(), m.end()), text,
                                     valid_from=f"{y}-01-01" if y else None)))
    for m in _HQ.finditer(text):
        out.append((m.start(), _fact(m["s"], "headquartered_in", m["o"], "literal", (m.start(), m.end()), text)))
    return [f for _, f in sorted(out, key=lambda p: p[0])]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 3}


class FakeModel:
    """Deterministic model client. inject(fault, times) makes the next applicable replies bad."""

    def __init__(self, faults: dict[str, int] | None = None) -> None:
        self.faults: dict[str, int] = {}
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        for name, times in (faults or {}).items():
            self.inject(name, times)

    def inject(self, fault: str, times: int = 1) -> FakeModel:
        """Arm a fault for the next `times` replies it applies to."""
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}; choose from {', '.join(FAULTS)}")
        self.faults[fault] = self.faults.get(fault, 0) + times
        return self

    def _take(self, fault: str) -> bool:
        if self.faults.get(fault, 0) > 0:
            self.faults[fault] -= 1
            return True
        return False

    def complete(self, task: dict[str, Any], feedback: str | None = None) -> str:
        """Reply to an extract or answer task with JSON text."""
        with self._lock:
            self.calls.append({"task": task.get("task"), "index": task.get("index"), "feedback": feedback})
            if task.get("task") == "extract":
                return self._extract(task["text"], int(task["index"]))
            if task.get("task") == "answer":
                return self._answer(task["question"], task["chunks"])
            raise ValueError(f"unknown task {task.get('task')!r}")

    async def acomplete(self, task: dict[str, Any], feedback: str | None = None) -> str:
        """Async form of complete (yields to the loop once, like a network call)."""
        await asyncio.sleep(0)
        return self.complete(task, feedback)

    def _extract(self, text: str, index: int) -> str:
        facts = read_facts(text)
        if index >= len(facts):
            return json.dumps(_fact("", "NONE", "", "literal", (0, 0), text))
        f = dict(facts[index])
        if self._take("garbage"):
            return "Sure! Here is the fact you asked for: " + f["quote"]
        if self._take("abstain"):
            return json.dumps({**f, "predicate": "CANNOT_COMPLY"})
        if self._take("model_id"):
            return json.dumps({**f, "id": "Q4115189"})
        if self._take("bad_predicate"):
            return json.dumps({**f, "predicate": "owns"})
        if f["predicate"] == "acquisition_price" and f["qualifier"] == "APPROX" and self._take("drop_qualifier"):
            # the guide's 7.6 slip: quotes "for $2.1 billion", which is not what the source says there
            value = f["quote"].replace("about ", "")
            f = {**f, "quote": "for " + value, "qualifier": "EXACT"}
        elif self._take("misquote"):
            f = {**f, "start": f["start"] + 1, "end": f["end"] + 1}
        if self._take("empty_quote"):
            f = {**f, "quote": "", "start": 0, "end": 0}
        if self._take("inverted_interval"):
            f = {**f, "valid_from": "2024-06-30", "valid_to": "2023-01-01"}
        return json.dumps(f)

    def _answer(self, question: str, chunks: list[dict[str, str]]) -> str:
        if self._take("answer_garbage"):
            return "I think the answer is yes."
        q = _words(question)
        scored = sorted(((len(q & _words(c["text"])), i) for i, c in enumerate(chunks)), key=lambda p: (-p[0], p[1]))
        if not scored or scored[0][0] == 0:
            return json.dumps({"status": "CANNOT_ANSWER", "text": "", "citations": []})
        best = chunks[scored[0][1]]
        cites = [best["id"]]
        if self._take("uncited_chunk"):
            cites.append("src_000000000000:deadbeef:99")
        if self._take("no_citation"):
            cites = []
        return json.dumps({"status": "ANSWERED", "text": best["text"], "citations": cites})
