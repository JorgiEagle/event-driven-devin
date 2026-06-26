"""Task management: kickoff, status updates, and completion handling."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import Settings
from app.models import ImplementationPlan, Task, TaskStatus
from app.store import TaskStore

logger = logging.getLogger(__name__)

DEVIN_API_BASE = "https://api.devin.ai/v1"

# JSON schema sent on session creation so the planning stage returns a
# fixed-shape structured plan every time (mirrors ImplementationPlan).
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issue_summary": {"type": "string"},
        "root_causes": {"type": "array", "items": {"type": "string"}},
        "proposed_fix": {"type": "string"},
        "reasoning": {"type": "string"},
        "files_changing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "change": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
        "confidence": {"type": "integer"},
    },
}


async def kickoff_task(task: Task, store: TaskStore, settings: Settings) -> Task:
    """Start the planning stage (Stage 1) for the given task.

    Transitions: ready -> planning (on successful Devin API call), creating a
    session that analyzes the issue, returns a structured plan, and then waits
    for review instructions.
    """
    # Attempt to start the planning session
    session_data = await start_devin_session(task, settings)

    if session_data:
        task.devin_session_id = session_data["session_id"]
        task.devin_session_url = session_data["url"]
        task.transition_to(TaskStatus.PLANNING, reason="Devin planning session started")
        logger.info(
            "Devin planning session started",
            extra={
                "task_id": task.id,
                "issue_number": task.issue_number,
                "repo": task.repo,
                "event_type": task.trigger,
                "state": task.status.value,
                "devin_session_id": session_data["session_id"],
                "devin_session_url": session_data["url"],
            },
        )
    else:
        task.transition_to(
            TaskStatus.ATTENTION_REQUIRED,
            reason="Failed to start Devin session - check API token and connectivity",
        )
        logger.warning(
            "Failed to start Devin session",
            extra={
                "task_id": task.id,
                "issue_number": task.issue_number,
                "repo": task.repo,
                "event_type": task.trigger,
                "state": task.status.value,
                "outcome": "session_start_failed",
            },
        )

    store.update(task)
    return task


async def approve_task(
    task: Task, store: TaskStore, settings: Settings, plan_markdown: Optional[str] = None
) -> bool:
    """Gate-1 approval: send the (possibly edited) plan back to the same session
    and instruct Devin to implement it and open a PR.

    Transitions: awaiting_review -> implementing on success.
    Returns True if the approval message was delivered.
    """
    if task.status != TaskStatus.AWAITING_REVIEW or task.is_gate_two:
        logger.warning(
            "approve_task called on task not at the plan-review gate",
            extra={"task_id": task.id, "current_status": task.status.value},
        )
        return False
    if plan_markdown:
        task.plan_markdown = plan_markdown
    approved_plan = task.plan_markdown or (task.plan.to_markdown() if task.plan else "")

    message = (
        "The implementation plan has been reviewed and APPROVED. Proceed to "
        "implement the changes exactly as described in the approved plan below, "
        "then open a pull request.\n\n"
        f"--- APPROVED PLAN ---\n{approved_plan}\n--- END PLAN ---\n\n"
        "Instructions:\n"
        "1. Implement the fix as described in the approved plan.\n"
        "2. Create a pull request.\n"
        "3. Automatically test your changes — do NOT wait for approval to test. "
        "Run all relevant tests and verify the fix works end-to-end.\n"
        "4. In your final message, state whether tests PASSED or FAILED and "
        "summarize what was tested."
    )

    delivered = await continue_devin_session(task, settings, message)
    if not delivered:
        logger.warning(
            "Failed to deliver approval to Devin session",
            extra={"task_id": task.id, "devin_session_id": task.devin_session_id},
        )
        return False

    task.plan_approved = True
    task.transition_to(TaskStatus.IMPLEMENTING, reason="Plan approved — implementing")
    store.update(task)
    logger.info(
        "Plan approved, implementation started",
        extra={
            "task_id": task.id,
            "issue_number": task.issue_number,
            "repo": task.repo,
            "state": task.status.value,
            "devin_session_id": task.devin_session_id,
        },
    )
    return True


async def request_changes(
    task: Task, store: TaskStore, settings: Settings, feedback: str
) -> bool:
    """Send review feedback to the same session and loop back.

    Gate 1 (no PR): awaiting_review -> planning  (re-plan with feedback).
    Gate 2 (PR set): awaiting_review -> implementing  (re-implement with feedback).
    Returns True if the feedback message was delivered.
    """
    if task.status != TaskStatus.AWAITING_REVIEW:
        logger.warning(
            "request_changes called on task not awaiting review",
            extra={"task_id": task.id, "current_status": task.status.value},
        )
        return False
    gate_two = task.is_gate_two
    if gate_two:
        message = (
            "Changes are requested on the pull request before it can be merged. "
            "Please address the following feedback, update the PR, and re-run the "
            "relevant tests:\n\n"
            f"{feedback}\n\n"
            "In your final message, state whether tests PASSED or FAILED and "
            "summarize what changed."
        )
    else:
        message = (
            "Changes are requested on the plan. Please revise the plan based on "
            "the following feedback, present an UPDATED plan in the same "
            "structured format, and then stop and wait for further instructions "
            "(do NOT implement yet):\n\n"
            f"{feedback}"
        )

    delivered = await continue_devin_session(task, settings, message)
    if not delivered:
        logger.warning(
            "Failed to deliver change request to Devin session",
            extra={"task_id": task.id, "devin_session_id": task.devin_session_id},
        )
        return False

    task.replan_count += 1
    if gate_two:
        task.transition_to(
            TaskStatus.IMPLEMENTING, reason="Changes requested on PR — re-implementing"
        )
    else:
        # Clear the stale plan so the poller captures the revised one.
        # last_plan_hash is intentionally retained: the session keeps
        # returning the OLD structured_output until Devin produces a revised
        # plan, and the poller's staleness guard uses the hash to skip it.
        task.plan = None
        task.plan_markdown = None
        task.transition_to(
            TaskStatus.PLANNING, reason="Changes requested on plan — re-planning"
        )
    store.update(task)
    logger.info(
        "Changes requested",
        extra={
            "task_id": task.id,
            "issue_number": task.issue_number,
            "state": task.status.value,
            "gate": "two" if gate_two else "one",
            "replan_count": task.replan_count,
        },
    )
    return True


async def complete_task(
    task: Task, store: TaskStore, settings: Settings, pr_url: Optional[str] = None
) -> Task:
    """Mark a task as complete and create the PR reference.

    Only completed tasks reach ready_to_merge.
    """
    task.pr_url = pr_url
    task.transition_to(TaskStatus.READY_TO_MERGE, reason="Devin session completed, PR created")
    store.update(task)
    logger.info(
        "Task ready to merge",
        extra={
            "task_id": task.id,
            "issue_number": task.issue_number,
            "repo": task.repo,
            "event_type": "completion",
            "state": task.status.value,
            "pr_url": pr_url,
            "outcome": "success",
        },
    )
    return task


async def merge_task(task: Task, store: TaskStore) -> Task:
    """Mark a task as merged."""
    task.transition_to(TaskStatus.MERGED, reason="PR merged")
    store.update(task)
    logger.info(
        "Task merged",
        extra={
            "task_id": task.id,
            "issue_number": task.issue_number,
            "repo": task.repo,
            "event_type": "merge",
            "state": task.status.value,
            "outcome": "merged",
        },
    )
    return task


async def fail_task(task: Task, store: TaskStore, reason: str = "") -> Task:
    """Mark a task as failed."""
    task.transition_to(TaskStatus.FAILED, reason=reason or "Task execution failed")
    store.update(task)
    logger.error(
        "Task failed",
        extra={
            "task_id": task.id,
            "issue_number": task.issue_number,
            "repo": task.repo,
            "event_type": "failure",
            "state": task.status.value,
            "outcome": "failed",
            "reason": reason,
        },
    )
    return task


def _build_planning_prompt(task: Task) -> str:
    """Build the Stage-1 planning prompt (analyze only, no changes)."""
    return (
        "You are a senior expert software engineer, tasked with addressing issues in a codebase\n"
        "This is a PLANNING task. Do NOT make any code changes and do NOT open "
        "a pull request yet. All plans produced should be clear, and consistent with the "
        "exisiting codebase.\n"
        f"Analyze issue #{task.issue_number} in {task.repo}: {task.issue_title}\n\n"
        f"Issue URL: {task.issue_url}\n\n"
        "Investigate the issue and produce a structured "
        "implementation plan covering:\n"
        "  - issue_summary: a concise summary of the issue\n"
        "  - impact: the potential impact this issue\n"
        "  - root_causes: the underlying root cause(s)\n"
        "  - proposed_fix: the proposed implementation/fix\n"
        "  - reasoning: why this is the best solution and how it fixes the issue\n"
        "  - files_changing: each file you would change, with what changes and why\n"
        "  - confidence: how confident you are (0-100) that this fixes the issue\n\n"
        "Return the plan via the structured output, then STOP and WAIT for "
        "further instructions. Do not begin implementation until you are told "
        "the plan is approved."
    )


async def _call_devin_api(
    task: Task,
    settings: Settings,
    *,
    url: str,
    body: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Base Devin API plumbing shared by start/continue.

    POSTs ``body`` to ``url`` with auth headers. Returns the parsed JSON dict on
    success (an empty dict when the endpoint returns a null body, e.g. the
    message endpoint), or None on missing token / non-success / error.
    """
    if not settings.devin_api_token:
        logger.warning(
            "No Devin API token configured, skipping Devin API call",
            extra={"task_id": task.id, "issue_number": task.issue_number},
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.devin_api_token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if response.status_code in (200, 201, 204):
                try:
                    data = response.json()
                except ValueError:
                    data = None
                return data if isinstance(data, dict) else {}
            logger.warning(
                "Devin API returned non-success status",
                extra={
                    "task_id": task.id,
                    "url": url,
                    "status_code": response.status_code,
                    "response": response.text[:500],
                },
            )
    except Exception as exc:
        logger.error(
            "Devin API call failed",
            extra={"task_id": task.id, "url": url, "error": str(exc)},
        )
    return None


async def start_devin_session(
    task: Task, settings: Settings
) -> Optional[dict[str, str]]:
    """Start a new planning session (POST /v1/sessions).

    Returns {"session_id": ..., "url": ...} on success, None on failure.
    """
    body: dict[str, Any] = {
        "prompt": _build_planning_prompt(task),
        "title": f"Plan: #{task.issue_number} {task.issue_title}".strip()[:120],
        "structured_output_schema": PLAN_SCHEMA,
    }
    data = await _call_devin_api(
        task, settings, url=f"{DEVIN_API_BASE}/sessions", body=body
    )
    if data is None:
        return None
    session_id = data.get("session_id", "")
    url = data.get("url", "")
    if session_id:
        return {"session_id": session_id, "url": url}
    logger.warning(
        "Devin API response missing session_id",
        extra={"task_id": task.id, "response_keys": list(data.keys())},
    )
    return None


async def continue_devin_session(
    task: Task, settings: Settings, message: str
) -> bool:
    """Continue an existing session by messaging it
    (POST /v1/sessions/{id}/message).

    Returns True if the message was delivered, False otherwise.
    """
    if not task.devin_session_id:
        logger.warning(
            "Cannot continue session - no devin_session_id on task",
            extra={"task_id": task.id},
        )
        return False
    data = await _call_devin_api(
        task,
        settings,
        url=f"{DEVIN_API_BASE}/sessions/{task.devin_session_id}/message",
        body={"message": message},
    )
    return data is not None


def parse_plan(structured_output: dict[str, Any]) -> ImplementationPlan:
    """Build an ImplementationPlan from a session's structured_output dict."""
    return ImplementationPlan.model_validate(structured_output)
