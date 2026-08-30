from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from coding_orchestrator.client import (
    CreateSessionRequest,
    MutationOutcomeUnknown,
    ResultNotReady,
    SessionReadyTimeout,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
)


class SubprocessRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


_PANEL_TYPES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex-cli",
    "codex-cli": "codex-cli",
}
_READY_STATES = {"idle", "ready-for-review"}
_FAILED_STATES = {"cancelled", "dead", "error", "failed", "stopped", "exited"}
_RESULT_STATUSES = {
    "completed",
    "not-ready",
    "interrupted",
    "not-applicable",
    "unavailable",
}


@dataclass(frozen=True)
class _TurnBaseline:
    completion_timestamp: int | float | None
    event_seq: int | None
    ready_for_review_at: int | float | None


class PurpleMuxCLIClient:
    """Production terminal client backed only by PurpleMux CLI contracts."""

    def __init__(
        self,
        workspace_id: str,
        *,
        executable: str = "purplemux",
        poll_interval_seconds: float = 1.0,
        command_timeout_seconds: float = 30.0,
        read_timeout_retries: int = 1,
        runner: SubprocessRunner = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must not be negative")
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if read_timeout_retries < 0:
            raise ValueError("read_timeout_retries must not be negative")
        self.workspace_id = workspace_id
        self.executable = executable
        self.poll_interval_seconds = poll_interval_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.read_timeout_retries = read_timeout_retries
        self._runner = runner
        self._sleep = sleep
        self._monotonic = monotonic
        self._turn_baselines: dict[str, _TurnBaseline] = {}
        self._completed_turns: dict[str, dict[str, Any]] = {}

    def create_session(self, request: CreateSessionRequest) -> str:
        panel_type = _PANEL_TYPES.get(request.worker.lower())
        if panel_type is None:
            panel_type = _PANEL_TYPES.get(request.command.lower())
        if panel_type is None:
            raise WorkerFailure(
                f"unsupported PurpleMux worker {request.worker!r}; "
                "expected codex or claude-code"
            )
        data = self._run_json(
            ["tab", "create", "-w", self.workspace_id, "-t", panel_type],
            operation="create",
            read_only=False,
        )
        session_id = data.get("tabId") or data.get("tab_id") or data.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise WorkerFailure("PurpleMux create did not return a tab ID")
        return session_id

    def wait_until_ready(self, session_id: str, timeout_seconds: float) -> None:
        deadline = self._monotonic() + timeout_seconds
        while True:
            status = self._status(session_id)
            state = self._state(status, session_id)
            self._raise_abnormal_state(session_id, state, status)
            if state in _READY_STATES:
                return
            if self._monotonic() >= deadline:
                raise SessionReadyTimeout(
                    f"session {session_id} was not ready within {timeout_seconds}s "
                    f"(last cliState={state})"
                )
            self._sleep(self.poll_interval_seconds)

    def send_input(self, session_id: str, text: str) -> None:
        if not text:
            raise ValueError("text must not be empty")
        baseline = self._read_turn_baseline(session_id)
        self._run_json(
            ["tab", "send", "-w", self.workspace_id, session_id, text],
            operation="send",
            read_only=False,
        )
        self._turn_baselines[session_id] = baseline
        self._completed_turns.pop(session_id, None)

    def read_status(self, session_id: str) -> dict[str, Any]:
        """Read PurpleMux runtime status without deriving state from tmux details."""
        return self._status(session_id)

    def wait_for_turn_state_completion(
        self, session_id: str, timeout_seconds: float
    ) -> None:
        """Wait for a fresh ready-for-review state without reading agent output.

        This supports lifecycle diagnostics while structured result is unavailable;
        it does not satisfy `TerminalSessionClient.wait_for_turn_completion`.
        """
        self._wait_for_turn(session_id, timeout_seconds, require_result=False)

    def wait_until_turn_busy(self, session_id: str, timeout_seconds: float) -> None:
        """Wait until PurpleMux reports that the pending turn is busy."""
        baseline = self._turn_baselines.get(session_id)
        if baseline is None:
            raise WorkerFailure(f"session {session_id} has no pending input")
        deadline = self._monotonic() + timeout_seconds
        last_state = "unknown"
        while True:
            status = self._status(session_id)
            state = self._state(status, session_id)
            last_state = state
            self._raise_abnormal_state(session_id, state, status)
            if self._is_fresh_interrupt(status, baseline):
                raise WorkerInterrupted(f"session {session_id} turn was interrupted")
            if state == "busy":
                return
            if self._monotonic() >= deadline:
                raise WorkerFailure(
                    f"session {session_id} did not become busy within "
                    f"{timeout_seconds}s (last cliState={last_state})"
                )
            self._sleep(self.poll_interval_seconds)

    def wait_for_turn_completion(self, session_id: str, timeout_seconds: float) -> None:
        self._wait_for_turn(session_id, timeout_seconds, require_result=True)

    def _wait_for_turn(
        self,
        session_id: str,
        timeout_seconds: float,
        *,
        require_result: bool,
    ) -> None:
        deadline = self._monotonic() + timeout_seconds
        baseline = self._turn_baselines.get(session_id)
        if baseline is None:
            raise WorkerFailure(f"session {session_id} has no pending input")
        saw_busy = False
        last_state = "unknown"
        while True:
            status = self._status(session_id)
            state = self._state(status, session_id)
            last_state = state
            self._raise_abnormal_state(session_id, state, status)
            if self._is_fresh_interrupt(status, baseline):
                raise WorkerInterrupted(f"session {session_id} turn was interrupted")
            if state == "busy":
                saw_busy = True
            elif state == "inactive":
                raise WorkerFailure(
                    f"session {session_id} agent became inactive during its turn"
                )
            elif state == "ready-for-review" and (
                saw_busy or self._has_fresh_completion_event(status, baseline)
            ):
                if not require_result:
                    return
                # State and provider transcript updates can become visible a few
                # polls apart. Wait for the structured result as well, so the
                # workflow's immediate read_result call cannot race transcript
                # discovery. This also distinguishes busy -> idle interrupts.
                result = self._result_data(session_id)
                result_status = self._result_status(result, session_id)
                if result_status == "interrupted" or result.get("interrupted") is True:
                    raise WorkerInterrupted(
                        f"session {session_id} turn was interrupted"
                    )
                if result_status == "completed" and self._is_fresh_result(
                    result, baseline
                ):
                    self._completed_turns[session_id] = result
                    self._turn_baselines.pop(session_id, None)
                    return
                if result_status in {"not-applicable", "unavailable"}:
                    reason = result.get("reason")
                    raise WorkerFailure(
                        f"session {session_id} result is {result_status}: {reason}"
                    )
            elif state == "ready-for-review" and require_result:
                # A fresh structured result also proves completion if a short busy
                # transition and its status event were both missed by polling.
                result = self._result_data(session_id)
                result_status = self._result_status(result, session_id)
                if result_status == "interrupted" or result.get("interrupted") is True:
                    raise WorkerInterrupted(
                        f"session {session_id} turn was interrupted"
                    )
                if result_status == "completed" and self._is_fresh_result(
                    result, baseline
                ):
                    self._completed_turns[session_id] = result
                    self._turn_baselines.pop(session_id, None)
                    return
            elif state == "idle" and saw_busy and require_result:
                result = self._result_data(session_id)
                result_status = self._result_status(result, session_id)
                if result_status == "interrupted" or result.get("interrupted") is True:
                    raise WorkerInterrupted(
                        f"session {session_id} turn was interrupted"
                    )
            if self._monotonic() >= deadline:
                raise WorkerFailure(
                    f"session {session_id} did not complete a turn within "
                    f"{timeout_seconds}s (saw_busy={saw_busy}, "
                    f"last cliState={last_state})"
                )
            self._sleep(self.poll_interval_seconds)

    def read_result(self, session_id: str) -> str:
        data = self._completed_turns.pop(session_id, None)
        if data is None:
            data = self._result_data(session_id)
        status = self._result_status(data, session_id)
        reason = data.get("reason")
        detail = f": {reason}" if isinstance(reason, str) and reason else ""
        if status == "interrupted" or data.get("interrupted") is True:
            raise WorkerInterrupted(
                f"session {session_id} turn was interrupted{detail}"
            )
        if status == "completed":
            text = data.get("text")
            if not isinstance(text, str):
                raise WorkerFailure(
                    f"session {session_id} completed result has no text"
                )
            return text
        if status == "not-ready":
            raise ResultNotReady(f"session {session_id} result is not ready{detail}")
        raise WorkerFailure(f"session {session_id} result is {status}{detail}")

    def interrupt(self, session_id: str) -> None:
        self._run_json(
            ["tab", "interrupt", "-w", self.workspace_id, session_id],
            operation="interrupt",
            read_only=False,
        )

    def close_session(self, session_id: str) -> None:
        self._run(
            ["tab", "close", "-w", self.workspace_id, session_id],
            operation="close",
            read_only=False,
        )
        self._turn_baselines.pop(session_id, None)
        self._completed_turns.pop(session_id, None)

    def capture_screen(self, session_id: str) -> str:
        """Capture diagnostic pane text; this is never used as an agent result."""
        data = self._run_json(
            ["tab", "capture", "-w", self.workspace_id, session_id],
            operation="capture",
            read_only=True,
        )
        content = data.get("content")
        if not isinstance(content, str):
            raise WorkerFailure("PurpleMux capture did not return text content")
        return content

    def _status(self, session_id: str) -> dict[str, Any]:
        return self._run_json(
            ["tab", "status", "-w", self.workspace_id, session_id],
            operation="status",
            read_only=True,
        )

    def _result_data(self, session_id: str) -> dict[str, Any]:
        return self._run_json(
            ["tab", "result", "-w", self.workspace_id, session_id],
            operation="result",
            read_only=True,
        )

    def _read_turn_baseline(self, session_id: str) -> _TurnBaseline:
        status_data = self._status(session_id)
        state = self._state(status_data, session_id)
        self._raise_abnormal_state(session_id, state, status_data)
        event_seq = status_data.get("eventSeq")
        if not isinstance(event_seq, int):
            event_seq = None
        ready_for_review_at = status_data.get("readyForReviewAt")
        if not isinstance(ready_for_review_at, int | float):
            ready_for_review_at = None
        data = self._result_data(session_id)
        status = self._result_status(data, session_id)
        if status == "completed":
            timestamp = data.get("completionTimestamp")
            if not isinstance(timestamp, int | float):
                raise WorkerFailure(
                    f"session {session_id} completed result has no completionTimestamp"
                )
            return _TurnBaseline(timestamp, event_seq, ready_for_review_at)
        if status in {"not-ready", "interrupted"}:
            return _TurnBaseline(None, event_seq, ready_for_review_at)
        reason = data.get("reason")
        raise WorkerFailure(
            f"session {session_id} cannot start a correlated turn: {status}: {reason}"
        )

    def _is_fresh_result(
        self, data: Mapping[str, Any], baseline: _TurnBaseline
    ) -> bool:
        timestamp = data.get("completionTimestamp")
        if not isinstance(timestamp, int | float):
            raise WorkerFailure("PurpleMux completed result has no completionTimestamp")
        if baseline.completion_timestamp is None:
            return True
        return timestamp > baseline.completion_timestamp

    def _has_fresh_completion_event(
        self, data: Mapping[str, Any], baseline: _TurnBaseline
    ) -> bool:
        ready_for_review_at = data.get("readyForReviewAt")
        if isinstance(ready_for_review_at, int | float) and (
            baseline.ready_for_review_at is None
            or ready_for_review_at > baseline.ready_for_review_at
        ):
            return True
        last_event = data.get("lastEvent")
        if not isinstance(last_event, Mapping):
            return False
        if str(last_event.get("name", "")).lower() != "stop":
            return False
        event_seq = last_event.get("seq")
        return (
            isinstance(event_seq, int)
            and baseline.event_seq is not None
            and event_seq > baseline.event_seq
        )

    def _is_fresh_interrupt(
        self, data: Mapping[str, Any], baseline: _TurnBaseline
    ) -> bool:
        last_event = data.get("lastEvent")
        if not isinstance(last_event, Mapping):
            return False
        if str(last_event.get("name", "")).lower() != "interrupt":
            return False
        event_seq = last_event.get("seq")
        return (
            isinstance(event_seq, int)
            and baseline.event_seq is not None
            and event_seq > baseline.event_seq
        )

    def _state(self, data: Mapping[str, Any], session_id: str) -> str:
        state = data.get("cliState")
        if not isinstance(state, str) or not state:
            raise WorkerFailure(f"PurpleMux status for {session_id} has no cliState")
        return state.lower()

    def _result_status(self, data: Mapping[str, Any], session_id: str) -> str:
        status = data.get("status")
        if not isinstance(status, str) or status not in _RESULT_STATUSES:
            raise WorkerFailure(
                f"PurpleMux result for {session_id} has invalid status {status!r}"
            )
        return status

    def _raise_abnormal_state(
        self, session_id: str, state: str, data: Mapping[str, Any]
    ) -> None:
        if state == "needs-input":
            raise WorkerNeedsInput(f"session {session_id} needs input")
        if state in _FAILED_STATES or data.get("alive") is False:
            raise WorkerFailure(f"session {session_id} entered {state}")

    def _run_json(
        self, args: Sequence[str], *, operation: str, read_only: bool
    ) -> dict[str, Any]:
        completed = self._run(args, operation=operation, read_only=read_only)
        try:
            data = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise WorkerFailure(
                f"PurpleMux {operation} returned malformed JSON"
            ) from exc
        if not isinstance(data, dict):
            raise WorkerFailure(f"PurpleMux {operation} returned non-object JSON")
        return cast(dict[str, Any], data)

    def _run(
        self, args: Sequence[str], *, operation: str, read_only: bool
    ) -> subprocess.CompletedProcess[str]:
        command = [self.executable, *args]
        attempts = self.read_timeout_retries + 1 if read_only else 1
        for attempt in range(attempts):
            try:
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt + 1 < attempts:
                    continue
                if read_only:
                    raise WorkerFailure(
                        f"PurpleMux {operation} timed out after "
                        f"{self.command_timeout_seconds}s"
                    ) from exc
                # A mutation may have reached PurpleMux before the local process
                # timed out. Blind retry could duplicate create/send. Callers must
                # reconcile via status/result (and tab list for create/close).
                raise MutationOutcomeUnknown(
                    f"PurpleMux {operation} timed out after "
                    f"{self.command_timeout_seconds}s; remote outcome is unknown"
                ) from exc
            except OSError as exc:
                raise WorkerFailure(
                    f"could not execute PurpleMux {operation}: {exc}"
                ) from exc
            if completed.returncode != 0:
                stderr = completed.stderr.strip() or "no stderr"
                raise WorkerFailure(
                    f"PurpleMux {operation} failed with exit code "
                    f"{completed.returncode}: {stderr}"
                )
            return completed
        raise AssertionError("unreachable")
