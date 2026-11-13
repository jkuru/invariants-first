# Replay traces

These traces are hand-written fixtures, not recordings (each file says `"fixture": "hand-written"`).
The requests were built by the app's own prompt code; the replies were written by hand in the shape
the Messages API returns for this app's `CauseReply` schema. `oom_format_fix.json` scripts a first
reply with a `confidence` field the closed schema forbids, so the parser error goes back as a second
user turn.

`pytest --record` (or `APP_MODEL=record`) with `ANTHROPIC_API_KEY` set calls Claude and rewrites
these files with real replies, marked `"fixture": "recorded"`.
