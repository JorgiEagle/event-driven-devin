"""Dashboard routes: web UI for issue listing, plan review, and PR merge."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.github_client import GitHubClient
from app.models import TaskStatus
from app.store import TaskStore
from app.tasks import approve_task, merge_task, request_changes
from app.tunnel import get_tunnel_url, get_webhook_url

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the dashboard with issues fetched from GitHub and task statuses."""
    settings: Settings = request.app.state.settings
    store: TaskStore = request.app.state.store
    github = GitHubClient(settings)

    # Fetch open issues from the target repo
    issues = await github.list_issues(state="open")

    # Annotate issues with their task status (if a task exists)
    issues_with_status = []
    for issue in issues:
        issue_number = issue.get("number", 0)
        task = store.get_by_issue(settings.target_repo, issue_number)
        issues_with_status.append({
            "issue": issue,
            "task": task,
        })

    # Also get tasks (for the task history section)
    tasks = store.list_all()

    # Compute metric card counts
    in_progress_statuses = {
        TaskStatus.READY,
        TaskStatus.PLANNING,
        TaskStatus.AWAITING_REVIEW,
        TaskStatus.IMPLEMENTING,
        TaskStatus.ATTENTION_REQUIRED,
        # Legacy active statuses
        TaskStatus.QUEUED,
        TaskStatus.RESOLVING,
        TaskStatus.READY_TO_MERGE,
    }
    resolved = sum(1 for t in tasks if t.status == TaskStatus.MERGED)
    in_progress = sum(1 for t in tasks if t.status in in_progress_statuses)
    in_progress_issue_numbers = {
        t.issue_number for t in tasks if t.status in in_progress_statuses
    }
    outstanding = sum(
        1
        for entry in issues_with_status
        if entry["issue"].get("number") not in in_progress_issue_numbers
    )

    # Discover tunnel URL for webhook configuration
    tunnel_url = await get_tunnel_url()
    webhook_url = get_webhook_url(tunnel_url)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "issues_with_status": issues_with_status,
            "tasks": tasks,
            "target_repo": settings.target_repo,
            "webhook_url": webhook_url,
            "resolved": resolved,
            "in_progress": in_progress,
            "outstanding": outstanding,
        },
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str) -> HTMLResponse:
    """Render task detail with full status history."""
    store: TaskStore = request.app.state.store
    task = store.get(task_id)
    settings: Settings = request.app.state.settings
    if not task:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Task not found", "target_repo": settings.target_repo},
            status_code=404,
        )
    return templates.TemplateResponse(
        "task_detail.html",
        {"request": request, "task": task, "target_repo": settings.target_repo},
    )


@router.get("/tasks/{task_id}/panel", response_class=HTMLResponse)
async def task_panel(request: Request, task_id: str) -> HTMLResponse:
    """Render just the task detail fragment for the dashboard's right pane."""
    store: TaskStore = request.app.state.store
    settings: Settings = request.app.state.settings
    task = store.get(task_id)
    if not task:
        return HTMLResponse(
            '<div class="card"><p class="meta">Task not found.</p></div>',
            status_code=404,
        )
    return templates.TemplateResponse(
        "_task_panel.html",
        {"request": request, "task": task, "target_repo": settings.target_repo},
    )


@router.post("/tasks/begin/{issue_number}")
async def begin_task(
    request: Request,
    issue_number: int,
) -> RedirectResponse:
    """Begin work on an issue by adding the trigger label on GitHub.

    There is no manual task creation path: adding the ``assign-devin`` label
    fires an ``issues.labeled`` webhook, which is the single entry point for
    task creation. This keeps GitHub and the dashboard as one source of truth.
    """
    settings: Settings = request.app.state.settings
    github = GitHubClient(settings)

    added = await github.add_label(issue_number, settings.trigger_label)
    if added:
        logger.info(
            "Trigger label added from dashboard",
            extra={
                "issue_number": issue_number,
                "repo": settings.target_repo,
                "label": settings.trigger_label,
            },
        )
    else:
        logger.warning(
            "Failed to add trigger label from dashboard",
            extra={"issue_number": issue_number, "repo": settings.target_repo},
        )
    return RedirectResponse(url="/", status_code=303)


@router.post("/tasks/{task_id}/approve")
async def approve_plan(
    request: Request,
    task_id: str,
    plan_markdown: str = Form(default=""),
) -> RedirectResponse:
    """Gate-1 approval: send the (possibly edited) plan back to implement."""
    settings: Settings = request.app.state.settings
    store: TaskStore = request.app.state.store

    task = store.get(task_id)
    if not task:
        return RedirectResponse(url="/", status_code=303)

    await approve_task(task, store, settings, plan_markdown or None)
    return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)


@router.post("/tasks/{task_id}/request-changes")
async def request_task_changes(
    request: Request,
    task_id: str,
    feedback: str = Form(default=""),
) -> RedirectResponse:
    """Send review feedback to the session and loop back (re-plan / re-implement)."""
    settings: Settings = request.app.state.settings
    store: TaskStore = request.app.state.store

    task = store.get(task_id)
    if not task:
        return RedirectResponse(url="/", status_code=303)

    await request_changes(task, store, settings, feedback)
    return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)


@router.get("/tasks/{task_id}/plan.md", response_class=PlainTextResponse)
async def export_plan(request: Request, task_id: str) -> PlainTextResponse:
    """Download the implementation plan as a Markdown file."""
    store: TaskStore = request.app.state.store
    task = store.get(task_id)
    if not task or not (task.plan_markdown or task.plan):
        return PlainTextResponse("No plan available.", status_code=404)
    content = task.plan_markdown or (task.plan.to_markdown() if task.plan else "")
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f'attachment; filename="plan-{task.issue_number}.md"'
        },
        media_type="text/markdown",
    )


@router.post("/tasks/{task_id}/merge")
async def merge_pr(request: Request, task_id: str) -> RedirectResponse:
    """Merge the PR associated with a task via the GitHub API."""
    settings: Settings = request.app.state.settings
    store: TaskStore = request.app.state.store
    github = GitHubClient(settings)

    task = store.get(task_id)
    if not task:
        return RedirectResponse(url="/", status_code=303)

    if not task.pr_number:
        logger.warning(
            "Cannot merge - no PR number on task",
            extra={"task_id": task_id},
        )
        return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)

    merged = await github.merge_pull_request(task.pr_number)
    if merged:
        await merge_task(task, store)
        logger.info(
            "PR merged from dashboard",
            extra={
                "task_id": task.id,
                "issue_number": task.issue_number,
                "pr_number": task.pr_number,
            },
        )
    else:
        logger.warning(
            "Failed to merge PR from dashboard",
            extra={
                "task_id": task.id,
                "pr_number": task.pr_number,
            },
        )

    return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)
