"""integrations.anthropic: AnthropicModel over a stub client, ReplayModel, RecordingModel, and the
conversation one model_call invocation keeps across its retries. No network, no anthropic package."""
from __future__ import annotations

import asyncio
import gc
import importlib
import json
import sys
import threading
import types
from typing import Any, Iterator

import pytest

import invariants as inv
from invariants.integrations import anthropic as inv_an

app: Any = None
GOOD = json.dumps({"cause": "OUT_OF_MEMORY", "summary": "killed at 8 GiB"})
TIMEOUT = json.dumps({"cause": "TIMEOUT", "summary": "ran long"})
OOM_LOG = "kernel: Out of memory: Killed process 4242"


@pytest.fixture(scope="module", autouse=True)
def _app() -> Iterator[None]:
    global app
    app = importlib.import_module("integ_anthropic_app")
    yield
    app = None
    sys.modules.pop("integ_anthropic_app", None)
    gc.collect()


@pytest.fixture
def reg() -> Iterator[inv.Registry]:
    with inv.isolated() as r:
        inv.load(app.RULES)
        yield r


# --- a stub of the anthropic client ------------------------------------------------------------
def block(kind: str, **kw: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(type=kind, **kw)


class StubMessages:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def _next(self, kw: dict[str, Any]) -> Any:
        self.requests.append(json.loads(json.dumps(kw, default=lambda b: vars(b))))
        r = self.replies.pop(0)
        if isinstance(r, str):
            r = types.SimpleNamespace(stop_reason="end_turn",
                                      content=[block("thinking", thinking="", signature="sig"), block("text", text=r)])
        return r

    def create(self, **kw: Any) -> Any:
        return self._next(kw)


class AsyncStubMessages(StubMessages):
    async def create(self, **kw: Any) -> Any:  # type: ignore[override]
        await asyncio.sleep(0)
        return self._next(kw)


def stub(*replies: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(messages=StubMessages(list(replies)))


def anthropic_model(*replies: Any, **kw: Any) -> inv_an.AnthropicModel:
    return inv_an.AnthropicModel(system=app.SYSTEM, client=stub(*replies), **kw)


def call(job: str = "J-1", oom: bool = True) -> tuple[Any, Any]:
    try:
        return app.propose_cause(job, OOM_LOG, oom), None
    except inv.InvariantError as e:
        return None, e


# --- AnthropicModel ----------------------------------------------------------------------------
def test_one_request_with_model_system_and_max_tokens(reg):
    app.llm = m = anthropic_model(GOOD, max_tokens=512, output_config={"effort": "low"})
    c, err = call()
    assert err is None and c.cause == "OUT_OF_MEMORY" and c.attempts == 1
    [req] = m.client().messages.requests
    assert req["model"] == "claude-haiku-5-5" and req["max_tokens"] == 512 and req["system"] == app.SYSTEM
    assert req["output_config"] == {"effort": "low"}
    assert req["messages"] == [{"role": "user", "content": app.prompt_for("J-1", OOM_LOG)}]


def test_a_format_retry_is_a_follow_up_turn_after_the_bad_reply(reg):
    app.llm = m = anthropic_model("Sure! It ran out of memory.", GOOD)
    c, err = call()
    assert err is None and c.attempts == 2
    first, second = m.client().messages.requests
    assert len(first["messages"]) == 1
    msgs = second["messages"]
    assert [x["role"] for x in msgs] == ["user", "assistant", "user"]
    assert msgs[0] == first["messages"][0]
    # the assistant turn goes back as the blocks the API returned, thinking block included
    assert msgs[1]["content"][0]["type"] == "thinking" and msgs[1]["content"][1]["text"].startswith("Sure!")
    assert msgs[2]["content"].startswith("That reply could not be used: ")
    assert "corrected JSON" in msgs[2]["content"]


def test_a_semantic_resample_is_a_fresh_conversation(reg):
    app.llm = m = anthropic_model(TIMEOUT, GOOD)
    c, err = call(oom=True)
    assert err is None and c.attempts == 2
    first, second = m.client().messages.requests
    assert second["messages"] == first["messages"] and len(second["messages"]) == 1


def test_a_new_invocation_starts_a_new_conversation(reg):
    app.llm = m = anthropic_model("not json", GOOD, GOOD)
    call("J-1")
    call("J-2")
    reqs = m.client().messages.requests
    assert len(reqs) == 3 and len(reqs[1]["messages"]) == 3
    assert reqs[2]["messages"] == [{"role": "user", "content": app.prompt_for("J-2", OOM_LOG)}]


def test_retries_spent_keep_one_growing_conversation(reg):
    app.llm = m = anthropic_model("a", "b", "c")
    _, err = call()
    assert isinstance(err, inv.Escalation) and err.outcome.kind == "parse_error"
    assert [len(r["messages"]) for r in m.client().messages.requests] == [1, 3, 5]


def test_a_refusal_raises():
    m = anthropic_model(types.SimpleNamespace(stop_reason="refusal", content=[]))
    with pytest.raises(inv_an.ModelRefusal):
        m.complete("hello")


def test_only_text_blocks_make_the_reply():
    m = anthropic_model(types.SimpleNamespace(stop_reason="end_turn", content=[
        block("text", text='{"a": '), block("tool_use", id="t", name="x", input={}), block("text", text="1}")]))
    assert m.complete("hello") == '{"a": 1}'


def test_feedback_without_a_conversation_is_an_error():
    m = anthropic_model(GOOD)
    with pytest.raises(inv_an.ConversationError, match="no conversation"):
        m.complete("hello", "bad json")


def test_feedback_for_another_prompt_is_an_error():
    m = anthropic_model(GOOD, GOOD)
    m.complete("hello")
    with pytest.raises(inv_an.ConversationError, match="another prompt"):
        m.complete("goodbye", "bad json")


def test_a_prompt_as_messages():
    m = anthropic_model("x", GOOD)
    few = [{"role": "user", "content": "example"}, {"role": "assistant", "content": GOOD},
           {"role": "user", "content": "now this one"}]
    m.complete(few)
    m.complete(few, "not JSON")
    assert len(m.client().messages.requests[1]["messages"]) == 5
    with pytest.raises(TypeError):
        m.complete([{"role": "assistant", "content": "x"}])
    with pytest.raises(TypeError):
        m.complete([])


def test_threads_keep_their_own_conversations():
    seen: dict[str, list[int]] = {}
    lock = threading.Lock()

    class Echo(inv_an.ChatModel):
        def _send(self, request: dict[str, Any], wire: list[Any]) -> tuple[str, Any]:
            with lock:
                seen.setdefault(request["messages"][0]["content"], []).append(len(request["messages"]))
            return "r", "r"

    e = Echo()
    barrier = threading.Barrier(2)

    def run(p: str) -> None:
        e.complete(p)
        barrier.wait()
        e.complete(p, "again")

    ts = [threading.Thread(target=run, args=(p,)) for p in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert seen == {"a": [1, 3], "b": [1, 3]}


def test_no_anthropic_package_is_a_clear_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    m = inv_an.AnthropicModel()
    with pytest.raises(ImportError, match=r"invariants-first\[anthropic\]"):
        m.complete("hello")


def test_async_body_uses_acomplete(reg):
    app.llm = m = inv_an.AnthropicModel(system=app.SYSTEM,
                                        async_client=types.SimpleNamespace(messages=AsyncStubMessages(["nope", GOOD])))
    c = asyncio.run(app.apropose_cause("J-1", OOM_LOG, True))
    assert c.attempts == 2
    assert [len(r["messages"]) for r in m.async_client().messages.requests] == [1, 3]


# --- RecordingModel and ReplayModel -------------------------------------------------------------
def test_record_then_replay_the_same_flow(reg, tmp_path):
    path = tmp_path / "traces" / "oom.json"
    app.llm = rec = inv_an.RecordingModel(anthropic_model("Sure! OOM.", GOOD), path)
    c, _ = call()
    assert c.attempts == 2
    trace = json.loads(path.read_text())
    assert trace["fixture"] == "recorded" and trace["model"] == "claude-haiku-5-5" and len(trace["calls"]) == 2
    second = trace["calls"][1]["request"]
    assert second["system"] == app.SYSTEM
    assert second["messages"][1] == {"role": "assistant", "content": "Sure! OOM."}     # text, not blocks
    assert rec.calls == trace["calls"]

    app.llm = rep = inv_an.ReplayModel(path, system=app.SYSTEM)
    c2, _ = call()
    assert c2.attempts == 2 and c2.cause == "OUT_OF_MEMORY" and rep.remaining == 0
    rep.assert_done()


def write_trace(path: Any, calls: list[dict[str, Any]], fixture: str = "hand-written") -> Any:
    path.write_text(json.dumps({"fixture": fixture, "model": "claude-haiku-5-5", "calls": calls}))
    return path


def req(*contents: str, system: str | None = None) -> dict[str, Any]:
    roles = ["user", "assistant"]
    r: dict[str, Any] = {"messages": [{"role": roles[i % 2], "content": c} for i, c in enumerate(contents)]}
    if system is not None:
        r["system"] = system
    return r


def test_replay_mismatch_shows_a_diff(tmp_path):
    p = write_trace(tmp_path / "t.json", [{"request": req("hello there", system="S"), "reply": GOOD}])
    m = inv_an.ReplayModel(p, system="S")
    with pytest.raises(inv_an.ReplayMismatch) as ei:
        m.complete("hello world")
    text = str(ei.value)
    assert "-" in text and '"content": "hello there"' in text and '"content": "hello world"' in text
    assert "recorded call 0" in text


def test_replay_matches_out_of_order_and_runs_out_loudly(tmp_path):
    p = write_trace(tmp_path / "t.json", [{"request": req("a"), "reply": "ra"}, {"request": req("b"), "reply": "rb"}])
    m = inv_an.ReplayModel(p)
    assert m.complete("b") == "rb" and m.complete("a") == "ra"
    with pytest.raises(inv_an.ReplayMismatch, match="no recorded call left"):
        m.complete("a")


def test_replay_repeats_identical_requests_in_order(tmp_path):
    p = write_trace(tmp_path / "t.json", [{"request": req("a"), "reply": "1"}, {"request": req("a"), "reply": "2"}])
    m = inv_an.ReplayModel(p)
    assert [m.complete("a"), m.complete("a")] == ["1", "2"]


def test_replay_system_only_compared_when_recorded(tmp_path):
    p = write_trace(tmp_path / "t.json", [{"request": req("a"), "reply": "1"}])
    assert inv_an.ReplayModel(p, system="anything").complete("a") == "1"
    p2 = write_trace(tmp_path / "t2.json", [{"request": req("a", system="S"), "reply": "1"}])
    with pytest.raises(inv_an.ReplayMismatch, match="system"):
        inv_an.ReplayModel(p2, system="T").complete("a")


def test_assert_done_names_unused_calls(tmp_path):
    p = write_trace(tmp_path / "t.json", [{"request": req("a"), "reply": "1"}, {"request": req("b"), "reply": "2"}])
    m = inv_an.ReplayModel(p)
    m.complete("a")
    with pytest.raises(inv_an.ReplayMismatch, match=r"\[1\]"):
        m.assert_done()


@pytest.mark.parametrize("trace, match", [
    ({"calls": []}, "fixture"),
    ({"fixture": "made-up", "calls": []}, "fixture"),
    ({"fixture": "recorded"}, "calls"),
    ({"fixture": "recorded", "calls": [{"request": {}, "reply": "x"}]}, "call 0"),
    ({"fixture": "recorded", "calls": [{"request": {"messages": []}, "reply": 3}]}, "call 0"),
])
def test_bad_trace_files(tmp_path, trace, match):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(trace))
    with pytest.raises(ValueError, match=match):
        inv_an.ReplayModel(p)


def test_missing_trace_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="no replay trace"):
        inv_an.ReplayModel(tmp_path / "none.json")


# --- model_for ------------------------------------------------------------------------------------
def test_model_for(tmp_path):
    p = write_trace(tmp_path / "t.json", [])
    assert inv_an.model_for("fake") is None
    assert isinstance(inv_an.model_for("replay", p), inv_an.ReplayModel)
    rec = inv_an.model_for("record", tmp_path / "r.json", system="S", max_tokens=100)
    assert isinstance(rec, inv_an.RecordingModel) and rec.inner.max_tokens == 100 and rec.system == "S"
    a = inv_an.model_for("anthropic", model="claude-opus-5-5")
    assert isinstance(a, inv_an.AnthropicModel) and a.model == "claude-opus-5-5"
    with pytest.raises(ValueError, match="needs a trace"):
        inv_an.model_for("replay")
    with pytest.raises(ValueError, match="unknown model mode"):
        inv_an.model_for("openai")
