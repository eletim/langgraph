from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class TerminalSessionError(RuntimeError):
    """Base error for terminal session operations."""


class SessionReadyTimeout(TerminalSessionError):
    """Raised when a terminal session does not become ready in time."""


class WorkerFailure(TerminalSessionError):
    """Raised when a terminal-backed worker fails."""


@dataclass(frozen=True)
class CreateSessionRequest:
    worker: str
    cwd: str
    command: str
    metadata: Mapping[str, str] = field(default_factory=dict)


class TerminalSessionClient(Protocol):
    def create_session(self, request: CreateSessionRequest) -> str:
        """Create a terminal session and return its session id."""

    def wait_until_ready(self, session_id: str, timeout_seconds: float) -> None:
        """Wait until the session can accept input."""

    def send_input(self, session_id: str, text: str) -> None:
        """Send one prompt to the session."""

    def wait_for_turn_completion(self, session_id: str, timeout_seconds: float) -> None:
        """Wait until the current worker turn has completed."""

    def read_screen(self, session_id: str) -> str:
        """Read the current terminal screen or captured turn output."""


@dataclass(frozen=True)
class FakeTurn:
    output: str = ""
    fail: bool = False


class FakeTerminalSessionClient:
    """Deterministic in-memory client for workflow tests."""

    def __init__(
        self,
        scripts: Mapping[str, Sequence[FakeTurn | str]],
        *,
        ready_timeout_workers: set[str] | None = None,
        create_failure_workers: set[str] | None = None,
    ) -> None:
        self._scripts = {
            worker: deque(
                turn if isinstance(turn, FakeTurn) else FakeTurn(turn) for turn in turns
            )
            for worker, turns in scripts.items()
        }
        self._ready_timeout_workers = ready_timeout_workers or set()
        self._create_failure_workers = create_failure_workers or set()
        self._session_workers: dict[str, str] = {}
        self._screens: dict[str, str] = {}
        self._pending_input: dict[str, str] = {}
        self._counts: defaultdict[str, int] = defaultdict(int)
        self.inputs: list[tuple[str, str, str]] = []
        self.created_sessions: list[tuple[str, CreateSessionRequest]] = []

    def create_session(self, request: CreateSessionRequest) -> str:
        if request.worker in self._create_failure_workers:
            raise WorkerFailure(f"{request.worker} session creation failed")
        self._counts[request.worker] += 1
        session_id = f"{request.worker}-{self._counts[request.worker]}"
        self._session_workers[session_id] = request.worker
        self._screens[session_id] = ""
        self.created_sessions.append((session_id, request))
        return session_id

    def wait_until_ready(self, session_id: str, timeout_seconds: float) -> None:
        worker = self._worker_for(session_id)
        if worker in self._ready_timeout_workers:
            raise SessionReadyTimeout(
                f"{worker} session {session_id} was not ready within {timeout_seconds}s"
            )

    def send_input(self, session_id: str, text: str) -> None:
        worker = self._worker_for(session_id)
        self._pending_input[session_id] = text
        self.inputs.append((session_id, worker, text))

    def wait_for_turn_completion(self, session_id: str, timeout_seconds: float) -> None:
        worker = self._worker_for(session_id)
        if session_id not in self._pending_input:
            raise WorkerFailure(f"{worker} session {session_id} has no pending input")
        self._pending_input.pop(session_id)
        if not self._scripts.get(worker):
            raise WorkerFailure(f"{worker} script has no remaining turns")
        turn = self._scripts[worker].popleft()
        if turn.fail:
            raise WorkerFailure(f"{worker} turn failed")
        self._screens[session_id] = turn.output

    def read_screen(self, session_id: str) -> str:
        self._worker_for(session_id)
        return self._screens[session_id]

    def _worker_for(self, session_id: str) -> str:
        try:
            return self._session_workers[session_id]
        except KeyError as exc:
            raise WorkerFailure(f"unknown session {session_id}") from exc


class MulmoTerminalHTTPClient:
    """HTTP implementation for the planned MulmoTerminal Session API."""

    def __init__(
        self,
        base_url: str,
        *,
        poll_interval_seconds: float = 1.0,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.poll_interval_seconds = poll_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds

    def create_session(self, request: CreateSessionRequest) -> str:
        data = self._request_json(
            "POST",
            "/api/sessions",
            {
                "worker": request.worker,
                "cwd": request.cwd,
                "command": request.command,
                "metadata": dict(request.metadata),
            },
        )
        session_id = data.get("id") or data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise WorkerFailure("MulmoTerminal did not return a session id")
        return session_id

    def wait_until_ready(self, session_id: str, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data = self._get_session(session_id)
            status = str(data.get("status") or data.get("state") or "").lower()
            if status in {"ready", "idle"}:
                return
            if status in {"failed", "error", "stopped", "exited"}:
                raise WorkerFailure(f"session {session_id} entered {status}")
            time.sleep(self.poll_interval_seconds)
        raise SessionReadyTimeout(
            f"session {session_id} was not ready within {timeout_seconds}s"
        )

    def send_input(self, session_id: str, text: str) -> None:
        self._request_json(
            "POST",
            f"/api/sessions/{quote(session_id, safe='')}/input",
            {"input": text},
        )

    def wait_for_turn_completion(self, session_id: str, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        saw_busy = False
        while time.monotonic() < deadline:
            data = self._get_session(session_id)
            status = str(data.get("status") or data.get("state") or "").lower()
            turn_status = str(data.get("turn_status") or "").lower()
            if status in {"failed", "error", "stopped", "exited"}:
                raise WorkerFailure(f"session {session_id} entered {status}")
            if turn_status in {"running", "busy"} or status in {"running", "busy"}:
                saw_busy = True
            if turn_status in {"complete", "completed", "done"}:
                return
            if saw_busy and status in {"ready", "idle"}:
                return
            time.sleep(self.poll_interval_seconds)
        raise WorkerFailure(
            f"session {session_id} did not complete a turn within {timeout_seconds}s"
        )

    def read_screen(self, session_id: str) -> str:
        data = self._request_json(
            "GET", f"/api/sessions/{quote(session_id, safe='')}/screen"
        )
        for key in ("text", "screen", "output", "content"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(data, ensure_ascii=False)

    def _get_session(self, session_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/sessions/{quote(session_id, safe='')}")

    def _request_json(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        body = None
        headers = {"accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.request_timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WorkerFailure(f"MulmoTerminal HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise WorkerFailure(f"MulmoTerminal request failed: {exc}") from exc
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise WorkerFailure("MulmoTerminal returned a non-object JSON response")
        return data
