from __future__ import annotations

import json
import os
import shlex
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from overnight_runner.models import ExecutorConfig, TaskAttachment, TaskDefinition

DEFAULT_REPOSITORY_ID = "default"

DEFAULT_BACKLOG_DOCUMENT = {
    "defaults": {
        "repository_id": DEFAULT_REPOSITORY_ID,
        "base_branch": "main",
        "working_directory": ".",
        "retry_limit": 2,
        "executor": {"type": "codex"},
    },
    "repositories": [],
    "tasks": [],
}


def _is_reviewed_task(raw_task: Dict[str, Any]) -> bool:
    return bool(raw_task.get("reviewed"))


def _expand_command_arguments(command: List[str]) -> List[str]:
    expanded = []
    for part in command:
        value = os.path.expandvars(part)
        value = value.replace("${RUNNER_PYTHON}", sys.executable)
        expanded.append(value)
    return expanded


def _normalize_command(raw_command: Any) -> List[str]:
    if raw_command is None:
        return []
    if isinstance(raw_command, list) and all(isinstance(item, str) for item in raw_command):
        return _expand_command_arguments(raw_command)
    if isinstance(raw_command, str):
        return _expand_command_arguments(shlex.split(raw_command))
    raise ValueError("Each task test_command must be a string, a list of strings, or omitted.")


def _merge_executor(defaults: Dict[str, Any], raw_task: Dict[str, Any]) -> ExecutorConfig:
    merged = dict(defaults.get("executor", {}))
    merged.update(raw_task.get("executor", {}))
    return ExecutorConfig(
        type=merged.get("type", "codex"),
        prompt=merged.get("prompt", ""),
    )


def _normalize_repository_id(raw_repository_id: Any) -> str:
    return str(raw_repository_id or "").strip()


def normalize_repository_entries(
    raw_repositories: Any,
    fallback_repo_root: Optional[Path] = None,
) -> List[Dict[str, str]]:
    if raw_repositories in (None, ""):
        raw_repositories = []
    if not isinstance(raw_repositories, list):
        raise ValueError("Backlog repositories must be a list when provided.")

    repositories: List[Dict[str, str]] = []
    seen_ids = set()
    for raw_repository in raw_repositories:
        if not isinstance(raw_repository, dict):
            raise ValueError("Each backlog repository must be an object.")
        repository_id = _normalize_repository_id(raw_repository.get("id"))
        repository_path = str(raw_repository.get("path", "")).strip()
        if not repository_id:
            raise ValueError("Each backlog repository must include a non-empty `id` field.")
        if not repository_path:
            raise ValueError(
                f"Backlog repository `{repository_id}` must include a non-empty `path` field."
            )
        if repository_id in seen_ids:
            raise ValueError(f"Backlog repository id `{repository_id}` is duplicated.")
        seen_ids.add(repository_id)
        repositories.append(
            {
                "id": repository_id,
                "path": str(Path(repository_path).expanduser().resolve()),
            }
        )

    if repositories:
        return repositories

    if fallback_repo_root is None:
        return []
    return [
        {
            "id": DEFAULT_REPOSITORY_ID,
            "path": str(fallback_repo_root.expanduser().resolve()),
        }
    ]


def build_repository_registry(
    document: Dict[str, Any],
    fallback_repo_root: Optional[Path] = None,
) -> Dict[str, str]:
    return {
        repository["id"]: repository["path"]
        for repository in normalize_repository_entries(
            document.get("repositories", []),
            fallback_repo_root=fallback_repo_root,
        )
    }


def _normalize_attachments(
    raw_attachments: Any,
    backlog_root: Optional[Path],
) -> List[TaskAttachment]:
    if raw_attachments in (None, ""):
        return []
    if not isinstance(raw_attachments, list):
        raise ValueError("Task attachments must be a list when provided.")

    attachments = []
    resolved_backlog_root = backlog_root.resolve() if backlog_root else None
    for raw_attachment in raw_attachments:
        if not isinstance(raw_attachment, dict):
            raise ValueError("Each task attachment must be an object.")

        attachment_id = str(raw_attachment.get("id", "")).strip()
        name = str(raw_attachment.get("name", "")).strip()
        relative_path = str(raw_attachment.get("relative_path", "")).strip()
        if not attachment_id or not name or not relative_path:
            raise ValueError(
                "Each task attachment must include non-empty `id`, `name`, and `relative_path` fields."
            )

        if resolved_backlog_root is not None:
            attachment_path = (resolved_backlog_root / relative_path).resolve()
            if attachment_path != resolved_backlog_root and resolved_backlog_root not in attachment_path.parents:
                raise ValueError(
                    f"Attachment path `{relative_path}` for task `{raw_attachment.get('id', '')}` escapes the backlog directory."
                )
        else:
            attachment_path = Path(relative_path)

        attachments.append(
            TaskAttachment(
                id=attachment_id,
                name=name,
                path=str(attachment_path),
                relative_path=relative_path,
                content_type=str(
                    raw_attachment.get("content_type") or "application/octet-stream"
                ).strip()
                or "application/octet-stream",
                size_bytes=int(raw_attachment.get("size_bytes") or 0),
            )
        )
    return attachments


def build_task_definition(
    defaults: Dict[str, Any],
    raw_task: Dict[str, Any],
    backlog_root: Optional[Path] = None,
    repository_registry: Optional[Dict[str, str]] = None,
) -> TaskDefinition:
    merged = dict(defaults)
    merged.update(raw_task)
    executor = _merge_executor(defaults, raw_task)
    normalized_registry = repository_registry or {}
    repository_id = _normalize_repository_id(
        merged.get("repository_id")
        or merged.get("repository")
        or defaults.get("repository_id")
    )
    if not repository_id:
        repository_id = (
            next(iter(normalized_registry))
            if normalized_registry
            else DEFAULT_REPOSITORY_ID
        )
    if normalized_registry:
        if repository_id not in normalized_registry:
            available = ", ".join(sorted(normalized_registry))
            raise ValueError(
                f"Task `{merged.get('id', '')}` references unknown repository "
                f"`{repository_id}`. Available repositories: {available}."
            )
        repository_path = normalized_registry[repository_id]
    else:
        repository_path = str(merged.get("repository_path", "")).strip()

    return TaskDefinition(
        id=merged["id"],
        title=merged["title"],
        description=merged.get("description", ""),
        repository_id=repository_id,
        repository_path=(
            str(Path(repository_path).expanduser().resolve())
            if repository_path
            else ""
        ),
        base_branch=merged.get("base_branch", "main"),
        working_directory=merged.get("working_directory", "."),
        test_command=_normalize_command(merged.get("test_command", [])),
        retry_limit=int(merged.get("retry_limit", 2)),
        executor=executor,
        attachments=_normalize_attachments(merged.get("attachments", []), backlog_root),
    )


def load_backlog_document(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return deepcopy(DEFAULT_BACKLOG_DOCUMENT)

    data = json.loads(path.read_text(encoding="utf-8"))
    document = deepcopy(DEFAULT_BACKLOG_DOCUMENT)
    document["defaults"].update(data.get("defaults", {}))
    document["repositories"] = data.get("repositories", [])
    document["tasks"] = data.get("tasks", [])
    return document


def save_backlog_document(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "defaults": document.get("defaults", deepcopy(DEFAULT_BACKLOG_DOCUMENT["defaults"])),
        "repositories": document.get(
            "repositories",
            deepcopy(DEFAULT_BACKLOG_DOCUMENT["repositories"]),
        ),
        "tasks": document.get("tasks", []),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_backlog(
    path: Path,
    default_repo_root: Optional[Path] = None,
) -> List[TaskDefinition]:
    data = load_backlog_document(path)
    defaults = data.get("defaults", {})
    repository_registry = build_repository_registry(
        data,
        fallback_repo_root=default_repo_root,
    )
    raw_tasks = [task for task in data.get("tasks", []) if not _is_reviewed_task(task)]
    if not raw_tasks:
        raise ValueError(f"Backlog {path} does not contain any active tasks.")
    return [
        build_task_definition(
            defaults,
            raw_task,
            backlog_root=path.parent,
            repository_registry=repository_registry,
        )
        for raw_task in raw_tasks
    ]
