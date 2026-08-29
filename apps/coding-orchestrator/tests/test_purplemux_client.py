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

    assert len(runner.calls) == 6


def test_completion_waits_for_structured_result_to_become_ready() -> None:
    runner = FakeRunner(
        [
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
    assert len(runner.calls) == 7


def test_short_turn_can_complete_when_busy_poll_is_missed() -> None:
    runner = FakeRunner(
        [
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


def test_wait_for_turn_completion_raises_for_needs_input() -> None:
    runner = FakeRunner(
        [
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


def test_previous_completed_result_with_new_interrupt_is_not_accepted() -> None:
    runner = FakeRunner(
        [
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


def test_read_only_timeout_is_retried_then_fails() -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    runner = FakeRunner([timeout, timeout])

    with pytest.raises(WorkerFailure, match="status timed out"):
        client(runner).wait_until_ready("tab-1", 1)

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
        [completed({"status": "not-ready"}), timeout, completed({"ok": True})]
    )

    with pytest.raises(MutationOutcomeUnknown, match="outcome is unknown"):
        client(runner).send_input("tab-1", "do work")

    send_calls = [call for call in runner.calls if call[2] == "send"]
    assert len(send_calls) == 1


def test_capture_is_separate_from_result() -> None:
    runner = FakeRunner([completed({"content": "diagnostic pane"})])

    assert client(runner).capture_screen("tab-1") == "diagnostic pane"
    assert runner.calls[0][2] == "capture"
