from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Protocol


@dataclass(frozen=True)
class RegisteredRepository:
    id: str
    path: str


@dataclass(frozen=True)
class ExecutorConfig:
    type: str = "codex"
    prompt: str = ""


@dataclass(frozen=True)
class TaskAttachment:
    id: str
    name: str
    path: str
    relative_path: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0


@dataclass(frozen=True)
class TaskDefinition:
    id: str
    title: str
    description: str
    base_branch: str
    working_directory: str
    test_command: List[str]
    retry_limit: int
    executor: ExecutorConfig
    attachments: List[TaskAttachment] = field(default_factory=list)
    repository_id: str = "default"
    repository_path: str = ""


@dataclass(frozen=True)
class PullRequestResult:
    status: str
    url: str = ""
    number: int = 0


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    title: str
    status: str
    branch_name: str
    attempts: int
    log_path: Path
    summary_path: Path
    pull_request_status: str
    pull_request_url: str = ""
    repository_id: str = "default"
    repository_path: str = ""
    workflow_state_path: Path = field(default_factory=Path)
    resume_history_path: Path = field(default_factory=Path)
    workflow_state: Dict[str, Any] = field(default_factory=dict)
    resume_history: List[Dict[str, Any]] = field(default_factory=list)
    last_blocker: Dict[str, Any] = field(default_factory=dict)
    resume_count: int = 0
    last_resume_from: str = ""


class EventLogger(Protocol):
    def log(self, step: str, status: str, message: str, **context: object) -> None:
        ...


class TaskExecutor(Protocol):
    def execute(
        self,
        task: TaskDefinition,
        attempt: int,
        task_root: Path,
        worktree_path: Path,
        feedback: str,
        logger: EventLogger,
    ) -> Path:
        ...


class PullRequestPublisher(Protocol):
    def publish(
        self,
        task: TaskDefinition,
        branch_name: str,
        worktree_path: Path,
        logger: EventLogger,
    ) -> PullRequestResult:
        ...
