# PurpleMux Python Client

## Purpose

`purplemux-client` is a thin Python adapter for the shared PurpleMux runtime. It
contains no fixed coding workflow: callers may write ordinary Python or use a
LangGraph `StateGraph` to compose any workflow they need.

## Architecture

```text
Python workflow
  -> PurpleMuxCLIClient
  -> public PurpleMux CLI
  -> shared PurpleMux runtime
  -> provider / StatusManager / timeline
  -> tmux
  -> Codex / Claude
```

The package depends only on the Python standard library. It does not import
LangGraph.

## Example

```python
from purplemux_client import CreateSessionRequest, PurpleMuxCLIClient

client = PurpleMuxCLIClient("ws-example")
session_id = client.create_session(
    CreateSessionRequest(
        worker="codex",
        cwd="/workspace/project",
        command="codex",
    )
)

try:
    client.wait_until_ready(session_id, 60)
    client.send_input(session_id, "Implement the requested change.")
    client.wait_for_turn_completion(session_id, 900)
    print(client.read_result(session_id))
finally:
    client.close_session(session_id)
```

Use `worker="claude-code"` and `command="claude"` for Claude Code. PurpleMux
owns the provider launch command and uses the selected workspace's configured
directory. `cwd` and `command` express caller intent; the current public
`tab create` contract does not forward arbitrary commands or directories.

The public operations are:

- `create_session()`
- `read_status()`
- `wait_until_ready()`
- `send_input()`
- `wait_for_turn_completion()`
- `read_result()`
- `interrupt()`
- `close_session()`
- `capture_screen()` for diagnostics only

## Guarantees

- Uses only the public `purplemux` CLI.
- Never calls tmux or private PurpleMux HTTP APIs directly.
- Never reads files under `~/.purplemux`.
- Never parses terminal screen text to infer agent state or output.
- Uses live StatusManager fields returned by `tab status`.
- Uses structured provider timeline output returned by `tab result`.
- Correlates turns with `eventSeq`, `readyForReviewAt`, and
  `completionTimestamp`, rejecting stale ready states and results.
- Reports needs-input, interruption, inactive/dead runtime, malformed JSON,
  non-zero CLI exits, and subprocess timeouts explicitly.
- Retries timed-out read-only status/result/capture calls only.
- Never blindly retries create/send/interrupt/close mutations.
- Raises `MutationOutcomeUnknown` when a mutation times out after its remote
  outcome becomes unknowable.

`capture_screen()` is intentionally separate from `read_result()`. Captured
terminal text is diagnostic data and is never an agent-result fallback.

## Live smoke

The lifecycle smoke creates one Codex session, waits for readiness, sends one
prompt, waits for a fresh structured completion, verifies the exact result, and
closes the session:

```bash
make live-smoke ARGS="lifecycle \
  --workspace ws-example \
  --cwd /workspace/project"
```

It uses no capture fallback, tmux access, private API, or PurpleMux internal
file.
