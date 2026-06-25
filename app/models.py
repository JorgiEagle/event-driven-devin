"""Task and event models with explicit lifecycle states."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, enum.Enum):
    """Task lifecycle statuses."""

    READY = "ready"
    QUEUED = "queued"
    RESOLVING = "resolving"
    READY_TO_MERGE = "ready_to_merge"
    MERGED = "merged"
    FAILED = "failed"
    ATTENTION_REQUIRED = "attention_required"


class StatusTransition(BaseModel):
    """A single state transition record."""

    from_status: Optional[TaskStatus] = None
    to_status: TaskStatus
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    reason: str = ""


class Task(BaseModel):
    """Represents a tracked automation task."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    issue_number: int
    issue_title: str = ""
    issue_url: str = ""
    repo: str = ""
    status: TaskStatus = TaskStatus.READY
    trigger: str = "webhook"  # "webhook" or "manual"
    devin_session_id: Optional[str] = None
    devin_session_url: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    test_result: Optional[str] = None  # "passed", "failed", or None
    test_summary: Optional[str] = None  # Human-readable test outcome
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    transitions: list[StatusTransition] = Field(default_factory=list)

    def transition_to(self, new_status: TaskStatus, reason: str = "") -> None:
        """Record a state transition."""
        transition = StatusTransition(
            from_status=self.status,
            to_status=new_status,
            timestamp=datetime.now(timezone.utc).isoformat(),
            reason=reason,
        )
        self.transitions.append(transition)
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc).isoformat()
