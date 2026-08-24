from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from coding_orchestrator.client import (
    CreateSessionRequest,
    TerminalSessionClient,
    TerminalSessionError,
)
from coding_orchestrator.prompts import (
    fix_prompt,
    implementation_prompt,
    review_prompt,
)

ReviewResult = Literal["APPROVED", "CHANGES_REQUESTED"]
FinalStatus = Literal["APPROVED", "FAILED"]

_DECISION_RE = re.compile(r"^\s*(?:DECISION:\s*)?(APPROVED|CHANGES_REQUESTED)\s*$")


class CodingWorkflowState(TypedDict, total=False):
    cwd: str
    task: str
    implementer_session_id: str
    reviewer_session_id: str
    review_count: int
    review_result: ReviewResult
    review_output: str
    implementer_output: str
    final_status: FinalStatus
    error: str


@dataclass(frozen=True)
class CodingWorkflowConfig:
    implementer_worker: str = "codex"
    implementer_command: str = "codex"
    reviewer_worker: str = "claude-code"
    reviewer_command: str = "claude"
    max_reviews: int = 4
    ready_timeout_seconds: float = 60.0
    turn_timeout_seconds: float = 900.0


class CodingWorkflow:
    def __init__(
        self,
        terminal_client: TerminalSessionClient,
        config: CodingWorkflowConfig | None = None,
    ) -> None:
        self.terminal_client = terminal_client
        self.config = config or CodingWorkflowConfig()
        self.graph = self._build_graph()

    def invoke(self, *, cwd: str, task: str) -> CodingWorkflowState:
        initial_state: CodingWorkflowState = {
            "cwd": cwd,
            "task": task,
            "review_count": 0,
        }
        return self.graph.invoke(initial_state)

    def _build_graph(self) -> Any:
        builder = StateGraph(cast(Any, CodingWorkflowState))
        builder.add_node("implement", self._implement)
        builder.add_node("review", self._review)
        builder.add_node("fix", self._fix)
        builder.add_node("failed", self._failed)

        builder.add_edge(START, "implement")
        builder.add_conditional_edges(
            "implement",
            self._route_worker_step,
            {"review": "review", "failed": "failed"},
        )
        builder.add_conditional_edges(
            "review",
            self._route_review,
            {"approved": END, "fix": "fix", "failed": "failed"},
        )
        builder.add_conditional_edges(
            "fix",
            self._route_worker_step,
            {"review": "review", "failed": "failed"},
        )
        builder.add_edge("failed", END)
        return builder.compile()

    def _implement(self, state: CodingWorkflowState) -> CodingWorkflowState:
        try:
            session_id = state.get("implementer_session_id")
            if not session_id:
                session_id = self._create_and_wait(
                    worker=self.config.implementer_worker,
                    command=self.config.implementer_command,
                    cwd=state["cwd"],
                    role="implementer",
                )
            output = self._send_turn(session_id, implementation_prompt(state["task"]))
            return {
                "implementer_session_id": session_id,
                "implementer_output": output,
            }
        except TerminalSessionError as exc:
            return self._terminal_failure(exc)

    def _review(self, state: CodingWorkflowState) -> CodingWorkflowState:
        try:
            session_id = state.get("reviewer_session_id")
            if not session_id:
                session_id = self._create_and_wait(
                    worker=self.config.reviewer_worker,
                    command=self.config.reviewer_command,
                    cwd=state["cwd"],
                    role="reviewer",
                )
            output = self._send_turn(session_id, review_prompt(state["task"]))
            result = parse_review_result(output)
            next_count = state.get("review_count", 0) + 1
            update: CodingWorkflowState = {
                "reviewer_session_id": session_id,
                "review_count": next_count,
                "review_result": result,
                "review_output": output,
            }
            if result == "APPROVED":
                update["final_status"] = "APPROVED"
            return update
        except TerminalSessionError as exc:
            return self._terminal_failure(exc)

    def _fix(self, state: CodingWorkflowState) -> CodingWorkflowState:
        try:
            session_id = state["implementer_session_id"]
            output = self._send_turn(
                session_id, fix_prompt(state["task"], state.get("review_output", ""))
            )
            return {"implementer_output": output}
        except TerminalSessionError as exc:
            return self._terminal_failure(exc)

    def _failed(self, state: CodingWorkflowState) -> CodingWorkflowState:
        if state.get("final_status") == "FAILED":
            return {}
        reason = "maximum review attempts reached"
        return {"final_status": "FAILED", "error": state.get("error", reason)}

    def _create_and_wait(
        self, *, worker: str, command: str, cwd: str, role: str
    ) -> str:
        session_id = self.terminal_client.create_session(
            CreateSessionRequest(
                worker=worker,
                cwd=cwd,
                command=command,
                metadata={"role": role},
            )
        )
        self.terminal_client.wait_until_ready(
            session_id, self.config.ready_timeout_seconds
        )
        return session_id

    def _send_turn(self, session_id: str, prompt: str) -> str:
        self.terminal_client.send_input(session_id, prompt)
        self.terminal_client.wait_for_turn_completion(
            session_id, self.config.turn_timeout_seconds
        )
        return self.terminal_client.read_screen(session_id)

    def _route_worker_step(
        self, state: CodingWorkflowState
    ) -> Literal["review", "failed"]:
        if state.get("final_status") == "FAILED":
            return "failed"
        return "review"

    def _route_review(
        self, state: CodingWorkflowState
    ) -> Literal["approved", "fix", "failed"]:
        if state.get("final_status") == "FAILED":
            return "failed"
        if state.get("review_result") == "APPROVED":
            return "approved"
        if state.get("review_count", 0) >= self.config.max_reviews:
            return "failed"
        return "fix"

    def _terminal_failure(self, exc: TerminalSessionError) -> CodingWorkflowState:
        return {"final_status": "FAILED", "error": str(exc)}


def parse_review_result(output: str) -> ReviewResult:
    for line in output.splitlines():
        match = _DECISION_RE.match(line.upper())
        if match:
            return cast(ReviewResult, match.group(1))
    return "CHANGES_REQUESTED"
