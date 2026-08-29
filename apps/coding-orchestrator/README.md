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
