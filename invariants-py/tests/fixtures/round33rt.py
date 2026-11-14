"""Fixture app for the round-33 runtime tests: targetless rules over a plain and a FastAPI guard, model
calls that share one chat model (one nested in the other's body), and a provider read with
unhashable arguments."""
from __future__ import annotations

import dataclasses
from typing import Any

import invariants as inv
from invariants.integrations.anthropic import ChatModel

M = "py:round33rt"


# --- targetless rules with a decorator and a middleware enforcer -----------------------------------
@inv.guard("T-1")
def plain(x: int) -> int:
    return x


def targetless_doc(points: tuple[str, ...] = ("decorator", "middleware"),
                   subject: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"rules": [{"id": "T-1", "status": "detect", "bad": "args.x < 0",
                       **({"subject": subject} if subject is not None else {}),
                       "enforcers": [{"point": p, "timing": "pre_effect", "mode": "detect"} for p in points]}]}


# --- one chat model, a model call nested in another's body -------------------------------------------
@dataclasses.dataclass(frozen=True)
class Answer:
    text: str


class Scripted(ChatModel):
    """A chat model answering from a script: each request's messages and the reply sent are kept."""

    def __init__(self) -> None:
        super().__init__()
        self.script: list[str] = []
        self.requests: list[list[Any]] = []

    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[Any, Any]:
        self.requests.append(request["messages"])
        reply = self.script.pop(0)
        return reply, reply


LLM = Scripted()


@inv.model_call("N-inner", schema=Answer)
def summarize(text: str, inv_feedback: str | None = None) -> inv.Checked:
    return LLM.complete("summarize: " + text, inv_feedback)    # type: ignore[return-value]


@inv.model_call("N-outer", schema=Answer)
def answer(q: str, inv_feedback: str | None = None) -> inv.Checked:
    summarize(q)                                     # the same model asked something else first
    return LLM.complete("answer: " + q, inv_feedback)    # type: ignore[return-value]


@inv.model_call("N-inner-a", schema=Answer)
async def asummarize(text: str, inv_feedback: str | None = None) -> inv.Checked:
    return await LLM.acomplete("summarize: " + text, inv_feedback)    # type: ignore[return-value]


@inv.model_call("N-outer-a", schema=Answer)
async def aanswer(q: str, inv_feedback: str | None = None) -> inv.Checked:
    await asummarize(q)
    return await LLM.acomplete("answer: " + q, inv_feedback)    # type: ignore[return-value]


def _model_rule(rid: str, target: str) -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": target}, "bad": "reply.text == 'bad'",
            "model_policy": {"schema": "Answer", "format_retries": 2, "semantic_resamples": 0},
            "falsified_by": ["t::x"],
            "enforcers": [{"point": "model_call", "timing": "post_effect", "mode": "enforce",
                           "on_violation": "reject"}]}


def nested_doc(asynchronous: bool = False) -> dict[str, Any]:
    inner, outer = ("asummarize", "aanswer") if asynchronous else ("summarize", "answer")
    sfx = "-a" if asynchronous else ""
    return {"targets": {f"{M}.{inner}": {"effects": "none"}, f"{M}.{outer}": {"effects": "none"}},
            "rules": [_model_rule(f"N-inner{sfx}", f"{M}.{inner}"), _model_rule(f"N-outer{sfx}", f"{M}.{outer}")]}


# --- a provider read with an unhashable argument -------------------------------------------------------
@inv.provider("total")
def total(xs: list[int]) -> int:
    return sum(xs)


PROVIDER_DOC: dict[str, Any] = {"providers": {"total": {"from": f"{M}.total", "type": "int",
                                                        "args": {"xs": "src"}}}, "rules": []}
