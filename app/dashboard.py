"""Dashboard routes: web UI for issue listing and manual task kickoff."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.github_client import GitHubClient
from app.models import Task, TaskStatus
from app.store import TaskStore
from app.tasks import kickoff_task

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

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "issues_with_status": issues_with_status,
            "tasks": tasks,
            "target_repo": settings.target_repo,
        },
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str) -> HTMLResponse:
    """Render task detail with full status history."""
    store: TaskStore = request.app.state.store
    task = store.get(task_id)
    if not task:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Task not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "task_detail.html",
        {"request": request, "task": task},
    )


@router.post("/tasks/kickoff/{issue_number}")
async def manual_kickoff(
    request: Request,
    issue_number: int,
) -> RedirectResponse:
    """Kick off a Devin session for a specific GitHub issue.

    The issue data is fetched from GitHub (authoritative source).
    """
    settings: Settings = request.app.state.settings
    store: TaskStore = request.app.state.store
    github = GitHubClient(settings)

    repo = settings.target_repo

    # Check for existing active task
    existing = store.get_by_issue(repo, issue_number)
    if existing and existing.status not in (TaskStatus.FAILED, TaskStatus.MERGED):
        logger.info(
            "Manual kickoff skipped - task already active",
            extra={
                "task_id": existing.id,
                "issue_number": issue_number,
                "state": existing.status.value,
            },
        )
        return RedirectResponse(url=f"/tasks/{existing.id}", status_code=303)

    # Fetch issue details from GitHub
    issue_data = await github.get_issue(issue_number)

    if not issue_data:
        logger.warning(
            "Cannot kick off - issue not found on GitHub",
            extra={"issue_number": issue_number, "repo": repo},
        )
        return RedirectResponse(url="/", status_code=303)

    task = Task(
        issue_number=issue_number,
        issue_title=issue_data.get("title", f"Issue #{issue_number}"),
        issue_url=issue_data.get("html_url", f"https://github.com/{repo}/issues/{issue_number}"),
        repo=repo,
        trigger="manual",
    )
    task.transition_to(TaskStatus.READY, reason="Manual kickoff from dashboard")
    store.add(task)

    logger.info(
        "Task created via manual kickoff",
        extra={
            "task_id": task.id,
            "issue_number": issue_number,
            "repo": repo,
            "event_type": "manual",
            "state": task.status.value,
        },
    )

    await kickoff_task(task, store, settings)
    return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)
