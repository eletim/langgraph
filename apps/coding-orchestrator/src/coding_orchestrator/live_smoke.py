from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from coding_orchestrator.client import (
    CreateSessionRequest,
    TerminalSessionError,
    WorkerFailure,
    WorkerInterrupted,
)
from coding_orchestrator.purplemux import PurpleMuxCLIClient
from coding_orchestrator.workflow import CodingWorkflow, CodingWorkflowConfig


def _request(worker: str, cwd: str, role: str) -> CreateSessionRequest:
    command = "codex" if worker == "codex" else "claude"
    return CreateSessionRequest(worker, cwd, command, {"role": role})


def _record(event: str, **details: Any) -> None:
    print(json.dumps({"event": event, **details}, sort_keys=True), flush=True)


def _close_and_verify(client: PurpleMuxCLIClient, session_id: str) -> None:
    client.close_session(session_id)
    try:
        client.read_status(session_id)
    except WorkerFailure as exc:
        if "not found" not in str(exc).lower():
            raise WorkerFailure(
                f"could not verify that session {session_id} disappeared: {exc}"
            ) from exc
        _record("closed", session_id=session_id, status_unavailable=True)
        return
    raise WorkerFailure(f"closed session {session_id} is still visible")


def _close_all(client: PurpleMuxCLIClient, session_ids: list[str]) -> None:
    cleanup_errors: list[str] = []
    for session_id in dict.fromkeys(session_ids):
        try:
            _close_and_verify(client, session_id)
        except TerminalSessionError as exc:
            cleanup_errors.append(f"{session_id}: {exc}")
    if cleanup_errors:
        raise WorkerFailure(f"live smoke cleanup failed: {'; '.join(cleanup_errors)}")


def _create_ready(
    client: PurpleMuxCLIClient, worker: str, cwd: str, role: str, timeout: float
) -> str:
    session_id = client.create_session(_request(worker, cwd, role))
    _record("created", worker=worker, session_id=session_id)
    try:
        client.wait_until_ready(session_id, timeout)
        status = client.read_status(session_id)
    except BaseException as ready_error:
        try:
            _close_all(client, [session_id])
        except TerminalSessionError as cleanup_error:
            raise WorkerFailure(
                f"session {session_id} failed before ready: {ready_error}; "
                f"cleanup also failed: {cleanup_error}"
            ) from ready_error
        raise
    _record(
        "ready",
        worker=worker,
        session_id=session_id,
        cli_state=status.get("cliState"),
        event_seq=status.get("eventSeq"),
    )
    return session_id


def lifecycle_smoke(
    client: PurpleMuxCLIClient, cwd: str, ready_timeout: float, turn_timeout: float
) -> None:
    session_id = _create_ready(client, "codex", cwd, "lifecycle-smoke", ready_timeout)
    try:
        client.send_input(
            session_id,
            "Run `sleep 8` in the terminal, then reply with exactly "
            "LIFECYCLE_SMOKE_OK.",
        )
        client.wait_until_turn_busy(session_id, turn_timeout)
        busy = client.read_status(session_id)
        _record("busy", session_id=session_id, event_seq=busy.get("eventSeq"))
        client.wait_for_turn_state_completion(session_id, turn_timeout)
        ready = client.read_status(session_id)
        _record(
            "state-completed",
            session_id=session_id,
            cli_state=ready.get("cliState"),
            event_seq=ready.get("eventSeq"),
            result_intentionally_unread=True,
        )
    finally:
        _close_all(client, [session_id])


def interrupt_smoke(
    client: PurpleMuxCLIClient, cwd: str, ready_timeout: float, turn_timeout: float
) -> None:
    session_id = _create_ready(client, "codex", cwd, "interrupt-smoke", ready_timeout)
    try:
        client.send_input(
            session_id,
            "Run `sleep 60` in the terminal, then reply INTERRUPT_TASK_FINISHED.",
        )
        client.wait_until_turn_busy(session_id, turn_timeout)
        _record("busy-before-interrupt", session_id=session_id)
        client.interrupt(session_id)
        _record("interrupt-sent", session_id=session_id)
        try:
            client.wait_for_turn_state_completion(session_id, turn_timeout)
        except WorkerInterrupted:
            status = client.read_status(session_id)
            _record(
                "interrupted",
                session_id=session_id,
                cli_state=status.get("cliState"),
                last_event=status.get("lastEvent"),
            )
        else:
            raise WorkerFailure("interrupt was not observed from PurpleMux status")

        client.send_input(
            session_id,
            "Run `sleep 8` in the terminal, then reply REUSE_AFTER_INTERRUPT_OK.",
        )
        client.wait_until_turn_busy(session_id, turn_timeout)
        try:
            client.wait_for_turn_state_completion(session_id, turn_timeout)
        except WorkerInterrupted:
            status = client.read_status(session_id)
            _record(
                "reuse-unavailable",
                session_id=session_id,
                cli_state=status.get("cliState"),
                event_seq=status.get("eventSeq"),
                last_event=status.get("lastEvent"),
                reason="PurpleMux emitted a fresh interrupt for the reuse turn",
            )
            return
        _record("reused-after-interrupt", session_id=session_id)
    finally:
        _close_all(client, [session_id])


def pre_e2e_smoke(
    client: PurpleMuxCLIClient, cwd: str, ready_timeout: float, turn_timeout: float
) -> None:
    implementer_id = _create_ready(
        client, "codex", cwd, "pre-e2e-implementer", ready_timeout
    )
    reviewer_id: str | None = None
    try:
        client.send_input(
            implementer_id,
            "Run `sleep 8` in the terminal, then reply PRE_E2E_IMPLEMENTER_OK.",
        )
        client.wait_until_turn_busy(implementer_id, turn_timeout)
        client.wait_for_turn_state_completion(implementer_id, turn_timeout)
        _record("implementer-state-completed", session_id=implementer_id)
        reviewer_id = _create_ready(
            client, "claude-code", cwd, "pre-e2e-reviewer", ready_timeout
        )
        _record("pre-e2e-ready", reviewer_session_id=reviewer_id)
    finally:
        session_ids = [implementer_id]
        if reviewer_id is not None:
            session_ids.insert(0, reviewer_id)
        _close_all(client, session_ids)


def full_e2e(
    client: PurpleMuxCLIClient,
    cwd: str,
    task: str,
    ready_timeout: float,
    turn_timeout: float,
) -> None:
    result = CodingWorkflow(
        client,
        CodingWorkflowConfig(
            ready_timeout_seconds=ready_timeout,
            turn_timeout_seconds=turn_timeout,
        ),
    ).invoke(cwd=cwd, task=task)
    _record("full-e2e-result", workflow=result)
    if result.get("final_status") != "APPROVED":
        raise WorkerFailure(f"full E2E did not approve: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live PurpleMux smoke checks without screen capture fallback."
    )
    parser.add_argument(
        "mode", choices=("lifecycle", "interrupt", "pre-e2e", "full-e2e")
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--task", default="Inspect the repository and make no changes.")
    parser.add_argument("--ready-timeout", type=float, default=60.0)
    parser.add_argument("--turn-timeout", type=float, default=180.0)
    args = parser.parse_args()

    client = PurpleMuxCLIClient(args.workspace, poll_interval_seconds=1.0)
    if args.mode == "lifecycle":
        lifecycle_smoke(client, args.cwd, args.ready_timeout, args.turn_timeout)
    elif args.mode == "interrupt":
        interrupt_smoke(client, args.cwd, args.ready_timeout, args.turn_timeout)
    elif args.mode == "pre-e2e":
        pre_e2e_smoke(client, args.cwd, args.ready_timeout, args.turn_timeout)
    else:
        full_e2e(
            client,
            args.cwd,
            args.task,
            args.ready_timeout,
            args.turn_timeout,
        )


if __name__ == "__main__":
    main()
