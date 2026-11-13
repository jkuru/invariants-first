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

With a reply tool, the model answers by calling a tool whose input is the structured reply, as an
agent does, instead of writing JSON text:

    llm = AnthropicModel(system="...", tool=reply_tool("submit_cause", CauseReply))

complete() then returns the tool call's input as JSON text, which the decorator parses and checks
like any reply, and the feedback after a format failure goes back as a tool_result turn for that
tool_use id, with is_error true, as the Messages API expects after a tool call.

ReplayModel answers from a JSON trace and fails loudly, with a diff, when a request differs from
the recorded one. RecordingModel wraps a real model and writes such a trace. A trace is

    {"fixture": "recorded" | "hand-written", "model": "...",
     "calls": [{"request": {"model": "...", "max_tokens": 16000, "system": "...", "messages": [...]},
                "reply": "..."}]}

where a reply is the text, or, when the model called the reply tool, its text and tool_use blocks
as a list ([{"type": "tool_use", "id": "toolu_...", "name": "...", "input": {...}}]).

A request records everything the adapter sends (model, max_tokens, every param, tools and
tool_choice when a reply tool is set, system when set, the messages), and a replay compares all of it, unless ReplayModel(..., ignore=[...]) names fields
to leave out.

The `anthropic` package is imported only when an AnthropicModel without a client makes its first
request; ReplayModel and RecordingModel over a stub need nothing beyond the library.
"""
from __future__ import annotations

import base64
import contextvars
import copy
import dataclasses
import difflib
import importlib
import inspect
import itertools
import json
import os
import pathlib
import threading
import weakref
from collections.abc import Sequence
from typing import Any

__all__ = ["ChatModel", "AnthropicModel", "ReplayModel", "RecordingModel", "ReplayMismatch", "ConversationError",
           "ModelRefusal", "model_for", "reply_tool", "MODES", "DEFAULT_MODEL", "FEEDBACK_TEMPLATE",
           "TOOL_FEEDBACK_TEMPLATE", "TOOL_CHOICE"]

DEFAULT_MODEL = "claude-haiku-5-5"
FEEDBACK_TEMPLATE = "That reply could not be used: {feedback}\nReply again with only the corrected JSON object."
TOOL_FEEDBACK_TEMPLATE = "That call could not be used: {feedback}\nCall {tool} again with the corrected input."
# auto, not a forced {"type": "tool"}: several current models (and requests with thinking on) refuse a
# forced tool choice. The system prompt names the tool; a reply with no tool call is parsed as text.
TOOL_CHOICE: dict[str, Any] = {"type": "auto", "disable_parallel_tool_use": True}
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
    """One logical model call: its messages as a trace records them (assistant turns as text, or text
    and tool_use blocks) and as the API is sent them (assistant turns as the content blocks the API
    returned). pending: the tool_use ids of the last reply, which a follow-up turn must answer."""

    __slots__ = ("opening", "messages", "wire", "pending")

    def __init__(self, opening: list[Message]) -> None:
        self.opening = opening
        self.messages: list[Message] = copy.deepcopy(opening)
        self.wire: list[Any] = copy.deepcopy(opening)
        self.pending: list[str] = []


_CONVERSATIONS: contextvars.ContextVar[dict[int, _Conversation]] = contextvars.ContextVar(
    "invariants_anthropic_conversations", default={})
_IDS = itertools.count(1)

# Every model holding a lock (AnthropicModel, ReplayModel, RecordingModel): a forked child gives each a
# fresh one, since a thread of the parent may have held it at the fork and none of the child's would
# ever release it.
_LOCKED: weakref.WeakSet[Any] = weakref.WeakSet()


def _after_fork_in_child() -> None:
    for model in list(_LOCKED):
        model._lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _opening(prompt: Prompt) -> list[Message]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    msgs = [dict(m) for m in prompt]
    if not msgs or any(not isinstance(m.get("role"), str) or "content" not in m for m in msgs):
        raise TypeError("prompt must be a string or a non-empty list of {role, content} messages")
    if msgs[-1]["role"] != "user":
        raise TypeError("a prompt given as messages must end with a user message")
    return msgs


def _input_schema(schema: Any) -> dict[str, Any]:
    """The JSON Schema of a reply schema (pydantic model, dataclass, TypedDict or {field: type} map),
    closed at the top (additionalProperties false), as the parser closes it."""
    if isinstance(schema, dict):
        if not all(isinstance(k, str) for k in schema):
            raise TypeError("a {field: type} schema has string keys")
        schema = dataclasses.make_dataclass("Reply", list(schema.items()))
    if hasattr(schema, "model_json_schema"):
        out: Any = schema.model_json_schema()
    elif isinstance(schema, type):
        from ..lock import json_schema
        out = json_schema(schema)
    else:
        raise TypeError(f"reply_tool needs a pydantic model, dataclass, TypedDict or {{field: type}} map, "
                        f"not {type(schema).__name__}")
    out = json.loads(json.dumps(out))
    if not isinstance(out, dict) or out.get("type") != "object":
        raise TypeError("a reply tool's input schema must describe a JSON object")
    out.setdefault("additionalProperties", False)
    return out


def reply_tool(name: str, schema: Any = None, *, description: str | None = None,
               input_schema: dict[str, Any] | None = None, strict: bool = False) -> dict[str, Any]:
    """A tool definition whose input is the structured reply, for AnthropicModel(tool=...) and
    ReplayModel(tool=...). schema: the model_call's schema (its JSON Schema becomes input_schema), or
    give input_schema yourself. strict=True sets the API's strict tool use, which accepts only the
    JSON Schema subset strict mode supports; the decorator validates the reply either way."""
    if not isinstance(name, str) or not name:
        raise TypeError("a reply tool needs a name")
    if (schema is None) == (input_schema is None):
        raise TypeError("reply_tool takes a schema or an input_schema, not both or neither")
    body = _input_schema(schema) if input_schema is None else json.loads(json.dumps(input_schema))
    if description is None:
        doc = inspect.getdoc(schema) if schema is not None and not isinstance(schema, dict) else None
        description = (doc or "").split("\n\n")[0].strip() or "Give your reply as this tool's input."
    tool: dict[str, Any] = {"name": name, "description": description, "input_schema": body}
    if strict:
        tool["strict"] = True
    return tool


def _check_tool(tool: Any) -> dict[str, Any] | None:
    if tool is None:
        return None
    if not (isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"]
            and isinstance(tool.get("input_schema"), dict)):
        raise TypeError("tool is a tool definition {name, description, input_schema}; reply_tool() builds one")
    out: dict[str, Any] = json.loads(json.dumps(tool))
    return out


def _block_dict(b: Any) -> dict[str, Any] | None:
    """A text or tool_use block as a trace records it; None for any other block (thinking, ...)."""
    def get(k: str, default: Any = None) -> Any:
        return b.get(k, default) if isinstance(b, dict) else getattr(b, k, default)
    kind = get("type")
    if kind == "text":
        return {"type": "text", "text": get("text", "")}
    if kind == "tool_use":
        return {"type": "tool_use", "id": get("id"), "name": get("name"),
                "input": json.loads(json.dumps(get("input"), ensure_ascii=False, default=_encode))}
    return None


def _valid_reply(reply: Any) -> bool:
    """A trace reply: text, or a non-empty list of text and tool_use blocks."""
    if isinstance(reply, str):
        return True
    if not isinstance(reply, list) or not reply:
        return False
    for b in reply:
        if not isinstance(b, dict):
            return False
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            continue
        if not (b.get("type") == "tool_use" and isinstance(b.get("id"), str) and isinstance(b.get("name"), str)
                and isinstance(b.get("input"), dict)):
            return False
    return True


class ChatModel:
    """The conversation logic shared by the three models. Subclasses send one request.

    tool: a reply tool (reply_tool(...)): the request offers it (tools, tool_choice), the reply is the
    input of the model's call to it, as JSON text, and feedback goes back as a tool_result."""

    def __init__(self, *, system: str | None = None, feedback_template: str | None = None,
                 tool: dict[str, Any] | None = None, tool_choice: dict[str, Any] | None = None) -> None:
        self.system = system
        self.tool = _check_tool(tool)
        if tool_choice is not None and self.tool is None:
            raise TypeError("tool_choice needs a tool")
        self.tool_choice: dict[str, Any] | None = (
            None if self.tool is None else copy.deepcopy(TOOL_CHOICE if tool_choice is None else tool_choice))
        self.feedback_template = (feedback_template if feedback_template is not None
                                  else TOOL_FEEDBACK_TEMPLATE if self.tool is not None else FEEDBACK_TEMPLATE)
        self._key = next(_IDS)

    # --- what a model_call body calls ------------------------------------------------------------
    def complete(self, prompt: Prompt, feedback: str | None = None) -> str:
        """The model's reply text (with a reply tool: the tool call's input as JSON text). feedback
        None opens a new conversation; a feedback text is sent as the next user turn of the current
        one (a tool_result with is_error true when the last reply was a tool call)."""
        conv, request = self._turn(prompt, feedback)
        content, wire = self._send(request, conv.wire)
        return self._answered(conv, content, wire)

    async def acomplete(self, prompt: Prompt, feedback: str | None = None) -> str:
        """complete() for an async def body."""
        conv, request = self._turn(prompt, feedback)
        content, wire = await self._asend(request, conv.wire)
        return self._answered(conv, content, wire)

    # --- for subclasses --------------------------------------------------------------------------
    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[Any, Any]:
        """(the reply as a trace records it: text, or a list of text and tool_use blocks; the
        assistant content to send back on a follow-up turn)."""
        raise NotImplementedError

    async def _asend(self, request: dict[str, Any], wire: list[Any]) -> tuple[Any, Any]:
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
            text = self.feedback_template.format(feedback=feedback, tool=self.tool["name"] if self.tool else "")
            turn: Message = {"role": "user", "content": text}
            if conv.pending:            # after a tool call the API wants a tool_result for each tool_use
                turn = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": text,
                                                     "is_error": True} for i in conv.pending]}
            conv.messages.append(turn)
            conv.wire.append(copy.deepcopy(turn))
        request: dict[str, Any] = {}
        if self.tool is not None:
            request["tools"] = [copy.deepcopy(self.tool)]
            request["tool_choice"] = copy.deepcopy(self.tool_choice)
        if self.system is not None:
            request["system"] = self.system
        request["messages"] = copy.deepcopy(conv.messages)
        return conv, request

    def _read(self, content: Any) -> tuple[str, list[str]]:
        """(the reply text, the tool_use ids a follow-up turn answers) of a recorded reply."""
        if isinstance(content, str):
            return content, []
        uses = [b for b in content if b["type"] == "tool_use"]
        mine = next((b for b in uses if self.tool is not None and b["name"] == self.tool["name"]), None)
        text = (json.dumps(mine["input"], ensure_ascii=False) if mine is not None
                else "".join(b["text"] for b in content if b["type"] == "text"))
        return text, [b["id"] for b in uses]

    def _answered(self, conv: _Conversation, content: Any, wire: Any) -> str:
        text, conv.pending = self._read(content)
        conv.messages.append({"role": "assistant", "content": copy.deepcopy(content)})
        conv.wire.append({"role": "assistant", "content": wire})
        return text


class AnthropicModel(ChatModel):
    """A Claude model through the Messages API. client: an anthropic.Anthropic (or a stub with
    .messages.create); by default one is made on the first request (needs the `anthropic` extra and
    credentials, such as ANTHROPIC_API_KEY). async_client is used by acomplete.

    params are passed to messages.create unchanged (for example output_config). tool: a reply tool
    (reply_tool(...)); tool_choice defaults to TOOL_CHOICE (auto, one call at most)."""

    def __init__(self, model: str = DEFAULT_MODEL, *, system: str | None = None, max_tokens: int = 16000,
                 client: Any = None, async_client: Any = None, feedback_template: str | None = None,
                 tool: dict[str, Any] | None = None, tool_choice: dict[str, Any] | None = None,
                 **params: Any) -> None:
        super().__init__(system=system, feedback_template=feedback_template, tool=tool, tool_choice=tool_choice)
        _no_tool_params(params, self.tool)
        self.model, self.max_tokens, self.params = model, max_tokens, params
        self._client, self._async_client = client, async_client
        self._lock = threading.Lock()
        _LOCKED.add(self)

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

    def shape(self, request: dict[str, Any]) -> dict[str, Any]:
        """The request as a trace records it: model, max_tokens, the params, tools and tool_choice
        (with a reply tool), system (when set) and the messages, assistant turns as text (or text and
        tool_use blocks)."""
        return _request_shape(self.model, self.max_tokens, self.params, request)

    def _kwargs(self, request: dict[str, Any], wire: list[Any]) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": list(wire),
                              **self.params}
        for k in ("tools", "tool_choice", "system"):
            if k in request:
                kw[k] = request[k]
        return kw

    def _reply(self, response: Any) -> tuple[Any, Any]:
        if getattr(response, "stop_reason", None) == "refusal":
            raise ModelRefusal(f"{self.model} declined the request")
        content = list(getattr(response, "content", None) or [])
        kept = [d for d in map(_block_dict, content) if d is not None]
        if self.tool is not None and any(d["type"] == "tool_use" for d in kept):
            return kept, content            # a tool call: recorded as its text and tool_use blocks
        text = "".join(d["text"] for d in kept if d["type"] == "text")
        return text, content                # the blocks go back unchanged (thinking blocks included)

    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[Any, Any]:
        return self._reply(self.client().messages.create(**self._kwargs(request, wire)))

    async def _asend(self, request: dict[str, Any], wire: list[Any]) -> tuple[Any, Any]:
        return self._reply(await self.async_client().messages.create(**self._kwargs(request, wire)))


def _no_tool_params(params: dict[str, Any], tool: dict[str, Any] | None) -> None:
    if tool is not None and ("tools" in params or "tool_choice" in params):
        raise TypeError("with a reply tool, pass tool= and tool_choice=, not tools or tool_choice params")


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)


BYTES_MARKER = "$bytes_base64"


def _encode(obj: Any) -> Any:
    """bytes as {"$bytes_base64": "..."} (deterministic, so a replay of the same bytes matches); any
    other value JSON cannot hold is a TypeError."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {BYTES_MARKER: base64.b64encode(bytes(obj)).decode("ascii")}
    raise TypeError(f"a request holds a {type(obj).__name__}, which a JSON trace cannot record; pass JSON "
                    f"values (strings, numbers, lists, dicts) or bytes")


def _request_shape(model: str, max_tokens: int, params: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Everything the adapter sends but the wire form of assistant turns, as JSON would read it back.
    TypeError when it does not serialise, so a recording fails before the API call, not after."""
    shape = {"model": model, "max_tokens": max_tokens, **params, **request}
    out: dict[str, Any] = json.loads(json.dumps(shape, ensure_ascii=False, default=_encode))
    return out


_ABSENT = object()


def _matches(expected: dict[str, Any], request: dict[str, Any], ignore: frozenset[str]) -> bool:
    """The whole request shape is equal: every field either side has (a system prompt recorded on one
    side only is a mismatch), except the fields in ignore."""
    keys = (set(expected) | set(request)) - ignore
    return all(expected.get(k, _ABSENT) == request.get(k, _ABSENT) for k in keys)


class ReplayModel(ChatModel):
    """Replies from a trace file, matched on the whole request: messages, system (absent on one side
    and present on the other is a mismatch), model, max_tokens and every other param, as the
    AnthropicModel it stands in for would send them. So give it the configuration that model would
    have (model_for does). A request no unused recorded call matches raises ReplayMismatch with a
    diff against the next unused call. Thread-safe.

    ignore names request fields left out of the comparison (for example ignore=["model"] to replay a
    trace under another model); nothing is ignored by default. tool and tool_choice as AnthropicModel's:
    the tools and tool_choice are compared like any other field."""

    def __init__(self, path: str | os.PathLike[str], *, system: str | None = None, model: str = DEFAULT_MODEL,
                 max_tokens: int = 16000, ignore: Sequence[str] = (), feedback_template: str | None = None,
                 tool: dict[str, Any] | None = None, tool_choice: dict[str, Any] | None = None,
                 **params: Any) -> None:
        super().__init__(system=system, feedback_template=feedback_template, tool=tool, tool_choice=tool_choice)
        _no_tool_params(params, self.tool)
        if isinstance(ignore, str) or not all(isinstance(k, str) for k in ignore):
            raise TypeError("ignore is a list of request field names, such as ['model']")
        self.ignore: frozenset[str] = frozenset(ignore)
        self.max_tokens, self.params = max_tokens, params
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
                    and isinstance(c["request"].get("messages"), list) and _valid_reply(c.get("reply"))):
                raise ValueError(f"{self.path}: call {i} needs a request with messages and a reply (text, or "
                                 f"a list of text and tool_use blocks)")
        self.fixture: str = trace["fixture"]
        self.model: str = model
        self.trace_model: str | None = trace.get("model")
        self.calls: list[dict[str, Any]] = trace["calls"]
        self.used: list[bool] = [False] * len(self.calls)
        self._lock = threading.Lock()
        _LOCKED.add(self)

    @property
    def remaining(self) -> int:
        """Recorded calls not replayed yet."""
        return self.used.count(False)

    def assert_done(self) -> None:
        """ReplayMismatch unless every recorded call was replayed."""
        if self.remaining:
            left = [i for i, u in enumerate(self.used) if not u]
            raise ReplayMismatch(f"{self.path}: {len(left)} recorded call(s) never requested: {left}")

    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[Any, Any]:
        request = _request_shape(self.model, self.max_tokens, self.params, request)
        with self._lock:
            unused = [i for i, u in enumerate(self.used) if not u]
            for i in unused:
                if _matches(self.calls[i]["request"], request, self.ignore):
                    self.used[i] = True
                    reply = self.calls[i]["reply"]
                    return copy.deepcopy(reply), copy.deepcopy(reply)
        if not unused:
            raise ReplayMismatch(f"{self.path}: no recorded call left for this request:\n{_dump(request)}")
        expected = {k: v for k, v in self.calls[unused[0]]["request"].items() if k not in self.ignore}
        shown = {k: v for k, v in request.items() if k not in self.ignore}
        diff = "\n".join(difflib.unified_diff(_dump(expected).splitlines(), _dump(shown).splitlines(),
                                              f"recorded call {unused[0]}", "this request", lineterm=""))
        raise ReplayMismatch(f"{self.path}: no recorded call matches this request (re-record the trace if the "
                             f"prompt changed on purpose):\n{diff}")


class RecordingModel(ChatModel):
    """Sends through inner (an AnthropicModel) and writes every call to path as a trace marked
    "fixture": "recorded". The file is rewritten after each call, so a failed run keeps what it got."""

    def __init__(self, inner: AnthropicModel, path: str | os.PathLike[str]) -> None:
        super().__init__(system=inner.system, feedback_template=inner.feedback_template, tool=inner.tool,
                         tool_choice=inner.tool_choice)
        self.inner, self.path = inner, pathlib.Path(path)
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        _LOCKED.add(self)

    def _record(self, request: dict[str, Any], reply: Any) -> None:
        with self._lock:
            self.calls.append({"request": request, "reply": copy.deepcopy(reply)})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            trace = {"fixture": "recorded", "model": self.inner.model, "calls": self.calls}
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)

    def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[Any, Any]:
        shape = self.inner.shape(request)               # serialised first: a paid call is never lost
        content, back = self.inner._send(request, wire)
        self._record(shape, content)
        return content, back

    async def _asend(self, request: dict[str, Any], wire: list[Any]) -> tuple[Any, Any]:
        shape = self.inner.shape(request)
        content, back = await self.inner._asend(request, wire)
        self._record(shape, content)
        return content, back


def model_for(mode: str, trace: str | os.PathLike[str] | None = None, *, system: str | None = None,
              model: str = DEFAULT_MODEL, **kw: Any) -> ChatModel | None:
    """The model for a mode name: "replay" (ReplayModel(trace)), "record" (RecordingModel over an
    AnthropicModel, writing trace), "anthropic" (AnthropicModel) or "fake" (None: the caller keeps
    its own fake). kw goes to AnthropicModel, and to ReplayModel, so a replay compares the model,
    max_tokens, params and reply tool the live model would send; ignore= goes to ReplayModel only."""
    ignore = kw.pop("ignore", ())
    if mode not in MODES:
        raise ValueError(f"unknown model mode {mode!r}; pick from {', '.join(MODES)}")
    if mode == "fake":
        return None
    if mode == "anthropic":
        return AnthropicModel(model, system=system, **kw)
    if trace is None:
        raise ValueError(f"model mode {mode!r} needs a trace path")
    if mode == "replay":
        return ReplayModel(trace, system=system, model=model, ignore=ignore, **kw)
    return RecordingModel(AnthropicModel(model, system=system, **kw), trace)
