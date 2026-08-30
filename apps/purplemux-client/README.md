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

## Python API

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

## Local Python Runner UI

The package also includes a deliberately small local UI that runs Python code
and displays its standard output, standard error, and exit code. It is a generic
Python runner, not a PurpleMux-specific workflow editor: it has no provider
forms, workspace picker, review loop, graph, persistence, or workflow
semantics.

Start it from this directory:

```bash
make web
```

Equivalently:

```bash
uv run python -m purplemux_client.web
```

Then open <http://127.0.0.1:8765>. The host and port can be changed with, for
example, `make web ARGS="--host 127.0.0.1 --port 9000"`.

The editor can run any Python available in the current environment:

```python
print("HELLO_RUNNER")
```

It can also import this package and use the regular Python API:

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
    client.send_input(session_id, "exactly HELLO とだけ返してください")
    client.wait_for_turn_completion(session_id, 900)
    print(client.read_result(session_id))
finally:
    client.close_session(session_id)
```

Only one Python process runs at a time. Stop and server shutdown terminate the
process group started by the runner, including subprocesses created by the
script. PurpleMux sessions created by user code remain the script's
responsibility; the runner does not inspect or clean them up.

The polling API consists of `POST /api/run`, `GET /api/status`,
`GET /api/output`, and `POST /api/stop`. The browser first obtains the
per-server request token from `GET /api/token`; mutation requests without that
token or with a foreign browser origin are rejected.

### Security

This is a trusted local development tool that executes arbitrary Python code.
It binds to `127.0.0.1` by default. It provides no sandbox, authentication, user
isolation, or remote-execution security and must not be exposed directly to the
public internet. The Runner UI requires a POSIX operating system so it can
provide process-group cleanup; this restriction does not apply to the Python
client API itself.
