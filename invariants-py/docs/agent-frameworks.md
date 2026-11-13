# Agent frameworks: tool calls, LangGraph, async, dependencies

An agent framework moves a model's output from one step to the next. The library does not care which
framework does it: a model's reply becomes a `Checked` value at `@inv.model_call`, and the step that
acts on it is an `@inv.edge` that re-checks it. This page shows the pieces an agent needs: the model
answering with a tool call, a LangGraph graph, async harnesses, and how the rules' providers get the
store or client they read. The worked example is `apps/support-agent`.

## The model answers with a tool call

Agents usually ask the model for a tool call, not for JSON in the reply text. Give the adapter a
reply tool, built from the model call's schema:

```python
from invariants.integrations.anthropic import AnthropicModel, reply_tool

PLAN_TOOL = reply_tool("submit_plan", Plan, description="Submit the one action for this ticket.")
llm = AnthropicModel(system="... Call the submit_plan tool exactly once ...", tool=PLAN_TOOL)

@inv.model_call("PLAN-1", schema=Plan, returns=CheckedPlan)
def plan_action(ctx: AgentContext, ticket: str, customer_id: str, inv_feedback: str | None = None) -> str:
    return llm.complete(prompt_for(ticket, customer_id), inv_feedback)
```

The request then carries `tools=[PLAN_TOOL]` and `tool_choice`, and `complete()` returns the input
of the model's `tool_use` block as JSON text. The decorator parses it into the closed schema and
checks the rule exactly as it does a JSON reply: tool-call arguments get no other path.

- `reply_tool(name, schema, description=None, input_schema=None, strict=False)` takes a pydantic
  model, a dataclass, a TypedDict or a `{field: type}` map and makes its JSON Schema the tool's
  `input_schema`, closed at the top (`additionalProperties: false`). `strict=True` turns on the
  API's strict tool use; strict mode accepts only part of JSON Schema, and the decorator validates
  the input either way.
- `tool_choice` defaults to `{"type": "auto", "disable_parallel_tool_use": true}`. A forced choice
  (`{"type": "tool", ...}` or `any`) is refused by several current models and by requests with
  thinking on, so the system prompt names the tool instead. Pass `tool_choice=` to change it.
- A reply with no tool call is parsed as its text, the same way; a reply that calls the tool twice
  uses the first call.
- After a format failure the parser error goes back as a `tool_result` block for each `tool_use` id
  of the previous reply, with `is_error: true`, in the user turn that follows it, as the Messages API
  requires after a tool call. The assistant turn is sent back as the blocks the API returned
  (thinking blocks included). A semantic resample is a new conversation, as for text replies.
- `ReplayModel(..., tool=PLAN_TOOL)` and `RecordingModel` handle the same shape: a trace records
  `tools` and `tool_choice` in each request and compares them like any other field, and a reply that
  called the tool is recorded as its text and `tool_use` blocks. See [models.md](models.md).

## LangGraph

LangGraph is not a dependency of the library. `apps/support-agent` has it as an optional extra
(`pip install -e '.[langgraph]'`, tested with `langgraph==1.2.21`), and its `support_agent.langgraph_agent`
is a `StateGraph`:

```
START -> triage -> planner -> act -> done -> END
                      \         \
                       +---------+-> handoff -> END
```

```python
class AgentState(TypedDict, total=False):
    ticket: str
    customer_id: str
    plan: CheckedPlan | None
    ...

def planner(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    p = plan_step(runtime.context, state["ticket"], state["customer_id"])   # runs the @inv.model_call
    return {"plan": p.plan, ...}

def act(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    plan = state.get("plan")
    if not isinstance(plan, CheckedPlan):
        return {"handoff_reason": "the plan in state is not a CheckedPlan; plan again"}
    result = execute_plan(runtime.context, plan, state["customer_id"])      # the @inv.edge
    ...

app = builder().compile()
app.invoke({"ticket": "refund 1200 on O-1", "customer_id": "C-1"}, context=ctx)
```

The planner node runs the model call and puts the `CheckedPlan` in the graph state. The act node
passes `state["plan"]` to the edge, which re-validates it and re-checks the rule on what it receives,
so a node or a state update that swaps or edits the plan after the planner checked it is refused
there (`tests/test_langgraph.py` adds a node that swaps in a plan for another customer's order, which
the payment door alone would allow). Here the node unpacks the plan into the edge's own signature;
for an edge that takes the state object itself, with the `Checked` value as one of its fields, see
`@inv.edge` in [api.md](api.md).

Two things LangGraph does that matter:

- **Checkpointers do not keep a `Checked`.** With a checkpointer the state is serialised, and a
  `CheckedPlan` comes back as a plain dict of its fields (seen with `InMemorySaver` and an interrupt
  before `act`). The edge's type gate refuses that dict with a `TypeError`: a restored value was
  never checked. The example's act node checks for it first and hands the ticket to a person; plan
  again after a resume, or keep the plan and its use in one step.
- **The runtime context is the dependency.** `StateGraph(AgentState, context_schema=AgentContext)`
  and `invoke(..., context=ctx)` give every node `runtime.context`; the nodes pass it to the model
  call and the edge as an argument, and the rules' providers read it as `args.ctx.ledger`. Nothing
  context-local is needed.

## Async harnesses

- An `async def` model call body awaits `llm.acomplete(...)`, which uses `anthropic.AsyncAnthropic`.
  An async edge is `@inv.edge` on an `async def`.
- `await app.ainvoke(...)` runs a sync node in a worker thread with a copy of the caller's context.
  A model call made in that node keeps its whole invocation (the first attempt, its format retries
  and resamples) in that thread, so the adapter's conversation, which is context-local, holds
  together. Call the model from the body itself, not from a thread the body starts.
- An async node runs as a task with a copy of the context at the time it starts.
- `app.invoke(...)` with an `async def` node raises (LangGraph needs `ainvoke` for it).

## Providers and dependencies

A rule's providers take binding paths (`args.ctx.ledger`, `self.store`, `reply.order_id`). When the
value a provider needs is an argument of the call (the agent's context, a service's `self`), bind it
by path: that is explicit and needs nothing else. When it is not (a module-level model call whose
provider reads a store no argument carries), use `inv.Dependency`:

```python
STORE: inv.Dependency[Store] = inv.Dependency("store")

@inv.provider("chunk")
def chunk(chunk_id: str) -> ChunkInfo | None:
    return STORE.get().chunk(chunk_id)

with STORE.bound(store):
    propose_fact(chunk_id, text, source_hash, 0)
```

`apps/fact-pipeline` works this way (`fact_pipeline/context.py`): each `Pipeline` stage binds its
own store and client, so two pipelines in one thread never read each other's.

`inv.Dependency` is a context variable underneath, and that is what it gives an async agent: a value
bound around `ainvoke` (or `invoke`) is seen by every node, sync nodes in worker threads included,
and by the providers the nodes' model calls and edges read; a value bound inside one node is not
seen by the next, since each node runs in its own copy of the context. So bind it around the graph
run, or inside the node that makes the call. It cannot read LangGraph's runtime context or a
node's state by itself: to reach those from a provider, pass them to the model call or edge as an
argument and bind the provider by path, as the support agent does.
