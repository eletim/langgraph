from __future__ import annotations

import json

from coding_orchestrator.client import FakeTerminalSessionClient
from coding_orchestrator.workflow import CodingWorkflow


def main() -> None:
    client = FakeTerminalSessionClient(
        {
            "codex": ["implemented"],
            "claude-code": ["DECISION: APPROVED"],
        }
    )
    result = CodingWorkflow(client).invoke(cwd=".", task="smoke task")
    print(
        json.dumps(
            {
                "final_status": result.get("final_status"),
                "review_count": result.get("review_count"),
                "review_result": result.get("review_result"),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
