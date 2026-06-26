"""Task and event models with explicit lifecycle states."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, enum.Enum):
    """Task lifecycle statuses.

    Active 3-stage flow:
        READY -> PLANNING -> AWAITING_REVIEW -> IMPLEMENTING -> AWAITING_REVIEW -> MERGED

    AWAITING_REVIEW is a single review gate used at two points, distinguished by
    whether a PR exists yet:
        Gate 1 (no PR):  review/edit plan -> approve -> IMPLEMENTING
                                          -> request changes -> PLANNING
        Gate 2 (PR set): review PR        -> merge -> MERGED
                                          -> request changes -> IMPLEMENTING

    FAILED and ATTENTION_REQUIRED cover hard failures and non-fatal issues.
    QUEUED / RESOLVING / READY_TO_MERGE are retained only for backward
    compatibility with tasks persisted before the 3-stage workflow.
    """

    READY = "ready"
    PLANNING = "planning"
    AWAITING_REVIEW = "awaiting_review"
    IMPLEMENTING = "implementing"
    MERGED = "merged"
    FAILED = "failed"
    ATTENTION_REQUIRED = "attention_required"

    # Legacy statuses (pre-3-stage); kept for backward compatibility.
    QUEUED = "queued"
    RESOLVING = "resolving"
    READY_TO_MERGE = "ready_to_merge"


class FileChange(BaseModel):
    """A single file the plan proposes to change."""

    path: str = ""
    change: str = ""
    why: str = ""


class ImplementationPlan(BaseModel):
    """Structured plan returned by Devin during the planning stage.

    Mirrors the ``structured_output_schema`` sent on session creation so the
    plan comes back in a fixed shape every time.
    """

    issue_summary: str = ""
    root_causes: list[str] = Field(default_factory=list)
    proposed_fix: str = ""
    reasoning: str = ""
    files_changing: list[FileChange] = Field(default_factory=list)
    confidence: int = 0

    def to_markdown(self) -> str:
        """Render the plan as a Markdown document (editable / exportable)."""
        lines: list[str] = ["# Implementation Plan", ""]
        lines.append(f"**Confidence:** {self.confidence}/100")
        lines.append("")
        lines.append("## Issue Summary")
        lines.append(self.issue_summary or "_(none)_")
        lines.append("")
        lines.append("## Root Causes")
        if self.root_causes:
            lines.extend(f"- {cause}" for cause in self.root_causes)
        else:
            lines.append("_(none)_")
        lines.append("")
        lines.append("## Proposed Fix")
        lines.append(self.proposed_fix or "_(none)_")
        lines.append("")
        lines.append("## Reasoning")
        lines.append(self.reasoning or "_(none)_")
        lines.append("")
        lines.append("## Files Changing")
        if self.files_changing:
            for fc in self.files_changing:
                lines.append(f"- **{fc.path}** — {fc.change}")
                if fc.why:
                    lines.append(f"  - _why:_ {fc.why}")
        else:
            lines.append("_(none)_")
        lines.append("")
        return "\n".join(lines)


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
    # 3-stage workflow fields
    plan: Optional[ImplementationPlan] = None  # structured plan from Devin
    plan_markdown: Optional[str] = None  # editable/exported Markdown of the plan
    plan_approved: bool = False  # set when the Gate-1 plan is approved
    replan_count: int = 0  # number of "request changes" feedback loops
    review_summary: Optional[str] = None  # Gate-2: Devin Review comments + tests
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

    @property
    def confidence_label(self) -> Optional[str]:
        """Human-readable suitability label derived from the plan confidence."""
        if self.plan is None:
            return None
        score = self.plan.confidence
        if score >= 70:
            return "Ready to implement"
        if score >= 40:
            return "Needs attention"
        return "Not suitable for Devin"

    @property
    def confidence_class(self) -> Optional[str]:
        """CSS modifier key for the confidence badge."""
        if self.plan is None:
            return None
        score = self.plan.confidence
        if score >= 70:
            return "high"
        if score >= 40:
            return "medium"
        return "low"

    @property
    def is_gate_two(self) -> bool:
        """True when an AWAITING_REVIEW task is at the post-implementation gate."""
        return self.status == TaskStatus.AWAITING_REVIEW and bool(self.pr_url)
