from __future__ import annotations

import pytest

from coding_orchestrator.client import (
    FakeTerminalSessionClient,
    FakeTurn,
    MulmoTerminalHTTPClient,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
)
from coding_orchestrator.workflow import CodingWorkflow, CodingWorkflowConfig


def run_workflow(client: FakeTerminalSessionClient):
    return CodingWorkflow(
        client,
        CodingWorkflowConfig(
            ready_timeout_seconds=0.01,
            turn_timeout_seconds=0.01,
        ),
    ).invoke(cwd="/workspace/project", task="Implement the requested change")


def test_implement_to_approved() -> None:
    client = FakeTerminalSessionClient(
        {
            "codex": ["implementation complete"],
            "claude-code": ["DECISION: APPROVED"],
        }
    )

    result = run_workflow(client)

    assert result["final_status"] == "APPROVED"
    assert result["review_count"] == 1
    assert result["review_result"] == "APPROVED"
    assert result["implementer_session_id"] == "codex-1"
    assert result["reviewer_session_id"] == "claude-code-1"
    assert client.closed_sessions == ["codex-1", "claude-code-1"]


def test_review_ng_fix_approved_reuses_implementer_session() -> None:
    client = FakeTerminalSessionClient(
        {
            "codex": ["implementation complete", "fix complete"],
            "claude-code": [
                "DECISION: CHANGES_REQUESTED\nFix the missing test.",
                "DECISION: APPROVED",
            ],
        }
    )

    result = run_workflow(client)

    assert result["final_status"] == "APPROVED"
    assert result["review_count"] == 2
    codex_inputs = [
        (session_id, text)
        for session_id, worker, text in client.inputs
        if worker == "codex"
    ]
    assert [session_id for session_id, _ in codex_inputs] == ["codex-1", "codex-1"]
    assert "Reviewer output:" in codex_inputs[1][1]
    assert "Fix the missing test." in codex_inputs[1][1]
    assert client.closed_sessions == ["codex-1", "claude-code-1"]


def test_review_four_failures_ends_failed() -> None:
    client = FakeTerminalSessionClient(
        {
            "codex": [
                "implementation complete",
                "fix 1 complete",
                "fix 2 complete",
                "fix 3 complete",
            ],
            "claude-code": [
                "DECISION: CHANGES_REQUESTED\nissue 1",
                "DECISION: CHANGES_REQUESTED\nissue 2",
                "DECISION: CHANGES_REQUESTED\nissue 3",
                "DECISION: CHANGES_REQUESTED\nissue 4",
            ],
        }
    )

    result = run_workflow(client)

    assert result["final_status"] == "FAILED"
    assert result["review_count"] == 4
    assert result["error"] == "maximum review attempts reached"
    assert client.closed_sessions == ["codex-1", "claude-code-1"]


def test_session_ready_timeout_fails_workflow() -> None:
    client = FakeTerminalSessionClient(
        {"codex": ["implementation complete"]},
        ready_timeout_workers={"codex"},
    )

    result = run_workflow(client)

    assert result["final_status"] == "FAILED"
    assert "was not ready" in result["error"]
    assert "review_count" not in result or result["review_count"] == 0
    assert client.closed_sessions == ["codex-1"]


def test_ready_timeout_reports_close_failure_without_blind_retry() -> None:
    client = FakeTerminalSessionClient(
        {"codex": ["implementation complete"]},
        ready_timeout_workers={"codex"},
        close_failure_workers={"codex"},
    )

    result = run_workflow(client)

    assert result["final_status"] == "FAILED"
    assert "was not ready" in result["error"]
    assert "close failed" in result["cleanup_error"]
    assert result["unclean_session_ids"] == ["codex-1"]
    assert client.closed_sessions == []


def test_worker_failure_fails_workflow() -> None:
    client = FakeTerminalSessionClient(
        {
            "codex": [FakeTurn(fail=True)],
        }
    )

    result = run_workflow(client)

    assert result["final_status"] == "FAILED"
    assert "codex turn failed" in result["error"]
    assert client.closed_sessions == ["codex-1"]


def test_implementer_needs_input_fails_without_polling_forever() -> None:
    client = FakeTerminalSessionClient(
        {"codex": [FakeTurn(error=WorkerNeedsInput("codex needs input"))]}
    )

    result = run_workflow(client)

    assert result["final_status"] == "FAILED"
    assert result["error"] == "codex needs input"
    assert client.closed_sessions == ["codex-1"]
    assert not any(worker == "claude-code" for _, worker, _ in client.inputs)


def test_reviewer_interrupt_fails_and_cleans_up_both_sessions() -> None:
    client = FakeTerminalSessionClient(
        {
            "codex": ["implementation complete"],
            "claude-code": [FakeTurn(error=WorkerInterrupted("reviewer interrupted"))],
        }
    )

    result = run_workflow(client)

    assert result["final_status"] == "FAILED"
    assert result["error"] == "reviewer interrupted"
    assert client.closed_sessions == ["codex-1", "claude-code-1"]


def test_unexpected_exception_cleans_up_all_created_sessions() -> None:
    class UnexpectedReviewerResultClient(FakeTerminalSessionClient):
        def __init__(self) -> None:
            super().__init__(
                {
                    "codex": ["implementation complete"],
                    "claude-code": ["DECISION: APPROVED"],
                }
            )
            self.result_reads = 0

        def read_result(self, session_id: str) -> str:
            self.result_reads += 1
            if self.result_reads == 2:
                raise ValueError("unexpected reviewer result failure")
            return super().read_result(session_id)

    client = UnexpectedReviewerResultClient()

    with pytest.raises(ValueError, match="unexpected reviewer result failure"):
        run_workflow(client)

    assert client.closed_sessions == ["codex-1", "claude-code-1"]


def test_cleanup_failure_is_reported_without_overwriting_workflow_result() -> None:
    client = FakeTerminalSessionClient(
        {
            "codex": ["implementation complete"],
            "claude-code": ["DECISION: APPROVED"],
        },
        close_failure_workers={"claude-code"},
    )

    result = run_workflow(client)

    assert result["final_status"] == "APPROVED"
    assert "claude-code-1 close failed" in result["cleanup_error"]
    assert result["unclean_session_ids"] == ["claude-code-1"]
    assert client.closed_sessions == ["codex-1"]


def test_one_cleanup_failure_does_not_stop_the_other_cleanup() -> None:
    client = FakeTerminalSessionClient(
        {
            "codex": ["implementation complete"],
            "claude-code": ["DECISION: APPROVED"],
        },
        close_failure_workers={"codex"},
    )

    result = run_workflow(client)

    assert result["final_status"] == "APPROVED"
    assert "codex-1 close failed" in result["cleanup_error"]
    assert result["unclean_session_ids"] == ["codex-1"]
    assert client.closed_sessions == ["claude-code-1"]


def test_http_client_treats_turn_failure_as_worker_failure() -> None:
    client = MulmoTerminalHTTPClient("http://terminal.local", poll_interval_seconds=0)
    client._get_session = lambda session_id: {  # type: ignore[method-assign]
        "status": "idle",
        "turn_status": "failed",
    }

    with pytest.raises(WorkerFailure, match="turn entered failed"):
        client.wait_for_turn_completion("session-1", timeout_seconds=1)
