# Coding Orchestrator

LangGraph workflow for a single B/C coding-agent loop:

```text
Task
  -> B Implementer
  -> C Reviewer
      -> APPROVED: done
      -> CHANGES_REQUESTED: B fixes, then C reviews again
      -> 4 reviews without approval: failed
```

This app intentionally lives outside `libs/` so it can evolve as application-specific orchestration code without changing LangGraph itself.

Terminal integration is isolated behind `TerminalSessionClient`. Production uses
`PurpleMuxCLIClient`; tests can use `FakeTerminalSessionClient`, so unit tests do not
start PurpleMux, Codex, or Claude Code. `MulmoTerminalHTTPClient` remains as a
deprecated compatibility client.

```python
from coding_orchestrator import CodingWorkflow, PurpleMuxCLIClient

client = PurpleMuxCLIClient("ws-example")
result = CodingWorkflow(client).invoke(cwd="/workspace/project", task="...")
```

PurpleMux runtime state and structured results are authoritative. The workflow does
not inspect tmux or parse terminal captures. `capture_screen()` exists only as an
explicit diagnostic operation and is not part of `TerminalSessionClient`.

`status` and `result` are read-only and retry once after a subprocess timeout by
default. `create`, `send`, `interrupt`, and `close` are never retried automatically:
a timeout raises `MutationOutcomeUnknown`, because the operation may already have
reached PurpleMux. Reconcile those cases with `status`/`result` (or tab listing for
`create` and `close`) before deciding whether to issue another mutation.

`CodingWorkflow.invoke()` closes every successfully created session after its final
state. Cleanup is best-effort: a close failure is reported as `cleanup_error` without
overwriting the implementation/review result; affected IDs are returned in
`unclean_session_ids` for explicit reconciliation. A session that fails its initial
ready wait is also closed immediately. Failed or timed-out closes are not blindly
retried.

## Live smoke checks

The live checks use only public PurpleMux CLI status and result contracts. They do
not inspect tmux, capture a pane, resolve provider session IDs, or read PurpleMux
internal files.

```bash
make live-smoke ARGS="lifecycle --workspace ws-example --cwd /workspace/project"
make live-smoke ARGS="interrupt --workspace ws-example --cwd /workspace/project"
make live-smoke ARGS="pre-e2e --workspace ws-example --cwd /workspace/project"
```

The lifecycle and interrupt modes stop at a freshly correlated
`ready-for-review`/`interrupt` status event and intentionally do not claim an agent
result. Interrupt mode also attempts a second turn on the same tab and reports
`reuse-unavailable` without failing the primary interrupt check if PurpleMux emits a
new interrupt for that turn. `pre-e2e` additionally creates the Claude reviewer and
verifies that it is ready, but does not send review input without the implementer
structured result.

Once PurpleMux structured result discovery is working, run the full workflow without
changing or mocking the client:

```bash
make live-smoke ARGS="full-e2e --workspace ws-example \
  --cwd /workspace/project --task 'Implement the requested change'"
```

`full-e2e` runs Codex implementation, reads its structured result, runs Claude review,
reads that structured result, follows the approval/fix branch, and checks for an
approved terminal state. A `not-ready` result remains a real failure; there is no
capture fallback.
