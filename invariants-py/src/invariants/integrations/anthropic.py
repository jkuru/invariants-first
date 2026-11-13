"""Anthropic models for an @inv.model_call body, with deterministic replay and recording.

    from invariants.integrations.anthropic import AnthropicModel

    llm = AnthropicModel(system="Answer with one JSON object and nothing else.")   # needs the `anthropic` extra

    @inv.model_call("R-7", schema=CauseReply, returns=CheckedCause)
    def propose_cause(job_id: str, log: str, inv_feedback: str | None = None) -> str:
        return llm.complete(prompt_for(job_id, log), inv_feedback)

One model_call invocation, with its format retries, is one conversation. inv.model_call calls the
body again with the same arguments on every attempt and sets inv_feedback to the parser error
after a format failure, and to None on the first attempt and on a semantic resample (a fresh
sample that never sees the rule text). So `complete(prompt, None)` opens a new conversation, and
`complete(prompt, feedback)` sends the feedback as a follow-up user turn after the model's previous
reply in the current one. The current conversation is context-local (a contextvar), so threads and
asyncio tasks each keep their own; call the model from the body itself (an `async def` body uses
`acomplete`), not from a worker thread the body starts.

ReplayModel answers from a JSON trace and fails loudly, with a diff, when a request differs from
the recorded one. RecordingModel wraps a real model and writes such a trace. A trace is

    {"fixture": "recorded" | "hand-written", "model": "...",
     "calls": [{"request": {"system": "...", "messages": [...]}, "reply": "..."}]}

The `anthropic` package is imported only when an AnthropicModel without a client makes its first
request; ReplayModel and RecordingModel over a stub need nothing beyond the library.
"""
from __future__ import annotations

import contextvars
import copy
import difflib
import importlib
import itertools
import json
import os
import pathlib
import threading
from collections.abc import Sequence
from typing import Any

__all__ = ["ChatModel", "AnthropicModel", "ReplayModel", "RecordingModel", "ReplayMismatch", "ConversationError",
           "ModelRefusal", "model_for", "MODES", "DEFAULT_MODEL", "FEEDBACK_TEMPLATE"]

DEFAULT_MODEL = "claude-haiku-5-5"
FEEDBACK_TEMPLATE = "That reply could not be used: {feedback}\nReply again with only the corrected JSON object."
MODES = ("replay", "record", "anthropic", "fake")

Message = dict[str, Any]
Prompt = str | Sequence[Message]


class ConversationError(RuntimeError):
    """complete() was given feedback with no conversation to continue, or for another prompt."""


class ModelRefusal(RuntimeError):
    """The model declined the request (stop_reason "refusal")."""


class ReplayMismatch(AssertionError):
    """A ReplayModel request that no recorded call matches (the message carries a diff)."""


class _Conversation:
    """One logical model call: its messages as a trace records them (assistant turns as text) and as
    the API is sent them (assistant turns as the content blocks the API returned)."""

    __slots__ = ("opening", "messages", "wire")

    def __init__(self, opening: list[Message]) -> None:
        self.opening = opening
        self.messages: list[Message] = copy.deepcopy(opening)
        self.wire: list[Any] = copy.deepcopy(opening)


_CONVERSATIONS: contextvars.ContextVar[dict[int, _Conversation]] = contextvars.ContextVar(
    "invariants_anthropic_conversations", default={})
_IDS = itertools.count(1)


def _opening(prompt: Prompt) -> list[Message]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    msgs = [dict(m) for m in prompt]
    if not msgs or any(not isinstance(m.get("role"), str) or "content" not in m for m in msgs):
        raise TypeError("prompt must be a string or a non-empty list of {role, content} messages")
    if msgs[-1]["role"] != "user":
        raise TypeError("a prompt given as messages must end with a user message")
    return msgs


class ChatModel:
    """The conversation logic shared by the three models. Subclasses send one request."""

    def __init__(self, *, system: str | None = None, feedback_template: str = FEEDBACK_TEMPLATE) -> None:
        self.system = system
        self.feedback_template = feedback_template
        self._key = next(_IDS)

    # --- what a model_call body calls ------------------------------------------------------------
    def complete(self, prompt: Prompt, feedback: str | None = None) -> str:
        """The model's reply text. feedback None opens a new conversation; a feedback text is sent as
        the next user turn of the current one."""
        conv, request = self._turn(prompt, feedback)
        text, wire = self._send(request, conv.wire)
        self._answered(conv, text, wire)
        return text

    async def acomplete(self, prompt: Prompt, feedback: str | None = None) -> str:
        """complete() for an async def body."""
        conv, request = self._turn(prompt, feedback)
        text, wire = await self._asend(request, conv.wire)
        self._answered(conv, text, wire)
        return text

    # --- for subclasses --------------------------------------------------------------------------
    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[str, Any]:
        """(reply text, the assistant content to send back on a follow-up turn)."""
        raise NotImplementedError

    async def _asend(self, request: dict[str, Any], wire: list[Any]) -> tuple[str, Any]:
        return self._send(request, wire)

    # --- the conversation ------------------------------------------------------------------------
    def _turn(self, prompt: Prompt, feedback: str | None) -> tuple[_Conversation, dict[str, Any]]:
        opening = _opening(prompt)
        convs = _CONVERSATIONS.get()
        if feedback is None:
            conv = _Conversation(opening)
            _CONVERSATIONS.set({**convs, self._key: conv})      # copy on write: sibling contexts keep theirs
        else:
            got = convs.get(self._key)
            if got is None:
                raise ConversationError("feedback given with no conversation to continue: call complete(prompt) "
                                        "first, from the same thread or task")
            if got.opening != opening:
                raise ConversationError("feedback given for another prompt than the current conversation's")
            conv = got
            turn = {"role": "user", "content": self.feedback_template.format(feedback=feedback)}
            conv.messages.append(turn)
            conv.wire.append(dict(turn))
        request: dict[str, Any] = {}
        if self.system is not None:
            request["system"] = self.system
        request["messages"] = copy.deepcopy(conv.messages)
        return conv, request

    @staticmethod
    def _answered(conv: _Conversation, text: str, wire: Any) -> None:
        conv.messages.append({"role": "assistant", "content": text})
        conv.wire.append({"role": "assistant", "content": wire})


class AnthropicModel(ChatModel):
    """A Claude model through the Messages API. client: an anthropic.Anthropic (or a stub with
    .messages.create); by default one is made on the first request (needs the `anthropic` extra and
    credentials, such as ANTHROPIC_API_KEY). async_client is used by acomplete.

    params are passed to messages.create unchanged (for example output_config)."""

    def __init__(self, model: str = DEFAULT_MODEL, *, system: str | None = None, max_tokens: int = 16000,
                 client: Any = None, async_client: Any = None, feedback_template: str = FEEDBACK_TEMPLATE,
                 **params: Any) -> None:
        super().__init__(system=system, feedback_template=feedback_template)
        self.model, self.max_tokens, self.params = model, max_tokens, params
        self._client, self._async_client = client, async_client
        self._lock = threading.Lock()

    def _sdk(self) -> Any:
        try:
            return importlib.import_module("anthropic")
        except ImportError:
            raise ImportError("AnthropicModel needs the anthropic package: pip install 'invariants-first[anthropic]'"
                              ) from None

    def client(self) -> Any:
        with self._lock:
            if self._client is None:
                self._client = self._sdk().Anthropic()
            return self._client

    def async_client(self) -> Any:
        with self._lock:
            if self._async_client is None:
                self._async_client = self._sdk().AsyncAnthropic()
            return self._async_client

    def _kwargs(self, request: dict[str, Any], wire: list[Any]) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": list(wire),
                              **self.params}
        if "system" in request:
            kw["system"] = request["system"]
        return kw

    def _reply(self, response: Any) -> tuple[str, Any]:
        if getattr(response, "stop_reason", None) == "refusal":
            raise ModelRefusal(f"{self.model} declined the request")
        content = list(getattr(response, "content", None) or [])
        text = "".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
        return text, content                # the blocks go back unchanged (thinking blocks included)

    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[str, Any]:
        return self._reply(self.client().messages.create(**self._kwargs(request, wire)))

    async def _asend(self, request: dict[str, Any], wire: list[Any]) -> tuple[str, Any]:
        return self._reply(await self.async_client().messages.create(**self._kwargs(request, wire)))


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)


def _matches(expected: dict[str, Any], request: dict[str, Any]) -> bool:
    """The recorded request's keys (messages, and system when recorded) equal this request's."""
    return all(request.get(k) == v for k, v in expected.items())


class ReplayModel(ChatModel):
    """Replies from a trace file, matched on the request. A request no unused recorded call matches
    raises ReplayMismatch with a diff against the next unused call. Thread-safe."""

    def __init__(self, path: str | os.PathLike[str], *, system: str | None = None,
                 feedback_template: str = FEEDBACK_TEMPLATE) -> None:
        super().__init__(system=system, feedback_template=feedback_template)
        self.path = pathlib.Path(path)
        try:
            trace = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(f"no replay trace at {self.path} (record one with a RecordingModel)") from None
        if not isinstance(trace, dict) or not isinstance(trace.get("calls"), list):
            raise ValueError(f"{self.path}: a trace is an object with a 'calls' list")
        if trace.get("fixture") not in ("recorded", "hand-written"):
            raise ValueError(f"{self.path}: 'fixture' must be 'recorded' or 'hand-written'")
        for i, c in enumerate(trace["calls"]):
            if not (isinstance(c, dict) and isinstance(c.get("request"), dict)
                    and isinstance(c["request"].get("messages"), list) and isinstance(c.get("reply"), str)):
                raise ValueError(f"{self.path}: call {i} needs a request with messages and a string reply")
        self.fixture: str = trace["fixture"]
        self.model: str | None = trace.get("model")
        self.calls: list[dict[str, Any]] = trace["calls"]
        self.used: list[bool] = [False] * len(self.calls)
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        """Recorded calls not replayed yet."""
        return self.used.count(False)

    def assert_done(self) -> None:
        """ReplayMismatch unless every recorded call was replayed."""
        if self.remaining:
            left = [i for i, u in enumerate(self.used) if not u]
            raise ReplayMismatch(f"{self.path}: {len(left)} recorded call(s) never requested: {left}")

    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[str, Any]:
        with self._lock:
            unused = [i for i, u in enumerate(self.used) if not u]
            for i in unused:
                if _matches(self.calls[i]["request"], request):
                    self.used[i] = True
                    reply = self.calls[i]["reply"]
                    return reply, reply
        if not unused:
            raise ReplayMismatch(f"{self.path}: no recorded call left for this request:\n{_dump(request)}")
        expected = self.calls[unused[0]]["request"]
        shown = {k: request.get(k) for k in expected}
        diff = "\n".join(difflib.unified_diff(_dump(expected).splitlines(), _dump(shown).splitlines(),
                                              f"recorded call {unused[0]}", "this request", lineterm=""))
        raise ReplayMismatch(f"{self.path}: no recorded call matches this request (re-record the trace if the "
                             f"prompt changed on purpose):\n{diff}")


class RecordingModel(ChatModel):
    """Sends through inner (an AnthropicModel) and writes every call to path as a trace marked
    "fixture": "recorded". The file is rewritten after each call, so a failed run keeps what it got."""

    def __init__(self, inner: AnthropicModel, path: str | os.PathLike[str]) -> None:
        super().__init__(system=inner.system, feedback_template=inner.feedback_template)
        self.inner, self.path = inner, pathlib.Path(path)
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _record(self, request: dict[str, Any], text: str) -> None:
        with self._lock:
            self.calls.append({"request": request, "reply": text})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            trace = {"fixture": "recorded", "model": self.inner.model, "calls": self.calls}
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)

    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[str, Any]:
        text, back = self.inner._send(request, wire)
        self._record(request, text)
        return text, back

    async def _asend(self, request: dict[str, Any], wire: list[Any]) -> tuple[str, Any]:
        text, back = await self.inner._asend(request, wire)
        self._record(request, text)
        return text, back


def model_for(mode: str, trace: str | os.PathLike[str] | None = None, *, system: str | None = None,
              model: str = DEFAULT_MODEL, **kw: Any) -> ChatModel | None:
    """The model for a mode name: "replay" (ReplayModel(trace)), "record" (RecordingModel over an
    AnthropicModel, writing trace), "anthropic" (AnthropicModel) or "fake" (None: the caller keeps
    its own fake). kw goes to AnthropicModel."""
    if mode not in MODES:
        raise ValueError(f"unknown model mode {mode!r}; pick from {', '.join(MODES)}")
    if mode == "fake":
        return None
    if mode == "anthropic":
        return AnthropicModel(model, system=system, **kw)
    if trace is None:
        raise ValueError(f"model mode {mode!r} needs a trace path")
    if mode == "replay":
        return ReplayModel(trace, system=system)
    return RecordingModel(AnthropicModel(model, system=system, **kw), trace)
