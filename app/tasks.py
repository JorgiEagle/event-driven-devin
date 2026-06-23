"""Task management: kickoff, status updates, and completion handling."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import Settings
from app.models import Task, TaskStatus
from app.store import TaskStore

logger = logging.getLogger(__name__)


async def kickoff_task(task: Task, store: TaskStore, settings: Settings) -> Task:
    """Start a Devin session for the given task.

    Transitions: ready -> queued -> resolving (on successful Devin API call).
    """
    task.transition_to(TaskStatus.QUEUED, reason="Task queued for Devin session")
    store.update(task)
    logger.info(
        "Task queued",
        extra={
            "task_id": task.id,
            "issue_number": task.issue_number,
            "repo": task.repo,
            "event_type": task.trigger,
            "state": task.status.value,
        },
    )

    # Attempt to start Devin session
    session_url = await _start_devin_session(task, settings)

    if session_url:
        task.devin_session_url = session_url
        task.transition_to(TaskStatus.RESOLVING, reason="Devin session started")
        logger.info(
            "Devin session started",
            extra={
                "task_id": task.id,
                "issue_number": task.issue_number,
                "repo": task.repo,
                "event_type": task.trigger,
                "state": task.status.value,
                "devin_session_url": session_url,
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


async def _start_devin_session(task: Task, settings: Settings) -> Optional[str]:
    """Call the Devin API to start a session for the issue.

    Returns the session URL on success, None on failure.
    """
    if not settings.devin_api_token:
        logger.warning(
            "No Devin API token configured, skipping session creation",
            extra={"task_id": task.id, "issue_number": task.issue_number},
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.devin.ai/v1/sessions",
                headers={
                    "Authorization": f"Bearer {settings.devin_api_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "prompt": (
                        f"Fix issue #{task.issue_number} in {task.repo}: "
                        f"{task.issue_title}\n\n"
                        f"Issue URL: {task.issue_url}\n\n"
                        f"Instructions:\n"
                        f"1. Resolve the issue and create a pull request.\n"
                        f"2. Automatically test your changes — do NOT wait for "
                        f"user approval to test. Run all relevant tests and "
                        f"verify the fix works end-to-end.\n"
                        f"3. Include test results in your final message: "
                        f"state whether tests PASSED or FAILED and summarize "
                        f"what was tested."
                    ),
                },
            )
            if response.status_code in (200, 201):
                data = response.json()
                return data.get("url", data.get("session_url", ""))
            logger.warning(
                "Devin API returned non-success status",
                extra={
                    "task_id": task.id,
                    "status_code": response.status_code,
                    "response": response.text[:500],
                },
            )
    except Exception as exc:
        logger.error(
            "Devin API call failed",
            extra={"task_id": task.id, "error": str(exc)},
        )
    return None
