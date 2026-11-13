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
    r: dict[str, Any] = {"model": "claude-haiku-5-5", "max_tokens": 16000,
                         "messages": [{"role": roles[i % 2], "content": c} for i, c in enumerate(contents)]}
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


def test_replay_system_compared_absent_or_present(tmp_path):
    p = write_trace(tmp_path / "t.json", [{"request": req("a"), "reply": "1"}])
    with pytest.raises(inv_an.ReplayMismatch, match="system"):     # recorded without one: a new one mismatches
        inv_an.ReplayModel(p, system="anything").complete("a")
    assert inv_an.ReplayModel(p).complete("a") == "1"
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
    t = inv_an.model_for("replay", p, tool=inv_an.reply_tool("t", {"n": int}))
    assert isinstance(t, inv_an.ReplayModel) and t.tool is not None and t.tool_choice == inv_an.TOOL_CHOICE


# --- a reply tool: the model answers with a tool call whose input is the reply -------------------
GOOD_IN = {"cause": "OUT_OF_MEMORY", "summary": "killed at 8 GiB"}


def cause_tool() -> dict[str, Any]:
    return inv_an.reply_tool("submit_cause", app.CauseReply, description="Submit the cause of the failure.")


def tool_call(inp: dict[str, Any], tid: str = "toolu_1", text: str | None = None) -> types.SimpleNamespace:
    content = [block("thinking", thinking="", signature="sig")]
    if text is not None:
        content.append(block("text", text=text))
    content.append(block("tool_use", id=tid, name="submit_cause", input=inp))
    return types.SimpleNamespace(stop_reason="tool_use", content=content)


def test_reply_tool_schemas():
    t = cause_tool()
    assert t["name"] == "submit_cause" and t["description"] == "Submit the cause of the failure."
    s = t["input_schema"]
    assert s["type"] == "object" and s["additionalProperties"] is False
    assert set(s["required"]) == {"cause", "summary"} and "OUT_OF_MEMORY" in s["properties"]["cause"]["enum"]
    assert "strict" not in t and inv_an.reply_tool("t", app.CauseReply, strict=True)["strict"] is True
    m = inv_an.reply_tool("t", {"n": int})["input_schema"]
    assert m["properties"]["n"]["type"] == "integer" and m["additionalProperties"] is False
    raw = {"type": "object", "properties": {}}
    assert inv_an.reply_tool("t", input_schema=raw)["input_schema"] == raw
    pydantic = pytest.importorskip("pydantic")

    class P(pydantic.BaseModel):
        """A reply.

        More detail."""
        model_config = pydantic.ConfigDict(extra="forbid")
        n: int = pydantic.Field(ge=0, description="a count")

    p = inv_an.reply_tool("t", P)
    assert p["description"] == "A reply." and p["input_schema"]["properties"]["n"]["minimum"] == 0
    with pytest.raises(TypeError, match="name"):
        inv_an.reply_tool("", app.CauseReply)
    with pytest.raises(TypeError, match="not both or neither"):
        inv_an.reply_tool("t")
    with pytest.raises(TypeError, match="not both or neither"):
        inv_an.reply_tool("t", app.CauseReply, input_schema=raw)
    with pytest.raises(TypeError, match="pydantic model, dataclass"):
        inv_an.reply_tool("t", 3)


def test_a_tool_call_is_the_reply(reg):
    app.llm = m = anthropic_model(tool_call(GOOD_IN), tool=cause_tool())
    c, err = call()
    assert err is None and c.cause == "OUT_OF_MEMORY" and c.attempts == 1
    [r] = m.client().messages.requests
    assert r["tools"] == [cause_tool()]
    assert r["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert r["messages"] == [{"role": "user", "content": app.prompt_for("J-1", OOM_LOG)}]


def test_a_bad_tool_call_gets_an_error_tool_result_for_its_id(reg):
    app.llm = m = anthropic_model(tool_call({"cause": "OUT_OF_MEMORY"}, "toolu_A", text="Calling the tool."),
                                  tool_call(GOOD_IN, "toolu_B"), tool=cause_tool())
    c, err = call()
    assert err is None and c.attempts == 2
    _, second = m.client().messages.requests
    user, asst, fb = second["messages"]
    assert [b["type"] for b in asst["content"]] == ["thinking", "text", "tool_use"]    # sent back unchanged
    assert asst["content"][2]["id"] == "toolu_A"
    [res] = fb["content"]
    assert fb["role"] == "user" and res["type"] == "tool_result" and res["tool_use_id"] == "toolu_A"
    assert res["is_error"] is True and res["content"].startswith("That call could not be used: ")
    assert res["content"].endswith("Call submit_cause again with the corrected input.")


def test_every_tool_use_of_the_reply_gets_a_tool_result():
    two = types.SimpleNamespace(stop_reason="tool_use", content=[
        block("tool_use", id="t1", name="submit_cause", input={"cause": "x"}),
        block("tool_use", id="t2", name="submit_cause", input={})])
    m = anthropic_model(two, tool_call(GOOD_IN), tool=cause_tool())
    assert json.loads(m.complete("hello")) == {"cause": "x"}      # the first call to the reply tool
    m.complete("hello", "bad")
    fb = m.client().messages.requests[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in fb] == ["t1", "t2"] and all(r["is_error"] for r in fb)


def test_a_reply_with_no_tool_call_is_parsed_as_text_and_answered_as_text(reg):
    app.llm = m = anthropic_model("I think it ran out of memory.", tool_call(GOOD_IN), tool=cause_tool())
    c, err = call()
    assert err is None and c.attempts == 2
    fb = m.client().messages.requests[1]["messages"][2]
    assert isinstance(fb["content"], str) and fb["content"].startswith("That call could not be used: ")


def test_tool_params_are_checked():
    with pytest.raises(TypeError, match="tool_choice needs a tool"):
        inv_an.AnthropicModel(tool_choice={"type": "auto"})
    with pytest.raises(TypeError, match="tool="):
        inv_an.AnthropicModel(tool=cause_tool(), tools=[])
    with pytest.raises(TypeError, match="tool definition"):
        inv_an.AnthropicModel(tool={"name": "x"})
    m = anthropic_model(tool_call(GOOD_IN), tool=cause_tool(), tool_choice={"type": "any"})
    m.complete("hello")
    assert m.client().messages.requests[0]["tool_choice"] == {"type": "any"}


def test_async_tool_flow(reg):
    app.llm = m = inv_an.AnthropicModel(
        system=app.SYSTEM, tool=cause_tool(),
        async_client=types.SimpleNamespace(messages=AsyncStubMessages([tool_call({}, "t1"), tool_call(GOOD_IN, "t2")])))
    c = asyncio.run(app.apropose_cause("J-1", OOM_LOG, True))
    assert c.attempts == 2
    fb = m.async_client().messages.requests[1]["messages"][2]["content"]
    assert fb[0]["tool_use_id"] == "t1" and fb[0]["is_error"] is True


def test_record_then_replay_a_tool_flow(reg, tmp_path):
    path = tmp_path / "tool.json"
    app.llm = inv_an.RecordingModel(anthropic_model(tool_call({"cause": "OOM"}, "toolu_A"),
                                                    tool_call(GOOD_IN, "toolu_B"), tool=cause_tool()), path)
    c, _ = call()
    assert c.attempts == 2
    trace = json.loads(path.read_text())
    first, second = trace["calls"]
    assert first["request"]["tools"] == [cause_tool()] and first["request"]["tool_choice"]["type"] == "auto"
    assert first["reply"] == [{"type": "tool_use", "id": "toolu_A", "name": "submit_cause", "input": {"cause": "OOM"}}]
    asst, fb = second["request"]["messages"][1:]
    assert asst == {"role": "assistant", "content": first["reply"]}                  # no thinking block
    assert fb["content"][0]["tool_use_id"] == "toolu_A" and fb["content"][0]["is_error"] is True

    app.llm = rep = inv_an.ReplayModel(path, system=app.SYSTEM, tool=cause_tool())
    c2, _ = call()
    assert c2.attempts == 2 and c2.cause == "OUT_OF_MEMORY"
    rep.assert_done()
    with pytest.raises(inv_an.ReplayMismatch, match="tools"):                        # no tool: tools differ
        inv_an.ReplayModel(path, system=app.SYSTEM).complete(app.prompt_for("J-1", OOM_LOG))
    other = inv_an.reply_tool("submit_cause", app.CauseReply, description="Something else.")
    with pytest.raises(inv_an.ReplayMismatch, match="Something else"):
        inv_an.ReplayModel(path, system=app.SYSTEM, tool=other).complete(app.prompt_for("J-1", OOM_LOG))


@pytest.mark.parametrize("reply", [[], [{"type": "tool_use", "id": "t", "name": "n"}], [{"type": "thinking"}],
                                   [{"type": "text"}], ["x"]])
def test_tool_replies_in_a_trace(tmp_path, reply):
    good = [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "t", "name": "n", "input": {}}]
    assert inv_an.ReplayModel(write_trace(tmp_path / "ok.json", [{"request": req("a"), "reply": good}])).remaining == 1
    p = write_trace(tmp_path / "t.json", [{"request": req("a"), "reply": reply}])
    with pytest.raises(ValueError, match="call 0"):
        inv_an.ReplayModel(p)
