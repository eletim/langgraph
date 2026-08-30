from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import pytest

from coding_orchestrator.client import (
    CreateSessionRequest,
    MutationOutcomeUnknown,
    ResultNotReady,
    SessionReadyTimeout,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
)
from coding_orchestrator.purplemux import PurpleMuxCLIClient


def completed(data: object, *, returncode: int = 0, stderr: str = ""):
    stdout = data if isinstance(data, str) else json.dumps(data)
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeRunner:
    def __init__(
        self,
        outcomes: Sequence[
            subprocess.CompletedProcess[str] | subprocess.TimeoutExpired
        ],
    ) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert text is True
        assert check is False
        self.calls.append(list(args))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, subprocess.TimeoutExpired):
            raise outcome
        return outcome


def client(runner: FakeRunner, **kwargs: object) -> PurpleMuxCLIClient:
    return PurpleMuxCLIClient(
        "ws-test",
        poll_interval_seconds=0,
        runner=runner,
        sleep=lambda _: None,
        **kwargs,
    )


def request(worker: str = "codex") -> CreateSessionRequest:
    return CreateSessionRequest(worker, "/workspace/project", worker)


def test_create_response_parse_and_codex_panel_type() -> None:
    runner = FakeRunner([completed({"tabId": "tab-123"})])

    assert client(runner).create_session(request()) == "tab-123"
    assert runner.calls == [
        ["purplemux", "tab", "create", "-w", "ws-test", "-t", "codex-cli"]
    ]


def test_create_uses_claude_panel_type() -> None:
    runner = FakeRunner([completed({"tabId": "tab-claude"})])

    client(runner).create_session(request("claude-code"))

    assert runner.calls[0][-1] == "claude-code"


def test_wait_until_ready_polls_until_idle() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "inactive", "alive": True}),
            completed({"cliState": "idle", "alive": True}),
        ]
    )

    client(runner).wait_until_ready("tab-1", 1)

    assert len(runner.calls) == 2
    assert all(call[1:3] == ["tab", "status"] for call in runner.calls)


def test_wait_until_ready_accepts_ready_for_review() -> None:
    runner = FakeRunner([completed({"cliState": "ready-for-review", "alive": True})])

    client(runner).wait_until_ready("tab-1", 1)


def test_wait_for_turn_completion_observes_busy_then_ready_for_review() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed({"cliState": "idle", "alive": True}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "done",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert len(runner.calls) == 7


def test_completion_waits_for_structured_result_to_become_ready() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "ready-for-review", "alive": True, "eventSeq": 1}),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                }
            ),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                }
            ),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "done",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "done"
    assert len(runner.calls) == 8


def test_state_completion_rejects_stale_ready_for_review() -> None:
    runner = FakeRunner(
        [
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 10,
                    "readyForReviewAt": 100,
                    "lastEvent": {"name": "stop", "seq": 10},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                }
            ),
            completed({"sent": True}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 11,
                    "readyForReviewAt": 100,
                    "lastEvent": {"name": "user-prompt-submit", "seq": 11},
                }
            ),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 12,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 12},
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    cli.wait_for_turn_state_completion("tab-1", 1)

    assert len(runner.calls) == 5


def test_state_completion_accepts_fresh_event_when_busy_poll_is_missed() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": 200,
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    cli.wait_for_turn_state_completion("tab-1", 1)

    assert len(runner.calls) == 4


def test_short_turn_can_complete_when_busy_poll_is_missed() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "fast",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "fast"


def test_older_completion_timestamp_is_not_fresh() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "ready-for-review", "alive": True, "eventSeq": 1}),
            completed(
                {
                    "status": "completed",
                    "text": "baseline",
                    "completionTimestamp": 2,
                }
            ),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "older",
                    "completionTimestamp": 1,
                }
            ),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "fresh",
                    "completionTimestamp": 3,
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "fresh"


def test_not_ready_result_remains_a_blocker_without_capture_fallback() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed({"cliState": "ready-for-review", "alive": True, "eventSeq": 3}),
            completed(
                {
                    "status": "not-ready",
                    "reason": "agent-session-id-unavailable",
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="did not complete"):
        cli.wait_for_turn_completion("tab-1", 0)

    assert all(call[2] != "capture" for call in runner.calls)


def test_wait_for_turn_completion_raises_for_needs_input() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "needs-input", "alive": True}),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerNeedsInput, match="needs input"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_wait_for_turn_completion_raises_when_agent_becomes_inactive() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "inactive", "alive": True}),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="became inactive"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_wait_for_turn_completion_detects_interrupted_result() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "idle", "alive": True}),
            completed({"status": "interrupted", "reason": "turn-interrupted"}),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerInterrupted, match="interrupted"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_interrupt_hook_status_maps_to_worker_interrupted() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "lastEvent": {"name": "interrupt", "seq": 3},
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerInterrupted, match="interrupted"):
        cli.wait_for_turn_state_completion("tab-1", 1)


def test_session_can_start_another_turn_after_interrupt() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "lastEvent": {"name": "interrupt", "seq": 3},
                }
            ),
            completed({"cliState": "idle", "alive": True, "eventSeq": 3}),
            completed({"status": "interrupted"}),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 4}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 5,
                    "readyForReviewAt": 200,
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "first")
    with pytest.raises(WorkerInterrupted):
        cli.wait_for_turn_state_completion("tab-1", 1)

    cli.send_input("tab-1", "second")
    cli.wait_for_turn_state_completion("tab-1", 1)

    send_calls = [call for call in runner.calls if call[2] == "send"]
    assert len(send_calls) == 2


def test_previous_completed_result_with_new_interrupt_is_not_accepted() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "ready-for-review", "alive": True, "eventSeq": 1}),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                }
            ),
            completed({"sent": True}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "idle", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                    "interrupted": True,
                }
            ),
        ]
    )

    cli = client(runner)
    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerInterrupted, match="interrupted"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_read_result_completed_returns_text() -> None:
    runner = FakeRunner(
        [completed({"status": "completed", "text": "implementation complete"})]
    )

    assert client(runner).read_result("tab-1") == "implementation complete"


def test_read_result_not_ready_is_explicit() -> None:
    runner = FakeRunner(
        [completed({"status": "not-ready", "reason": "assistant-response-unavailable"})]
    )

    with pytest.raises(ResultNotReady, match="assistant-response-unavailable"):
        client(runner).read_result("tab-1")


@pytest.mark.parametrize("status", ["not-applicable", "unavailable"])
def test_read_result_unavailable_statuses_fail(status: str) -> None:
    runner = FakeRunner([completed({"status": status, "reason": "reason"})])

    with pytest.raises(WorkerFailure, match=status):
        client(runner).read_result("tab-1")


def test_ready_timeout() -> None:
    runner = FakeRunner([completed({"cliState": "inactive", "alive": True})])

    with pytest.raises(SessionReadyTimeout, match="not ready"):
        client(runner).wait_until_ready("tab-1", 0)


def test_dead_runtime_is_not_treated_as_ready() -> None:
    runner = FakeRunner([completed({"cliState": "idle", "alive": False})])

    with pytest.raises(WorkerFailure, match="entered idle"):
        client(runner).wait_until_ready("tab-1", 1)


@pytest.mark.parametrize(
    "state", ["cancelled", "dead", "error", "failed", "stopped", "exited"]
)
def test_failed_runtime_states_are_worker_failures(state: str) -> None:
    runner = FakeRunner([completed({"cliState": state, "alive": True})])

    with pytest.raises(WorkerFailure, match=f"entered {state}"):
        client(runner).wait_until_ready("tab-1", 1)


def test_missing_tab_cli_error_is_worker_failure() -> None:
    runner = FakeRunner([completed({}, returncode=1, stderr="tab not found")])

    with pytest.raises(WorkerFailure, match="tab not found"):
        client(runner).wait_until_ready("missing-tab", 1)


def test_read_only_timeout_is_retried_then_fails() -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    runner = FakeRunner([timeout, timeout])

    with pytest.raises(WorkerFailure, match="status timed out"):
        client(runner).wait_until_ready("tab-1", 1)

    assert len(runner.calls) == 2


@pytest.mark.parametrize("operation", ["result", "capture"])
def test_read_only_timeout_is_retried_successfully(operation: str) -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    response = (
        {"status": "completed", "text": "done"}
        if operation == "result"
        else {"content": "diagnostic"}
    )
    runner = FakeRunner([timeout, completed(response)])
    cli = client(runner)

    if operation == "result":
        assert cli.read_result("tab-1") == "done"
    else:
        assert cli.capture_screen("tab-1") == "diagnostic"

    assert len(runner.calls) == 2


def test_cli_non_zero_exit_includes_stderr() -> None:
    runner = FakeRunner([completed({}, returncode=2, stderr="server unavailable")])

    with pytest.raises(WorkerFailure, match="server unavailable"):
        client(runner).read_result("tab-1")


def test_malformed_json() -> None:
    runner = FakeRunner([completed("not-json")])

    with pytest.raises(WorkerFailure, match="malformed JSON"):
        client(runner).read_result("tab-1")


def test_interrupt_uses_cli() -> None:
    runner = FakeRunner([completed({"ok": True})])

    client(runner).interrupt("tab-1")

    assert runner.calls[0][1:] == [
        "tab",
        "interrupt",
        "-w",
        "ws-test",
        "tab-1",
    ]


def test_close_uses_cli_and_accepts_non_json_ok() -> None:
    runner = FakeRunner([completed("ok\n")])

    client(runner).close_session("tab-1")

    assert runner.calls[0][1:] == ["tab", "close", "-w", "ws-test", "tab-1"]


def test_mutation_timeout_is_not_retried() -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 1}),
            completed({"status": "not-ready"}),
            timeout,
            completed({"ok": True}),
        ]
    )

    with pytest.raises(MutationOutcomeUnknown, match="outcome is unknown"):
        client(runner).send_input("tab-1", "do work")

    send_calls = [call for call in runner.calls if call[2] == "send"]
    assert len(send_calls) == 1


@pytest.mark.parametrize("operation", ["create", "interrupt", "close"])
def test_other_mutation_timeouts_are_not_retried(operation: str) -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    runner = FakeRunner([timeout, completed({"ok": True})])
    cli = client(runner)

    with pytest.raises(MutationOutcomeUnknown, match="outcome is unknown"):
        if operation == "create":
            cli.create_session(request())
        elif operation == "interrupt":
            cli.interrupt("tab-1")
        else:
            cli.close_session("tab-1")

    assert len(runner.calls) == 1
    assert runner.calls[0][2] == operation


def test_capture_is_separate_from_result() -> None:
    runner = FakeRunner([completed({"content": "diagnostic pane"})])

    assert client(runner).capture_screen("tab-1") == "diagnostic pane"
    assert runner.calls[0][2] == "capture"
