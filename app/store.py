"""JSON file-based task persistence."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from app.models import Task


class TaskStore:
    """Thread-safe JSON-backed task store."""

    def __init__(self, data_dir: str = "/data") -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "tasks.json"
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            with open(self._file) as f:
                raw = json.load(f)
            self._tasks = {k: Task.model_validate(v) for k, v in raw.items()}

    def _save(self) -> None:
        with open(self._file, "w") as f:
            json.dump({k: v.model_dump() for k, v in self._tasks.items()}, f, indent=2)

    def add(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.id] = task
            self._save()
        return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def get_by_issue(self, repo: str, issue_number: int) -> Optional[Task]:
        for task in self._tasks.values():
            if task.repo == repo and task.issue_number == issue_number:
                return task
        return None

    def update(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.id] = task
            self._save()
        return task

    def list_all(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
