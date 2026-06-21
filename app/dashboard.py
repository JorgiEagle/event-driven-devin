"""Dashboard routes: web UI for task listing, history, and manual kickoff."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.models import Task, TaskStatus
from app.store import TaskStore
from app.tasks import complete_task, fail_task, kickoff_task, merge_task

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the main dashboard with task list."""
    store: TaskStore = request.app.state.store
    tasks = store.list_all()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "tasks": tasks},
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


@router.post("/tasks/kickoff")
async def manual_kickoff(
    request: Request,
    issue_number: int = Form(...),
    issue_title: str = Form(""),
    issue_url: str = Form(""),
    repo: str = Form(""),
) -> RedirectResponse:
    """Manually kick off a task for a given issue (same workflow as webhook)."""
    settings: Settings = request.app.state.settings
    store: TaskStore = request.app.state.store

    repo = repo or settings.target_repo

    # Check for duplicate
    existing = store.get_by_issue(repo, issue_number)
    if existing and existing.status not in (TaskStatus.FAILED, TaskStatus.MERGED):
        logger.info(
            "Manual kickoff skipped - task exists",
            extra={
                "task_id": existing.id,
                "issue_number": issue_number,
                "state": existing.status.value,
            },
        )
        return RedirectResponse(url=f"/tasks/{existing.id}", status_code=303)

    task = Task(
        issue_number=issue_number,
        issue_title=issue_title or f"Issue #{issue_number}",
        issue_url=issue_url or f"https://github.com/{repo}/issues/{issue_number}",
        repo=repo,
        trigger="manual",
    )
    task.transition_to(TaskStatus.READY, reason="Manual kickoff from dashboard")
    store.add(task)

    logger.info(
        "Task created manually",
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


@router.post("/tasks/{task_id}/status")
async def update_task_status(
    request: Request,
    task_id: str,
    new_status: str = Form(...),
    reason: str = Form(""),
    pr_url: str = Form(""),
) -> RedirectResponse:
    """Manually update a task's status (for testing/admin)."""
    settings: Settings = request.app.state.settings
    store: TaskStore = request.app.state.store
    task = store.get(task_id)

    if not task:
        return RedirectResponse(url="/", status_code=303)

    status = TaskStatus(new_status)

    if status == TaskStatus.READY_TO_MERGE:
        await complete_task(task, store, settings, pr_url=pr_url or None)
    elif status == TaskStatus.MERGED:
        await merge_task(task, store)
    elif status == TaskStatus.FAILED:
        await fail_task(task, store, reason=reason)
    else:
        task.transition_to(status, reason=reason)
        store.update(task)
        logger.info(
            "Task status updated manually",
            extra={
                "task_id": task.id,
                "issue_number": task.issue_number,
                "event_type": "manual_update",
                "state": task.status.value,
            },
        )

    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)
