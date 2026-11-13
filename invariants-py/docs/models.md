# Real models, replay and recording

`@inv.model_call` does not care which model wrote the reply: the body returns the raw reply text and
the decorator parses, retries, resamples and checks it. `invariants.integrations.anthropic` gives
that body a real model (Claude through the Messages API), a deterministic replay of a trace for
tests and CI, and a recorder that writes such traces. It adds no core dependency: the `anthropic`
package is imported only when an `AnthropicModel` without a client sends its first request
(`pip install 'invariants-first[anthropic]'`).

```python
import invariants as inv
from invariants.integrations.anthropic import AnthropicModel

llm = AnthropicModel(system="You triage failed jobs. Answer with one JSON object {cause, summary}.")
# AnthropicModel(model="claude-haiku-5-5", system=None, max_tokens=16000, client=None, **params)

@inv.model_call("R-7", schema=CauseReply, returns=CheckedCause)
def propose_cause(job_id: str, log: str, inv_feedback: str | None = None) -> str:
    return llm.complete(prompt_for(job_id, log), inv_feedback)
```

`complete(prompt, feedback)` takes a string or a list of `{role, content}` messages ending with a user
turn (few-shot examples), and returns the reply text (the text blocks joined; thinking and other blocks
are left out of the text). An `async def` body calls `await llm.acomplete(...)`, which uses
`anthropic.AsyncAnthropic`. A reply with stop reason `refusal` raises `ModelRefusal`. Extra keyword
arguments (for example `output_config={"effort": "low"}`) go to `messages.create` unchanged.

## One invocation is one conversation

`inv.model_call` calls the body again with the same arguments on each attempt, and sets
`inv_feedback`:

| attempt | `inv_feedback` | what the adapter sends |
|---|---|---|
| the first | `None` | a new conversation: the prompt as the first user turn |
| after a format failure (`parse_error`, `format_retries` left) | the parser error text | the same conversation, with the model's previous reply as the assistant turn and the feedback as a new user turn |
| a semantic resample (a `bad` clause fired, `semantic_resamples` left) | `None` | a new conversation: a fresh sample never sees the rule text |

So the adapter keys on `feedback` alone: `None` opens a conversation, a text continues the current
one. The feedback turn reads `That reply could not be used: <feedback>` and asks for the corrected
JSON object (`feedback_template=` changes it). The assistant turn is sent back as the content blocks
the API returned, thinking blocks included, so a follow-up turn is valid for a model that reasons.

The current conversation is context-local (a `contextvar`), per model object: each thread and each
asyncio task keeps its own, and a new invocation starts fresh. Call the model from the body itself,
not from a worker thread the body starts. A feedback text with no conversation to continue, or for
another prompt than the one the conversation opened with, raises `ConversationError`.

## Replay and recording

A trace is a JSON file:

```json
{"fixture": "recorded", "model": "claude-haiku-5-5",
 "calls": [{"request": {"system": "...", "messages": [{"role": "user", "content": "..."}]},
            "reply": "{\"cause\": \"OUT_OF_MEMORY\", \"summary\": \"...\"}"}]}
```

In a trace, an assistant turn is the reply text, not the API's content blocks. `fixture` says where
the replies came from: `recorded` (a `RecordingModel` wrote them) or `hand-written` (someone wrote
them in the shape of a recording). A trace file says which it is, and a reader should believe it,
not the directory it is in.

- `ReplayModel(path, system=...)` answers each request with the reply of the first unused recorded
  call whose request matches (its `messages`, and its `system` when the trace has one). Identical
  requests (a resample) replay in order; requests from concurrent threads may come in any order. A
  request that no unused call matches raises `ReplayMismatch`, an `AssertionError` whose message is a
  unified diff against the next unused call, so a changed prompt fails loudly. `assert_done()` fails
  when a recorded call was never requested; `remaining` counts them.
- `RecordingModel(inner, path)` sends through an `AnthropicModel` and rewrites `path` after each call,
  marked `"fixture": "recorded"`.
- `model_for(mode, trace, system=..., model=..., **kw)` returns the model for a mode name: `replay`,
  `record`, `anthropic`, or `fake` (`None`: the caller keeps its own fake).

The three demo apps use this: `APP_MODEL=replay|anthropic|record|fake` picks the model, tests and CI
default to `replay` on committed traces, the seeded-defect tests stay on each app's fake model (it can
be told to misbehave), and `pytest --record` with `ANTHROPIC_API_KEY` set records real traces. The
committed traces are hand-written fixtures, not recordings; see each app's README.

## Testing without the network

`AnthropicModel(client=...)` takes any object with `.messages.create(**kw)` returning something with
`.content` (a list of blocks with `.type` and `.text`) and `.stop_reason`. The library's tests use a
small stub, so they need neither the `anthropic` package nor a key.
