from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from overnight_runner.models import (
    EventLogger,
    TaskAttachment,
    TaskDefinition,
    TaskExecutor,
)

_COMMON_CODEX_BINARIES = (
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
)
_ATTACHMENT_GUARD_MODE_ENV = "CODEX_ATTACHMENT_GUARD"
_SESSION_ID_PATTERN = re.compile(
    r"^\s*session id:\s*([0-9a-fA-F-]{36})\s*$",
    re.MULTILINE,
)
_IMAGE_ATTACHMENT_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
}
_IMAGE_ERROR_PATTERNS = (
    re.compile(r"(?im)\bfailed\b.*\bimage\b"),
    re.compile(r"(?im)\bimage\b.*\bfailed\b"),
    re.compile(r"(?im)\bunable\b.*\bimage\b"),
    re.compile(r"(?im)\bcannot\b.*\bimage\b"),
    re.compile(r"(?im)\berror\b.*\bimage\b"),
    re.compile(r"(?im)\bimage\b.*\berror\b"),
)


def _resolve_executable(candidate: str) -> Optional[str]:
    expanded = os.path.expanduser(candidate)
    if os.path.sep in expanded:
        path = Path(expanded)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    return shutil.which(expanded)


def resolve_codex_binary() -> Optional[str]:
    configured = os.environ.get("CODEX_BIN", "").strip()
    candidates = []
    if configured:
        candidates.append(configured)
    candidates.append("codex")
    candidates.extend(_COMMON_CODEX_BINARIES)
    home = Path.home()
    candidates.extend(
        [
            str(home / ".local" / "bin" / "codex"),
            str(home / "bin" / "codex"),
        ]
    )

    seen = set()
    for candidate in candidates:
        resolved = _resolve_executable(candidate)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        return resolved
    return None


def _format_attachment_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _attachment_guard_mode() -> str:
    raw_mode = os.environ.get(_ATTACHMENT_GUARD_MODE_ENV, "log").strip().lower()
    if raw_mode in {"", "log", "warn", "warning"}:
        return "log"
    if raw_mode in {"fail", "strict", "error"}:
        return "fail"
    if raw_mode in {"off", "none", "0", "false", "disabled"}:
        return "off"
    return "log"


def _attachment_is_likely_image(attachment: TaskAttachment) -> bool:
    content_type = (attachment.content_type or "").strip().lower()
    if content_type.startswith("image/"):
        return True
    return Path(attachment.path).suffix.lower() in _IMAGE_ATTACHMENT_EXTENSIONS


def _collect_attachment_checks(task: TaskDefinition) -> List[dict]:
    checks = []
    for attachment in task.attachments:
        path = Path(attachment.path).expanduser().resolve()
        content_type = (attachment.content_type or "application/octet-stream").strip() or (
            "application/octet-stream"
        )
        content_type_is_image = content_type.lower().startswith("image/")
        likely_image = _attachment_is_likely_image(attachment)
        checks.append(
            {
                "id": attachment.id,
                "name": attachment.name,
                "path": str(path),
                "content_type": content_type,
                "exists": path.exists(),
                "is_file": path.is_file(),
                "readable": path.is_file() and os.access(path, os.R_OK),
                "is_image_by_content_type": content_type_is_image,
                "is_likely_image": likely_image,
                "attached_as_image": likely_image,
                "uses_extension_fallback": likely_image and not content_type_is_image,
            }
        )
    return checks


def _attachment_preflight_issues(attachment_checks: List[dict]) -> List[str]:
    issues = []
    for check in attachment_checks:
        if not check["exists"]:
            issues.append(
                f"{check['name']} ({check['id']}): attachment path does not exist ({check['path']})."
            )
            continue
        if not check["is_file"]:
            issues.append(
                f"{check['name']} ({check['id']}): attachment path is not a file ({check['path']})."
            )
            continue
        if not check["readable"]:
            issues.append(
                f"{check['name']} ({check['id']}): attachment is not readable ({check['path']})."
            )
    return issues


def _extract_session_id(stderr_text: str) -> str:
    match = _SESSION_ID_PATTERN.search(stderr_text or "")
    return match.group(1).strip() if match else ""


def _extract_image_related_errors(stderr_text: str) -> List[str]:
    matches = []
    for line in (stderr_text or "").splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        if any(pattern.search(normalized) for pattern in _IMAGE_ERROR_PATTERNS):
            matches.append(normalized)
    return matches


def _attachment_guard_issues(
    task: TaskDefinition,
    attachment_checks: List[dict],
    stderr_text: str,
    session_id: str,
) -> List[str]:
    issues: List[str] = []
    if not task.attachments:
        return issues

    if task.attachments and not session_id:
        issues.append(
            "Codex output did not expose a `session id`, so attachment traceability is limited."
        )

    missing_prompt_paths = [
        str(check["path"])
        for check in attachment_checks
        if str(check["path"]) and str(check["path"]) not in (stderr_text or "")
    ]
    if missing_prompt_paths:
        issues.append(
            "Some attachment paths were not echoed in Codex stderr user prompt: "
            + ", ".join(missing_prompt_paths)
        )

    image_errors = _extract_image_related_errors(stderr_text)
    if image_errors:
        issues.append(
            "Codex stderr contains image-related error/warning lines: " + " | ".join(image_errors)
        )

    return issues


def _attachment_reference_lines(task: TaskDefinition) -> list[str]:
    if not task.attachments:
        return []

    lines = [
        "Attached reference files:",
    ]
    for attachment in task.attachments:
        attachment_type = attachment.content_type or "application/octet-stream"
        lines.append(
            f"- {attachment.name} ({attachment_type}, {_format_attachment_size(attachment.size_bytes)})"
        )
        lines.append(f"  Path: {attachment.path}")
    lines.extend(
        [
            "",
            "Use the attached files as extra task context when relevant.",
            "Image attachments are also attached directly to the initial Codex prompt.",
        ]
    )
    return lines


def _build_codex_prompt(task: TaskDefinition, feedback: str) -> str:
    sections = [
        "You are working inside the reusable git worktree for one backlog task.",
        "Implement the task directly in the repository files.",
        "Do not create or switch branches.",
        "Do not commit, push, or open a pull request.",
        "Do not modify files outside this repository worktree.",
        "Prefer a small, clear implementation that is easy to review.",
        "",
        f"Task ID: {task.id}",
        f"Title: {task.title}",
        f"Description: {task.description or 'No description provided.'}",
        f"Primary working directory: {task.working_directory}",
        (
            "Validation command: " + " ".join(task.test_command)
            if task.test_command
            else "Validation command: none configured"
        ),
        "",
    ]

    attachment_lines = _attachment_reference_lines(task)
    if attachment_lines:
        sections.extend(attachment_lines)
        sections.append("")

    sections.extend(
        [
        "Additional executor instructions:",
        task.executor.prompt or "No additional instructions provided.",
        "",
        "Feedback from the previous attempt:",
        feedback or "None. This is the first attempt.",
        "",
        "When you finish, leave the repository in an edited state and return a short summary of what changed.",
        ]
    )
    return "\n".join(sections)


class NoopTaskExecutor:
    def execute(
        self,
        task: TaskDefinition,
        attempt: int,
        task_root: Path,
        worktree_path: Path,
        feedback: str,
        logger: EventLogger,
    ) -> Path:
        artifact_path = task_root / f"attempt-{attempt:02d}-executor-note.md"
        artifact_path.write_text(
            "\n".join(
                [
                    f"# Executor placeholder for {task.id}",
                    "",
                    "The minimal version does not call an LLM yet.",
                    f"Task title: {task.title}",
                    f"Attempt: {attempt}",
                    f"Worktree: {worktree_path}",
                    f"Feedback from previous validation: {feedback or 'None'}",
                    f"Requested executor type: {task.executor.type}",
                    f"Prompt placeholder: {task.executor.prompt or 'Not provided'}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        logger.log(
            "executor",
            "completed",
            "Wrote a placeholder executor artifact.",
            artifact_path=str(artifact_path),
        )
        return artifact_path


class CodexTaskExecutor:
    def __init__(self, codex_bin: Optional[str] = None, model: Optional[str] = None) -> None:
        self.codex_bin = codex_bin or resolve_codex_binary()
        self.model = model
        self.profile = os.environ.get("CODEX_PROFILE", "cli").strip() or "cli"

    def execute(
        self,
        task: TaskDefinition,
        attempt: int,
        task_root: Path,
        worktree_path: Path,
        feedback: str,
        logger: EventLogger,
    ) -> Path:
        if not self.codex_bin:
            raise RuntimeError(
                "Could not find the `codex` CLI. Install it locally or set CODEX_BIN."
            )

        artifact_path = task_root / f"attempt-{attempt:02d}-executor-note.md"
        stdout_path = task_root / f"attempt-{attempt:02d}-executor.stdout.log"
        stderr_path = task_root / f"attempt-{attempt:02d}-executor.stderr.log"
        prompt = _build_codex_prompt(task, feedback)
        attachment_guard_mode = _attachment_guard_mode()
        attachment_checks = _collect_attachment_checks(task)
        attachment_preflight_issues = _attachment_preflight_issues(attachment_checks)
        logger.log(
            "attachments",
            "started",
            "Running attachment preflight checks before launching Codex.",
            attempt=attempt,
            guard_mode=attachment_guard_mode,
            attachment_count=len(task.attachments),
            image_attachment_count=sum(
                1 for check in attachment_checks if check["attached_as_image"]
            ),
            extension_fallback_image_count=sum(
                1 for check in attachment_checks if check["uses_extension_fallback"]
            ),
            attachments=attachment_checks,
        )
        if attachment_preflight_issues:
            logger.log(
                "attachments",
                "failed",
                "Attachment preflight checks failed.",
                attempt=attempt,
                guard_mode=attachment_guard_mode,
                issues=attachment_preflight_issues,
                extension_fallback_image_count=sum(
                    1 for check in attachment_checks if check["uses_extension_fallback"]
                ),
                attachments=attachment_checks,
            )
            raise RuntimeError(
                "Attachment preflight failed: " + " ".join(attachment_preflight_issues)
            )
        logger.log(
            "attachments",
            "completed",
            "Attachment preflight checks passed.",
            attempt=attempt,
            guard_mode=attachment_guard_mode,
            attachment_count=len(task.attachments),
            image_attachment_count=sum(
                1 for check in attachment_checks if check["attached_as_image"]
            ),
            extension_fallback_image_count=sum(
                1 for check in attachment_checks if check["uses_extension_fallback"]
            ),
        )

        command = [
            self.codex_bin,
            "exec",
            "--full-auto",
            "--ephemeral",
            "--cd",
            str(worktree_path),
            "--output-last-message",
            str(artifact_path),
            "--profile",
            self.profile,
        ]
        if self.model:
            command.extend(["--model", self.model])
        attachment_parent_dirs = sorted(
            {str(Path(attachment.path).resolve().parent) for attachment in task.attachments}
        )
        for attachment_dir in attachment_parent_dirs:
            command.extend(["--add-dir", attachment_dir])
        for check in attachment_checks:
            if check["attached_as_image"]:
                command.extend(["--image", check["path"]])
        command.append(prompt)

        logger.log(
            "executor",
            "started",
            "Launching Codex for this attempt.",
            attempt=attempt,
            command=command,
            cwd=str(worktree_path),
        )
        completed = subprocess.run(
            command,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
        )
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if not artifact_path.exists():
            artifact_path.write_text("", encoding="utf-8")

        session_id = _extract_session_id(completed.stderr)
        attachment_guard_issues = _attachment_guard_issues(
            task=task,
            attachment_checks=attachment_checks,
            stderr_text=completed.stderr,
            session_id=session_id,
        )
        image_flag_count = sum(1 for value in command if value == "--image")
        if attachment_guard_issues:
            logger.log(
                "attachment_guard",
                "failed",
                "Attachment guard detected potential ingestion/visibility issues.",
                attempt=attempt,
                guard_mode=attachment_guard_mode,
                issues=attachment_guard_issues,
                session_id=session_id,
                image_flag_count=image_flag_count,
                expected_image_attachments=sum(
                    1 for check in attachment_checks if check["attached_as_image"]
                ),
                stderr_path=str(stderr_path),
                stdout_path=str(stdout_path),
            )
        else:
            logger.log(
                "attachment_guard",
                "completed",
                "Attachment guard checks passed with no anomalies.",
                attempt=attempt,
                guard_mode=attachment_guard_mode,
                session_id=session_id,
                image_flag_count=image_flag_count,
                expected_image_attachments=sum(
                    1 for check in attachment_checks if check["attached_as_image"]
                ),
            )

        logger.log(
            "executor",
            "completed" if completed.returncode == 0 else "failed",
            "Codex finished this attempt."
            if completed.returncode == 0
            else "Codex returned a non-zero exit code.",
            attempt=attempt,
            returncode=completed.returncode,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            artifact_path=str(artifact_path),
            session_id=session_id,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Codex execution failed. "
                f"See {stderr_path} for stderr and {artifact_path} for the final message."
            )
        if attachment_guard_mode == "fail" and attachment_guard_issues:
            raise RuntimeError(
                "Attachment guard failed in strict mode. "
                f"See {stderr_path} and {stdout_path} for details."
            )
        return artifact_path


class DelegatingTaskExecutor:
    def __init__(
        self,
        executors: Dict[str, TaskExecutor],
        default_executor_type: str = "codex",
    ) -> None:
        self.executors = executors
        self.default_executor_type = default_executor_type

    def execute(
        self,
        task: TaskDefinition,
        attempt: int,
        task_root: Path,
        worktree_path: Path,
        feedback: str,
        logger: EventLogger,
    ) -> Path:
        executor_type = (task.executor.type or self.default_executor_type).strip().lower()
        executor = self.executors.get(executor_type)
        if executor is None:
            available = ", ".join(sorted(self.executors))
            raise ValueError(
                f"Unsupported executor type `{executor_type}` for task {task.id}. "
                f"Available executors: {available}."
            )
        return executor.execute(
            task=task,
            attempt=attempt,
            task_root=task_root,
            worktree_path=worktree_path,
            feedback=feedback,
            logger=logger,
        )
