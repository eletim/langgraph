from __future__ import annotations


def implementation_prompt(task: str) -> str:
    return f"""Implement this task in the current working tree.

Task:
{task}
"""


def review_prompt(task: str) -> str:
    return f"""Review the current working tree diff for the task below.

Task:
{task}

Return exactly one machine-readable decision line:
DECISION: APPROVED
or
DECISION: CHANGES_REQUESTED

If changes are requested, include only blocking, actionable findings after the decision.
"""


def fix_prompt(task: str, review_output: str) -> str:
    return f"""The reviewer requested changes for this task.

Task:
{task}

Reviewer output:
{review_output}

Address only the blocking, actionable findings, then stop.
"""
