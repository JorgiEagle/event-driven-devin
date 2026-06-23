"""GitHub webhook receiver and event processing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import Settings
from app.models import Task, TaskStatus
from app.store import TaskStore
from app.tasks import kickoff_task

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not secret:
        return True  # No secret configured, skip verification (dev mode)
    expected = "sha256=" + hmac.HMAC(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _has_trigger_label(issue_data: dict[str, Any], trigger_label: str) -> bool:
    """Check if the issue has the trigger label."""
    labels = issue_data.get("labels", [])
    return any(label.get("name") == trigger_label for label in labels)


@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default="", alias="X-GitHub-Event"),
    x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256"),
) -> dict[str, str]:
    """Receive and process GitHub webhook events.

    Only processes 'issues' events where the issue has the 'assign-devin' label.
    """
    settings: Settings = request.app.state.settings
    store: TaskStore = request.app.state.store

    body = await request.body()

    # Verify webhook signature
    if settings.github_webhook_secret:
        if not _verify_signature(body, x_hub_signature_256, settings.github_webhook_secret):
            logger.warning(
                "Webhook signature verification failed",
                extra={"event_type": x_github_event},
            )
            raise HTTPException(status_code=401, detail="Invalid signature")

    # Only process issue events
    if x_github_event != "issues":
        logger.info(
            "Ignoring non-issue event",
            extra={"event_type": x_github_event, "outcome": "ignored"},
        )
        return {"status": "ignored", "reason": f"Event type '{x_github_event}' not handled"}

    payload = await request.json()
    action = payload.get("action", "")
    issue = payload.get("issue", {})
    repo_data = payload.get("repository", {})
    repo_full_name = repo_data.get("full_name", settings.target_repo)

    logger.info(
        "Received issue event",
        extra={
            "event_type": "issues",
            "action": action,
            "issue_number": issue.get("number"),
            "repo": repo_full_name,
        },
    )

    # Only trigger on 'opened' or 'labeled' actions
    if action not in ("opened", "labeled"):
        logger.info(
            "Ignoring issue action",
            extra={
                "event_type": "issues",
                "action": action,
                "issue_number": issue.get("number"),
                "outcome": "ignored",
            },
        )
        return {"status": "ignored", "reason": f"Action '{action}' not handled"}

    # Check for trigger label
    if action == "labeled":
        # For 'labeled' events, verify the specific label just added is the trigger
        added_label = payload.get("label", {}).get("name", "")
        if added_label != settings.trigger_label:
            logger.info(
                "Ignoring labeled event for non-trigger label",
                extra={
                    "event_type": "issues",
                    "action": action,
                    "issue_number": issue.get("number"),
                    "added_label": added_label,
                    "trigger_label": settings.trigger_label,
                    "outcome": "ignored",
                },
            )
            return {"status": "ignored", "reason": f"Added label '{added_label}' is not the trigger"}
    elif not _has_trigger_label(issue, settings.trigger_label):
        logger.info(
            "Issue missing trigger label",
            extra={
                "event_type": "issues",
                "action": action,
                "issue_number": issue.get("number"),
                "trigger_label": settings.trigger_label,
                "outcome": "ignored",
            },
        )
        return {"status": "ignored", "reason": f"Label '{settings.trigger_label}' not present"}

    issue_number = issue.get("number", 0)

    # Check for duplicate task
    existing = store.get_by_issue(repo_full_name, issue_number)
    if existing and existing.status not in (TaskStatus.FAILED, TaskStatus.MERGED):
        logger.info(
            "Task already exists for issue",
            extra={
                "task_id": existing.id,
                "issue_number": issue_number,
                "state": existing.status.value,
                "outcome": "duplicate",
            },
        )
        return {"status": "duplicate", "task_id": existing.id}

    # Create and kickoff new task
    task = Task(
        issue_number=issue_number,
        issue_title=issue.get("title", ""),
        issue_url=issue.get("html_url", ""),
        repo=repo_full_name,
        trigger="webhook",
    )
    task.transition_to(TaskStatus.READY, reason="Issue received via webhook")
    store.add(task)

    logger.info(
        "Task created from webhook",
        extra={
            "task_id": task.id,
            "issue_number": issue_number,
            "repo": repo_full_name,
            "event_type": "webhook",
            "state": task.status.value,
        },
    )

    # Kickoff the task asynchronously so the webhook response is immediate
    asyncio.create_task(kickoff_task(task, store, settings))

    return {"status": "accepted", "task_id": task.id}
