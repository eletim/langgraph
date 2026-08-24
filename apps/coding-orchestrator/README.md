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

MulmoTerminal integration is isolated behind `TerminalSessionClient`. Tests use `FakeTerminalSessionClient`, so they do not start MulmoTerminal, Codex, or Claude Code.
