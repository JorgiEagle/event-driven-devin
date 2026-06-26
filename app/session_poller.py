"""Background poller that monitors active Devin sessions for status changes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Optional

import httpx

from app.config import Settings
from app.github_client import GitHubClient
from app.models import Task, TaskStatus
from app.store import TaskStore
from app.tasks import parse_plan

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10
# Statuses the poller actively watches against the Devin API.
# PLANNING -> waiting for the structured plan; IMPLEMENTING -> waiting for a PR.
# RESOLVING is retained for backward compatibility with legacy tasks.
ACTIVE_STATUSES = {
    TaskStatus.PLANNING,
    TaskStatus.IMPLEMENTING,
    TaskStatus.RESOLVING,
}


def _extract_session_id(session_url: str) -> Optional[str]:
    """Extract the session ID from a Devin session URL."""
    match = re.search(r"/sessions/([a-zA-Z0-9_-]+)", session_url)
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

        # Check failure first — a partial failure should be treated as failure
        if any(phrase in text_lower for phrase in [
            "tests failed", "test failed", "tests are failing",
            "ci failed", "ci is failing", "build failed",
            "test failure",
        ]):
            summary = _truncate(text, 200)
            return "failed", summary

        if any(phrase in text_lower for phrase in [
            "tests passed", "test passed", "all tests pass",
            "tests are passing", "ci passed", "ci is passing",
            "passing ci", "build passed",
        ]):
            summary = _truncate(text, 200)
            return "passed", summary

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

        # Check if PR was closed without merging
        if pr_data.get("state") == "closed" and not pr_data.get("merged"):
            return "closed"

        # Check mergeable_state for CI status
        mergeable_state = pr_data.get("mergeable_state", "")
        if mergeable_state == "clean":
            return "passed"
        elif mergeable_state == "unstable":
            return "failed"

    except Exception:
        pass
    return None


async def _handle_planning_session(
    task: Task,
    store: TaskStore,
    structured_output: Optional[dict[str, Any]],
    session_status: str,
) -> None:
    """Stage 1: capture the structured plan and move to the Gate-1 review.

    The planning prompt instructs Devin to return the plan via structured
    output and then wait. We capture the plan as soon as it appears (the
    session may still be ``blocked``/``working``). If the session reaches a
    terminal state without producing a plan, flag it for attention.
    """
    if isinstance(structured_output, dict) and structured_output:
        # Staleness guard: after a "request changes" loop the session still
        # returns the PREVIOUS plan until Devin produces a revised one. Skip
        # any structured_output we have already captured so we don't snap the
        # task straight back to review with the stale plan.
        output_hash = hashlib.sha256(
            json.dumps(structured_output, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if output_hash == task.last_plan_hash:
            return
        plan = parse_plan(structured_output)
        task.plan = plan
        task.plan_markdown = plan.to_markdown()
        task.last_plan_hash = output_hash
        task.transition_to(
            TaskStatus.AWAITING_REVIEW,
            reason="Plan ready for review",
        )
        store.update(task)
        logger.info(
            "Plan captured, awaiting review",
            extra={
                "task_id": task.id,
                "issue_number": task.issue_number,
                "state": task.status.value,
                "confidence": plan.confidence,
            },
        )
        return

    if session_status in ("finished", "stopped", "expired"):
        task.transition_to(
            TaskStatus.ATTENTION_REQUIRED,
            reason="Planning session ended without producing a plan",
        )
        store.update(task)
        logger.warning(
            "Planning session ended without a plan",
            extra={
                "task_id": task.id,
                "issue_number": task.issue_number,
                "session_status": session_status,
            },
        )


async def _build_review_summary(task: Task, settings: Settings) -> str:
    """Assemble a Gate-2 review summary: Devin Review comments + test results."""
    parts: list[str] = []

    if task.test_result:
        parts.append(
            f"**Tests:** {task.test_result.upper()} — "
            f"{task.test_summary or 'see PR for details'}"
        )

    if task.pr_number and settings.github_token:
        client = GitHubClient(settings)
        if task.repo:
            client._repo = task.repo
        try:
            comments = await client.get_pr_review_comments(task.pr_number)
        except Exception:
            comments = []
        if comments:
            parts.append(f"**Devin Review ({len(comments)} comment(s)):**")
            for c in comments:
                parts.append(f"- _{c['author']}_: {_truncate(c['body'], 500)}")
        else:
            parts.append("**Devin Review:** no review comments found yet.")

    return "\n\n".join(parts) if parts else "No review details available yet."


async def _check_session(
    task: Task,
    store: TaskStore,
    settings: Settings,
    client: httpx.AsyncClient,
) -> None:
    """Check a single Devin session and update task status accordingly."""
    session_id = task.devin_session_id
    if not session_id:
        # Fallback for tasks created before devin_session_id was stored
        if task.devin_session_url:
            session_id = _extract_session_id(task.devin_session_url)
        if not session_id:
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
        # Devin API returns status_enum for lifecycle state (e.g. "finished")
        # while status may be "suspended" for completed sessions
        session_status = data.get("status_enum", data.get("status", ""))
        pr_info = data.get("pull_request")
        pr_url = pr_info.get("url", "") if isinstance(pr_info, dict) else ""
        structured_output = data.get("structured_output")
        messages = data.get("messages", [])

        # Extract PR number from URL
        pr_number = None
        if pr_url:
            pr_match = re.search(r"/pull/(\d+)$", pr_url)
            if pr_match:
                pr_number = int(pr_match.group(1))

        # Stage 1: planning session — capture the structured plan and move to
        # the Gate-1 review. Handled separately from the PR-based lifecycle.
        if task.status == TaskStatus.PLANNING:
            await _handle_planning_session(
                task, store, structured_output, session_status
            )
            return

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

        # Handle session completion (status_enum "finished" or "stopped")
        is_terminal = session_status in ("finished", "stopped")
        if is_terminal:
            # Guard: skip if task already moved past active state (e.g. merged
            # by dashboard or poller's _check_pr_merged during an await)
            if task.status not in ACTIVE_STATUSES:
                logger.info(
                    "Task status changed during poll, skipping transition",
                    extra={"task_id": task.id, "current_status": task.status.value},
                )
                return

            if test_result:
                task.test_result = test_result
                task.test_summary = test_summary

            if pr_url and session_status == "stopped":
                # Stopped session with PR — work may be incomplete
                task.pr_url = pr_url
                task.pr_number = pr_number
                task.transition_to(
                    TaskStatus.ATTENTION_REQUIRED,
                    reason="Devin session was stopped — PR may contain incomplete work",
                )
                store.update(task)
                logger.warning(
                    "Session stopped with PR (incomplete work)",
                    extra={
                        "task_id": task.id,
                        "issue_number": task.issue_number,
                        "pr_url": pr_url,
                    },
                )
            elif pr_url:
                task.pr_url = pr_url
                task.pr_number = pr_number

                # Check GitHub CI status if no test results from messages
                if not test_result:
                    ci_status = await _check_github_pr_status(task, settings, client)
                    # Re-check status after await — another handler may have
                    # transitioned the task while we were waiting
                    if task.status not in ACTIVE_STATUSES:
                        logger.info(
                            "Task status changed during CI check, skipping",
                            extra={"task_id": task.id, "current_status": task.status.value},
                        )
                        return
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
                    elif ci_status == "closed":
                        task.transition_to(
                            TaskStatus.FAILED,
                            reason="PR was closed without merging",
                        )
                        store.update(task)
                        return

                # Gate 2: PR is ready — surface the Devin Review comments + test
                # results and let the user merge or request changes.
                task.review_summary = await _build_review_summary(task, settings)
                # Re-check status after await — another handler may have
                # transitioned the task while we were fetching review comments
                if task.status not in ACTIVE_STATUSES:
                    logger.info(
                        "Task status changed during review summary fetch, skipping",
                        extra={"task_id": task.id, "current_status": task.status.value},
                    )
                    return
                task.transition_to(
                    TaskStatus.AWAITING_REVIEW,
                    reason="Implementation complete — PR ready for review",
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
            elif session_status == "stopped":
                task.transition_to(
                    TaskStatus.FAILED,
                    reason="Devin session was stopped",
                )
                store.update(task)
                logger.warning(
                    "Session stopped without PR",
                    extra={
                        "task_id": task.id,
                        "issue_number": task.issue_number,
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

        elif session_status == "running" and pr_url and task.status in (
            TaskStatus.IMPLEMENTING,
            TaskStatus.RESOLVING,
        ):
            # Session still running but PR created — update PR info, stay active
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


MERGE_CHECK_STATUSES = {
    TaskStatus.AWAITING_REVIEW,
    TaskStatus.ATTENTION_REQUIRED,
    TaskStatus.READY_TO_MERGE,
}


async def poll_active_sessions(store: TaskStore, settings: Settings) -> None:
    """Run a single poll cycle: check active sessions and pending merges."""
    all_tasks = store.list_all()

    active_tasks = [t for t in all_tasks if t.status in ACTIVE_STATUSES]
    merge_check_tasks = [
        t for t in all_tasks
        if t.status in MERGE_CHECK_STATUSES and t.pr_number
    ]
    # Also check GitHub PRs for active (RESOLVING/QUEUED) tasks that have a
    # known PR — the Devin session may never reach terminal state but the PR
    # can still be merged or closed independently on GitHub.
    active_with_pr = [t for t in active_tasks if t.pr_number]

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

        # Parallel GitHub check for active tasks whose PR may have been
        # merged/closed while the Devin session is still running
        if active_with_pr and settings.github_token:
            for task in active_with_pr:
                # Skip if _check_session already transitioned this task
                if task.status not in ACTIVE_STATUSES:
                    continue
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
