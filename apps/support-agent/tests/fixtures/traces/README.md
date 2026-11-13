# Replay traces

These traces are hand-written fixtures, not recordings (each file says `"fixture": "hand-written"`).
The requests were built by the app's own prompt code; the replies were written by hand in the shape
the Messages API returns for this app's `Plan` schema.

- `refund.json`, `credit_and_cancel.json`, `escalate.json`, `refund_format_fix.json`: the model
  answers with the plan as JSON text (`APP_REPLY=json`). `refund_format_fix.json` scripts a first
  reply that wraps the JSON in prose and a code fence, so the parser error goes back as a second
  user turn.
- `tool_refund.json`, `tool_escalate.json`, `tool_bad_call_fixed.json`, `tool_resample.json`: the
  model answers by calling the `submit_plan` tool (`APP_REPLY=tool`); each request records `tools`
  and `tool_choice`, and each reply is the `tool_use` block. `tool_bad_call_fixed.json` scripts a
  first call with a field the closed `Plan` does not have, so the parser error goes back as a
  `tool_result` with `is_error: true` for that `tool_use` id; `tool_resample.json` a first call
  that refunds more than is left of the order, which PLAN-1 resamples in a new conversation. The
  `toolu_hw_*` ids mark them as hand-written.

`pytest --record` (or `APP_MODEL=record`) with `ANTHROPIC_API_KEY` set calls Claude and rewrites
these files with real replies, marked `"fixture": "recorded"` (the scripted failure flows are skipped).
