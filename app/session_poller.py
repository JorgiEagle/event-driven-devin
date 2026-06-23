"""Background poller that monitors active Devin sessions for status changes."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

import httpx

from app.config import Settings
from app.models import Task, TaskStatus
from app.store import TaskStore

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
ACTIVE_STATUSES = {TaskStatus.RESOLVING, TaskStatus.QUEUED}


def _extract_session_id(session_url: str) -> Optional[str]:
    """Extract the session ID from a Devin session URL."""
    match = re.search(r"/sessions/([a-f0-9]+)$", session_url)
    if match:
        return match.group(1)
    return None


def _extract_test_result(messages: list[dict[str, Any]]) -> tuple[Optional[str], Optional[str]]:
    """Scan Devin session messages for test result indicators.

    Returns (result, summary) where result is "passed", "failed", or None.
    """
    # Check messages in reverse order (most recent first)
    for msg in reversed(messages):
        if msg.get("type") != "devin_message":
            continue
        text = msg.get("message", "")
        text_lower = text.lower()

        # Look for explicit test result indicators
        if any(phrase in text_lower for phrase in [
            "tests passed", "test passed", "all tests pass",
            "tests are passing", "ci passed", "ci is passing",
            "passing ci", "build passed",
        ]):
            summary = _truncate(text, 200)
            return "passed", summary

        if any(phrase in text_lower for phrase in [
            "tests failed", "test failed", "tests are failing",
            "ci failed", "ci is failing", "build failed",
            "test failure",
        ]):
            summary = _truncate(text, 200)
            return "failed", summary

    return None, None


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


async def _check_github_pr_status(
    task: Task,
    settings: Settings,
    client: httpx.AsyncClient,
) -> Optional[str]:
    """Check the CI status of a PR via GitHub API. Returns 'passed', 'failed', or None."""
    if not task.pr_number or not settings.github_token:
        return None

    repo = task.repo or settings.target_repo
    url = f"https://api.github.com/repos/{repo}/pulls/{task.pr_number}"

    try:
        response = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        if response.status_code != 200:
            return None

        pr_data = response.json()
        # Check if PR is merged
        if pr_data.get("merged"):
            return "merged"

        # Check mergeable_state for CI status
        mergeable_state = pr_data.get("mergeable_state", "")
        if mergeable_state == "clean":
            return "passed"
        elif mergeable_state == "unstable":
            return "failed"

    except Exception:
        pass
    return None


async def _check_session(
    task: Task,
    store: TaskStore,
    settings: Settings,
    client: httpx.AsyncClient,
) -> None:
    """Check a single Devin session and update task status accordingly."""
    if not task.devin_session_url:
        return

    session_id = _extract_session_id(task.devin_session_url)
    if not session_id:
        logger.warning(
            "Could not extract session ID from URL",
            extra={"task_id": task.id, "url": task.devin_session_url},
        )
        return

    try:
        response = await client.get(
            f"https://api.devin.ai/v1/sessions/{session_id}",
            headers={
                "Authorization": f"Bearer {settings.devin_api_token}",
                "Accept": "application/json",
            },
        )

        if response.status_code != 200:
            logger.warning(
                "Devin API returned non-200 for session check",
                extra={
                    "task_id": task.id,
                    "session_id": session_id,
                    "status_code": response.status_code,
                },
            )
            return

        data = response.json()
        session_status = data.get("status", "")
        pr_info = data.get("pull_request")
        pr_url = pr_info.get("url", "") if isinstance(pr_info, dict) else ""
        messages = data.get("messages", [])

        # Extract PR number from URL
        pr_number = None
        if pr_url:
            pr_match = re.search(r"/pull/(\d+)$", pr_url)
            if pr_match:
                pr_number = int(pr_match.group(1))

        # Update PR info on task if newly discovered
        if pr_url and not task.pr_url:
            task.pr_url = pr_url
            task.pr_number = pr_number
            store.update(task)
            logger.info(
                "PR detected for task",
                extra={
                    "task_id": task.id,
                    "issue_number": task.issue_number,
                    "pr_url": pr_url,
                },
            )

        # Extract test results from session messages
        test_result, test_summary = _extract_test_result(messages)

        # Handle session completion
        if session_status == "finished":
            if test_result:
                task.test_result = test_result
                task.test_summary = test_summary

            if pr_url:
                task.pr_url = pr_url
                task.pr_number = pr_number

                # Also check GitHub CI status if we haven't got test results from messages
                if not test_result:
                    ci_status = await _check_github_pr_status(task, settings, client)
                    if ci_status == "passed":
                        task.test_result = "passed"
                        task.test_summary = "CI checks passed on PR"
                    elif ci_status == "failed":
                        task.test_result = "failed"
                        task.test_summary = "CI checks failed on PR"
                    elif ci_status == "merged":
                        task.transition_to(TaskStatus.MERGED, reason="PR already merged")
                        store.update(task)
                        return

                if task.test_result == "failed":
                    task.transition_to(
                        TaskStatus.ATTENTION_REQUIRED,
                        reason=f"Tests failed: {task.test_summary or 'check PR for details'}",
                    )
                else:
                    task.transition_to(
                        TaskStatus.READY_TO_MERGE,
                        reason="Devin session completed, PR ready for review",
                    )
                store.update(task)
                logger.info(
                    "Task updated after session completion",
                    extra={
                        "task_id": task.id,
                        "issue_number": task.issue_number,
                        "state": task.status.value,
                        "test_result": task.test_result,
                        "pr_url": pr_url,
                    },
                )
            else:
                task.transition_to(
                    TaskStatus.ATTENTION_REQUIRED,
                    reason="Devin session finished without creating a PR",
                )
                store.update(task)
                logger.warning(
                    "Session finished without PR",
                    extra={
                        "task_id": task.id,
                        "issue_number": task.issue_number,
                    },
                )

        elif session_status == "stopped":
            task.transition_to(
                TaskStatus.FAILED,
                reason="Devin session was stopped",
            )
            store.update(task)
            logger.warning(
                "Session stopped",
                extra={
                    "task_id": task.id,
                    "issue_number": task.issue_number,
                },
            )

        elif session_status == "running" and pr_url and task.status == TaskStatus.RESOLVING:
            # Session still running but PR created — update PR info, stay in resolving
            # (session may still be testing)
            task.pr_url = pr_url
            task.pr_number = pr_number
            if test_result:
                task.test_result = test_result
                task.test_summary = test_summary
            store.update(task)
            logger.info(
                "PR detected, session still running (may be testing)",
                extra={
                    "task_id": task.id,
                    "issue_number": task.issue_number,
                    "pr_url": pr_url,
                    "test_result": test_result,
                },
            )

    except Exception as exc:
        logger.error(
            "Failed to check Devin session",
            extra={
                "task_id": task.id,
                "session_id": session_id,
                "error": str(exc),
            },
        )


async def _check_pr_merged(
    task: Task,
    store: TaskStore,
    settings: Settings,
    client: httpx.AsyncClient,
) -> None:
    """Check if a task's PR has been merged on GitHub and update status."""
    if not task.pr_number or not settings.github_token:
        return

    repo = task.repo or settings.target_repo
    url = f"https://api.github.com/repos/{repo}/pulls/{task.pr_number}"

    try:
        response = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        if response.status_code != 200:
            return

        pr_data = response.json()
        if pr_data.get("merged"):
            task.transition_to(TaskStatus.MERGED, reason="PR merged")
            store.update(task)
            logger.info(
                "PR merge detected",
                extra={
                    "task_id": task.id,
                    "issue_number": task.issue_number,
                    "pr_number": task.pr_number,
                    "trigger": task.trigger,
                    "state": task.status.value,
                },
            )
        elif pr_data.get("state") == "closed" and not pr_data.get("merged"):
            task.transition_to(
                TaskStatus.FAILED,
                reason="PR was closed without merging",
            )
            store.update(task)
            logger.warning(
                "PR closed without merge",
                extra={
                    "task_id": task.id,
                    "issue_number": task.issue_number,
                    "pr_number": task.pr_number,
                },
            )
    except Exception as exc:
        logger.error(
            "Failed to check PR merge status",
            extra={
                "task_id": task.id,
                "pr_number": task.pr_number,
                "error": str(exc),
            },
        )


MERGE_CHECK_STATUSES = {TaskStatus.READY_TO_MERGE, TaskStatus.ATTENTION_REQUIRED}


async def poll_active_sessions(store: TaskStore, settings: Settings) -> None:
    """Run a single poll cycle: check active sessions and pending merges."""
    all_tasks = store.list_all()

    active_tasks = [t for t in all_tasks if t.status in ACTIVE_STATUSES]
    merge_check_tasks = [
        t for t in all_tasks
        if t.status in MERGE_CHECK_STATUSES and t.pr_number
    ]

    if not active_tasks and not merge_check_tasks:
        return

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Check Devin sessions for active tasks
        if active_tasks and settings.devin_api_token:
            for task in active_tasks:
                await _check_session(task, store, settings, client)

        # Check GitHub for PR merge status on ready_to_merge tasks
        if merge_check_tasks and settings.github_token:
            for task in merge_check_tasks:
                await _check_pr_merged(task, store, settings, client)


async def start_poller(store: TaskStore, settings: Settings) -> None:
    """Background loop that periodically polls active Devin sessions."""
    logger.info(
        "Session poller started",
        extra={"poll_interval": POLL_INTERVAL_SECONDS},
    )
    while True:
        try:
            await poll_active_sessions(store, settings)
        except Exception as exc:
            logger.error(
                "Session poller cycle failed",
                extra={"error": str(exc)},
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
