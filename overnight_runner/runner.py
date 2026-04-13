from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from automation.overnight_runner.executors import NoopTaskExecutor
from automation.overnight_runner.git_ops import GitRepository, redact_sensitive_text
from automation.overnight_runner.models import (
    PullRequestPublisher,
    TaskDefinition,
    TaskExecutor,
    TaskResult,
)
from automation.overnight_runner.publishers import NoopPullRequestPublisher

TASK_BRANCH_PREFIX = "automator"


def create_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "task"


def task_branch_name(task_id: str) -> str:
    return f"{TASK_BRANCH_PREFIX}/{_slugify(task_id)}"


def task_worktree_name(task_id: str) -> str:
    return _slugify(task_id)


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _latest_attempt(events: List[Dict[str, Any]]) -> int:
    attempts = [
        int(event.get("context", {}).get("attempt"))
        for event in events
        if event.get("context", {}).get("attempt") is not None
    ]
    return max(attempts, default=0)


def _latest_event(events: List[Dict[str, Any]], step: str) -> Optional[Dict[str, Any]]:
    for event in reversed(events):
        if event.get("step") == step:
            return event
    return None


def _infer_task_status(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "pending"

    for event in reversed(events):
        if event.get("step") == "task" and event.get("status") == "passed":
            return "passed"
        if event.get("step") == "task" and event.get("status") in {"failed", "error"}:
            return "failed"

    for event in reversed(events):
        if event.get("status") in {"started", "retrying"}:
            return "running"
    return "running"


def _event_detail(event: Optional[Dict[str, Any]]) -> str:
    if not event:
        return ""
    context = event.get("context", {})
    detail = str(context.get("feedback") or context.get("error") or event.get("message", "")).strip()
    return redact_sensitive_text(detail)


def _split_execution_sessions(events: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    sessions: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for event in events:
        if event.get("step") == "task" and event.get("status") == "started":
            if current:
                sessions.append(current)
            current = [event]
            continue
        if current:
            current.append(event)
    if current:
        sessions.append(current)
    return sessions


def _status_label(raw_status: str) -> str:
    mapping = {
        "started": "running",
        "completed": "passed",
        "passed": "passed",
        "failed": "failed",
        "error": "failed",
        "retrying": "running",
        "skipped": "skipped",
        "current": "current",
    }
    return mapping.get(raw_status, "pending")


def _resume_stage_label(stage: str) -> str:
    labels = {
        "branch": "Branch",
        "code": "Code",
        "tests": "Tests",
        "pull_request": "PR",
    }
    return labels.get(stage, stage)


def _next_resume_stage(
    failure_stage: str,
    events: List[Dict[str, Any]],
    task_status: str,
    pull_request_status: str,
) -> str:
    if failure_stage in {"branch", "code", "tests", "pull_request"}:
        return failure_stage
    if task_status == "running":
        for event in reversed(events):
            if event.get("status") in {"started", "retrying"} and event.get("step") in {
                "branch",
                "code",
                "tests",
                "pull_request",
            }:
                return str(event["step"])
    if task_status == "passed" and pull_request_status != "passed":
        return "pull_request"
    return ""


def derive_task_documentation(
    events: List[Dict[str, Any]],
    status: str,
    pull_request_status: str,
    pull_request_url: str,
) -> Dict[str, Any]:
    sessions = _split_execution_sessions(events)
    resume_history: List[Dict[str, Any]] = []
    attempts_before = 0

    for session in sessions:
        started_event = session[0]
        started_context = started_event.get("context", {})
        session_attempts = _latest_attempt(session)
        attempts_after = max(attempts_before, session_attempts)
        session_task_status = _infer_task_status(session)
        session_pull_request_event = _latest_event(session, "pull_request")
        session_pull_request_status = (
            _status_label(str(session_pull_request_event.get("status", "")))
            if session_pull_request_event
            else (
                pull_request_status
                if session_task_status == "passed"
                else "skipped"
            )
        )
        session_pull_request_url = str(
            (session_pull_request_event or {}).get("context", {}).get("pr_url")
            or pull_request_url
            or ""
        )
        failure_event = next(
            (
                event
                for event in reversed(session)
                if event.get("status") in {"failed", "error"}
                and event.get("step") in {"branch", "code", "tests", "pull_request", "task"}
            ),
            None,
        )
        failure_stage = (
            str(failure_event.get("step"))
            if failure_event and failure_event.get("step") in {"branch", "code", "tests", "pull_request"}
            else ""
        )
        failure_detail = _event_detail(failure_event)
        push_events = [event for event in session if event.get("step") == "push"]
        push_completed_event = next(
            (event for event in reversed(push_events) if event.get("status") == "completed"),
            None,
        )
        push_retry_event = next(
            (event for event in reversed(push_events) if event.get("status") == "retrying"),
            None,
        )
        push_failure_event = next(
            (
                event
                for event in reversed(push_events)
                if event.get("status") in {"failed", "error", "retrying"}
            ),
            None,
        )
        push_status = (
            _status_label(str(push_completed_event.get("status", "")))
            if push_completed_event
            else (
                _status_label(str(push_failure_event.get("status", "")))
                if push_failure_event
                else "pending"
            )
        )
        if push_retry_event and push_completed_event:
            push_strategy = "remote_credentials_fallback"
        elif push_completed_event:
            push_strategy = "token"
        else:
            push_strategy = ""
        push_error = _event_detail(push_failure_event)
        next_stage = _next_resume_stage(
            failure_stage=failure_stage,
            events=session,
            task_status=session_task_status,
            pull_request_status=session_pull_request_status,
        )
        last_event = session[-1]
        session_record = {
            "sequence": len(resume_history) + 1,
            "requested_at": str(started_event.get("timestamp", "")),
            "finished_at": str(last_event.get("timestamp", "")),
            "resume_from": str(started_context.get("resume_from") or ""),
            "status": session_task_status,
            "attempts_before": attempts_before,
            "attempts_after": attempts_after,
            "failure_stage": failure_stage,
            "failure_detail": failure_detail,
            "next_resume_stage": next_stage,
            "next_resume_label": (
                f"Resume from {_resume_stage_label(next_stage)}" if next_stage else ""
            ),
            "branch_name": str(started_context.get("branch_name") or ""),
            "worktree_path": str(started_context.get("worktree_path") or ""),
            "pull_request_status": session_pull_request_status,
            "pull_request_url": session_pull_request_url,
            "push_status": push_status,
            "push_strategy": push_strategy,
            "push_error": push_error,
        }
        if started_context.get("resume_from"):
            resume_history.append(session_record)
        attempts_before = attempts_after

    latest_session = sessions[-1] if sessions else []
    latest_started_event = latest_session[0] if latest_session else {}
    latest_started_context = latest_started_event.get("context", {}) if latest_started_event else {}
    latest_failure_event = next(
        (
            event
            for event in reversed(latest_session)
            if event.get("status") in {"failed", "error"}
            and event.get("step") in {"branch", "code", "tests", "pull_request", "task"}
        ),
        None,
    )
    latest_failure_stage = (
        str(latest_failure_event.get("step"))
        if latest_failure_event and latest_failure_event.get("step") in {"branch", "code", "tests", "pull_request"}
        else ""
    )
    latest_failure_detail = _event_detail(latest_failure_event)
    latest_push_events = [event for event in latest_session if event.get("step") == "push"]
    latest_push_completed = next(
        (event for event in reversed(latest_push_events) if event.get("status") == "completed"),
        None,
    )
    latest_push_retry = next(
        (event for event in reversed(latest_push_events) if event.get("status") == "retrying"),
        None,
    )
    latest_push_failure = next(
        (
            event
            for event in reversed(latest_push_events)
            if event.get("status") in {"failed", "error", "retrying"}
        ),
        None,
    )
    latest_push_status = (
        _status_label(str(latest_push_completed.get("status", "")))
        if latest_push_completed
        else (
            _status_label(str(latest_push_failure.get("status", "")))
            if latest_push_failure
            else "pending"
        )
    )
    latest_push_strategy = (
        "remote_credentials_fallback"
        if latest_push_retry and latest_push_completed
        else ("token" if latest_push_completed else "")
    )
    latest_push_error = _event_detail(latest_push_failure)
    next_resume_stage = _next_resume_stage(
        failure_stage=latest_failure_stage,
        events=latest_session or events,
        task_status=status,
        pull_request_status=pull_request_status,
    )
    last_resume_from = (
        str(resume_history[-1].get("resume_from", ""))
        if resume_history
        else ""
    )
    last_blocker = (
        {
            "stage": next_resume_stage,
            "detail": latest_failure_detail,
            "timestamp": str((latest_failure_event or latest_started_event).get("timestamp", "")),
            "resume_label": (
                f"Resume from {_resume_stage_label(next_resume_stage)}" if next_resume_stage else ""
            ),
            "push_status": latest_push_status,
            "push_strategy": latest_push_strategy,
            "push_error": latest_push_error,
        }
        if next_resume_stage
        else {}
    )
    workflow_state = {
        "documentation_updated_at": str(events[-1].get("timestamp", "")) if events else "",
        "execution_mode": (
            "resume"
            if latest_started_context.get("resume_from")
            else ("initial" if latest_session else "")
        ),
        "status": status,
        "resume_count": len(resume_history),
        "last_resume_from": last_resume_from,
        "next_resume_stage": next_resume_stage,
        "next_resume_label": (
            f"Resume from {_resume_stage_label(next_resume_stage)}" if next_resume_stage else ""
        ),
        "last_failed_stage": latest_failure_stage,
        "last_failure_detail": latest_failure_detail,
        "branch_name": str(latest_started_context.get("branch_name") or ""),
        "worktree_path": str(latest_started_context.get("worktree_path") or ""),
        "pull_request_status": pull_request_status,
        "pull_request_url": pull_request_url,
        "push_status": latest_push_status,
        "push_strategy": latest_push_strategy,
        "push_error": latest_push_error,
    }
    return {
        "workflow_state": workflow_state,
        "resume_history": resume_history,
        "last_blocker": last_blocker,
        "resume_count": len(resume_history),
        "last_resume_from": last_resume_from,
    }


class TaskEventLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, step: str, status: str, message: str, **context) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "status": status,
            "message": message,
            "context": context,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


class TaskRunner:
    def __init__(
        self,
        repo_root: Path,
        runs_root: Path,
        worktrees_root: Path,
        executor: TaskExecutor,
        pull_request_publisher: PullRequestPublisher,
    ) -> None:
        self.repo_root = repo_root
        self.runs_root = runs_root
        self.worktrees_root = worktrees_root
        self.executor = executor
        self.pull_request_publisher = pull_request_publisher
        self.git = GitRepository(repo_root)

    def run(self, tasks: Iterable[TaskDefinition]) -> List[TaskResult]:
        return self.run_with_id(tasks)

    def run_with_id(
        self,
        tasks: Iterable[TaskDefinition],
        run_id: Optional[str] = None,
    ) -> List[TaskResult]:
        run_id = run_id or create_run_id()
        run_root = self.runs_root / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

        results = []
        for task in tasks:
            results.append(self._run_task(task, run_root))
        self._write_summary(run_root, results)
        return results

    def resume_task_with_id(
        self,
        task: TaskDefinition,
        run_id: str,
        resume_from: str,
    ) -> TaskResult:
        if resume_from not in {"branch", "code", "tests", "pull_request"}:
            raise ValueError(f"Unsupported resume stage `{resume_from}`.")

        run_root = self.runs_root / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

        result = self._resume_task(task, run_root, resume_from=resume_from)
        self._write_summary(run_root, [result], merge_existing=True)
        return result

    def _run_task(
        self,
        task: TaskDefinition,
        run_root: Path,
    ) -> TaskResult:
        task_slug = _slugify(task.id)
        task_root = run_root / "tasks" / task_slug
        task_root.mkdir(parents=True, exist_ok=True)
        logger = TaskEventLogger(task_root / "events.jsonl")

        branch_name = task_branch_name(task.id)
        worktree_path = self.worktrees_root / task_worktree_name(task.id)
        summary_path = task_root / "task-summary.json"

        logger.log(
            "task",
            "started",
            "Starting task processing.",
            task_id=task.id,
            title=task.title,
            branch_name=branch_name,
            base_branch=task.base_branch,
            worktree_path=str(worktree_path),
            working_directory=task.working_directory,
        )
        logger.log(
            "agent_execution",
            "started",
            "The automation runner started processing this task.",
            task_id=task.id,
        )

        attempts = 0
        feedback = ""
        status = "failed"
        pull_request_status = "skipped"
        pull_request_url = ""
        worktree_created = False

        try:
            worktree_path = self._prepare_task_worktree(
                task=task,
                branch_name=branch_name,
                worktree_path=worktree_path,
                logger=logger,
            )
            worktree_created = True
            for attempt in range(1, task.retry_limit + 2):
                attempts = attempt
                logger.log(
                    "attempt",
                    "started",
                    "Starting executor and validation attempt.",
                    attempt=attempt,
                )
                logger.log(
                    "code",
                    "started",
                    "Starting code generation and application step.",
                    attempt=attempt,
                )
                try:
                    artifact_path = self.executor.execute(
                        task=task,
                        attempt=attempt,
                        task_root=task_root,
                        worktree_path=worktree_path,
                        feedback=feedback,
                        logger=logger,
                    )
                except Exception as error:
                    logger.log(
                        "code",
                        "failed",
                        "Code generation and application step failed.",
                        attempt=attempt,
                        error=str(error),
                    )
                    raise
                logger.log(
                    "code",
                    "completed",
                    "Completed code generation and application step.",
                    attempt=attempt,
                    artifact_path=str(artifact_path),
                )
                commit_sha = self._commit_attempt_changes(task, attempt, worktree_path, logger)
                if not commit_sha:
                    feedback = (
                        "The executor finished without creating any git changes in the task worktree."
                    )
                    logger.log(
                        "code",
                        "failed",
                        "Code generation produced no git changes.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                    if attempt <= task.retry_limit:
                        logger.log(
                            "attempt",
                            "retrying",
                            "No changes were produced. Preparing another attempt.",
                            attempt=attempt,
                            feedback=feedback,
                        )
                        continue
                    logger.log(
                        "task",
                        "failed",
                        "Code generation never produced changes and retry budget is exhausted.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                    logger.log(
                        "agent_execution",
                        "failed",
                        "The automation runner exhausted the retry budget without producing changes.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                    break
                completed = self._run_tests(task, attempt, task_root, worktree_path, logger)
                if completed.returncode == 0:
                    status = "passed"
                    logger.log(
                        "task",
                        "passed",
                        "Validation passed for the task."
                        if task.test_command
                        else "No validation command configured; task passed with tests skipped.",
                        attempt=attempt,
                    )
                    logger.log(
                        "agent_execution",
                        "completed",
                        "The automation runner finished this task successfully.",
                        attempt=attempt,
                    )
                    break

                feedback = completed.stderr.strip() or completed.stdout.strip() or (
                    "Tests failed without any stdout or stderr output."
                )
                if attempt <= task.retry_limit:
                    logger.log(
                        "attempt",
                        "retrying",
                        "Validation failed. Preparing another attempt.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                else:
                    logger.log(
                        "task",
                        "failed",
                        "Validation failed and retry budget is exhausted.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                    logger.log(
                        "agent_execution",
                        "failed",
                        "The automation runner exhausted the retry budget for this task.",
                        attempt=attempt,
                        feedback=feedback,
                    )

            if status == "passed":
                pull_request_status, pull_request_url = self._publish_pull_request(
                    task=task,
                    branch_name=branch_name,
                    worktree_path=worktree_path,
                    logger=logger,
                )
        except Exception as error:
            if not worktree_created:
                logger.log(
                    "branch",
                    "failed",
                    "Failed to create task branch and worktree.",
                    branch_name=branch_name,
                    error=str(error),
                )
            logger.log(
                "task",
                "error",
                "Task processing raised an unexpected error.",
                error=str(error),
            )
            logger.log(
                "agent_execution",
                "failed",
                "The automation runner stopped because of an unexpected error.",
                error=str(error),
            )
            status = "failed"
        finally:
            if worktree_created:
                logger.log(
                    "cleanup",
                    "skipped",
                    "Keeping the task worktree for inspection.",
                    worktree_path=str(worktree_path),
                )

        return self._finalize_task_result(
            task=task,
            task_root=task_root,
            summary_path=summary_path,
            branch_name=branch_name,
            attempts=attempts,
            logger=logger,
            status=status,
            pull_request_status=pull_request_status,
            pull_request_url=pull_request_url,
        )

    def _resume_task(
        self,
        task: TaskDefinition,
        run_root: Path,
        resume_from: str,
    ) -> TaskResult:
        task_slug = _slugify(task.id)
        task_root = run_root / "tasks" / task_slug
        task_root.mkdir(parents=True, exist_ok=True)
        logger = TaskEventLogger(task_root / "events.jsonl")

        branch_name = task_branch_name(task.id)
        worktree_path = self.worktrees_root / task_worktree_name(task.id)
        summary_path = task_root / "task-summary.json"
        previous_summary = _load_json(summary_path)
        previous_events = _load_jsonl(logger.path)
        previous_attempts = _latest_attempt(previous_events)

        attempts = previous_summary.get("attempts", previous_attempts)
        feedback = self._resume_feedback(previous_events)
        status = previous_summary.get("status", "failed")
        pull_request_status = previous_summary.get("pull_request_status", "skipped")
        pull_request_url = previous_summary.get("pull_request_url", "")
        worktree_created = False
        resume_label = self._resume_stage_label(resume_from)

        logger.log(
            "task",
            "started",
            f"Resuming task processing from {resume_label}.",
            task_id=task.id,
            title=task.title,
            branch_name=branch_name,
            base_branch=task.base_branch,
            worktree_path=str(worktree_path),
            working_directory=task.working_directory,
            resume_from=resume_from,
        )
        logger.log(
            "agent_execution",
            "started",
            "The automation runner resumed this task from the last incomplete stage.",
            task_id=task.id,
            resume_from=resume_from,
        )

        try:
            worktree_path = self._prepare_task_worktree(
                task=task,
                branch_name=branch_name,
                worktree_path=worktree_path,
                logger=logger,
                resume_from=resume_from,
            )
            worktree_created = True

            if resume_from in {"branch", "code"}:
                attempt = previous_attempts + 1 if previous_attempts else 1
                attempts = attempt
                logger.log(
                    "attempt",
                    "started",
                    "Resuming executor and validation attempt.",
                    attempt=attempt,
                    resume_from=resume_from,
                )
                logger.log(
                    "code",
                    "started",
                    "Resuming code generation and application step.",
                    attempt=attempt,
                    resume_from=resume_from,
                )
                try:
                    artifact_path = self.executor.execute(
                        task=task,
                        attempt=attempt,
                        task_root=task_root,
                        worktree_path=worktree_path,
                        feedback=feedback,
                        logger=logger,
                    )
                except Exception as error:
                    logger.log(
                        "code",
                        "failed",
                        "Code generation and application step failed while resuming the task.",
                        attempt=attempt,
                        error=str(error),
                    )
                    raise
                logger.log(
                    "code",
                    "completed",
                    "Completed code generation and application step while resuming the task.",
                    attempt=attempt,
                    artifact_path=str(artifact_path),
                )
                commit_sha = self._commit_attempt_changes(task, attempt, worktree_path, logger)
                if not commit_sha:
                    feedback = (
                        "The executor finished without creating any git changes in the task worktree."
                    )
                    logger.log(
                        "code",
                        "failed",
                        "Code generation produced no git changes while resuming the task.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                    logger.log(
                        "task",
                        "failed",
                        "Resume did not produce any new git changes to validate.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                    logger.log(
                        "agent_execution",
                        "failed",
                        "The automation runner resumed the task but no new git changes were produced.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                    status = "failed"
                else:
                    completed = self._run_tests(task, attempt, task_root, worktree_path, logger)
                    if completed.returncode == 0:
                        status = "passed"
                        logger.log(
                            "task",
                            "passed",
                            "Validation passed while resuming the task."
                            if task.test_command
                            else "No validation command configured; resume passed with tests skipped.",
                            attempt=attempt,
                        )
                        logger.log(
                            "agent_execution",
                            "completed",
                            "The automation runner resumed this task successfully.",
                            attempt=attempt,
                        )
                    else:
                        feedback = completed.stderr.strip() or completed.stdout.strip() or (
                            "Tests failed without any stdout or stderr output."
                        )
                        status = "failed"
                        logger.log(
                            "task",
                            "failed",
                            "Validation failed while resuming the task.",
                            attempt=attempt,
                            feedback=feedback,
                        )
                        logger.log(
                            "agent_execution",
                            "failed",
                            "The automation runner stopped because validation still fails after resuming the task.",
                            attempt=attempt,
                            feedback=feedback,
                        )
            elif resume_from == "tests":
                attempt = previous_attempts + 1 if previous_attempts else 1
                attempts = attempt
                logger.log(
                    "attempt",
                    "started",
                    "Resuming validation from the current task branch.",
                    attempt=attempt,
                    resume_from=resume_from,
                )
                completed = self._run_tests(task, attempt, task_root, worktree_path, logger)
                if completed.returncode == 0:
                    status = "passed"
                    logger.log(
                        "task",
                        "passed",
                        "Validation passed while resuming the task."
                        if task.test_command
                        else "No validation command configured; resume passed with tests skipped.",
                        attempt=attempt,
                    )
                    logger.log(
                        "agent_execution",
                        "completed",
                        "The automation runner resumed validation successfully.",
                        attempt=attempt,
                    )
                else:
                    feedback = completed.stderr.strip() or completed.stdout.strip() or (
                        "Tests failed without any stdout or stderr output."
                    )
                    status = "failed"
                    logger.log(
                        "task",
                        "failed",
                        "Validation failed again while resuming the task.",
                        attempt=attempt,
                        feedback=feedback,
                    )
                    logger.log(
                        "agent_execution",
                        "failed",
                        "The automation runner stopped because validation still fails after resuming the task.",
                        attempt=attempt,
                        feedback=feedback,
                    )
            elif resume_from == "pull_request":
                status = "passed"
                logger.log(
                    "task",
                    "passed",
                    "Validation already passed earlier; resuming pull request publication only.",
                    attempt=attempts,
                    resume_from=resume_from,
                )
                logger.log(
                    "agent_execution",
                    "completed",
                    "The automation runner resumed the remaining publication steps.",
                    attempt=attempts,
                    resume_from=resume_from,
                )

            if status == "passed":
                pull_request_status, pull_request_url = self._publish_pull_request(
                    task=task,
                    branch_name=branch_name,
                    worktree_path=worktree_path,
                    logger=logger,
                    resume_from=resume_from,
                )
        except Exception as error:
            if not worktree_created:
                logger.log(
                    "branch",
                    "failed",
                    "Failed to prepare the task branch and worktree while resuming the task.",
                    branch_name=branch_name,
                    error=str(error),
                    resume_from=resume_from,
                )
            logger.log(
                "task",
                "error",
                "Task resumption raised an unexpected error.",
                error=str(error),
                resume_from=resume_from,
            )
            logger.log(
                "agent_execution",
                "failed",
                "The automation runner stopped because resuming the task raised an unexpected error.",
                error=str(error),
                resume_from=resume_from,
            )
            if resume_from != "pull_request":
                status = "failed"
        finally:
            if worktree_created:
                logger.log(
                    "cleanup",
                    "skipped",
                    "Keeping the task worktree for inspection.",
                    worktree_path=str(worktree_path),
                )

        return self._finalize_task_result(
            task=task,
            task_root=task_root,
            summary_path=summary_path,
            branch_name=branch_name,
            attempts=attempts,
            logger=logger,
            status=status,
            pull_request_status=pull_request_status,
            pull_request_url=pull_request_url,
        )

    def _run_tests(
        self,
        task: TaskDefinition,
        attempt: int,
        task_root: Path,
        worktree_path: Path,
        logger: TaskEventLogger,
    ) -> subprocess.CompletedProcess:
        command = task.test_command
        working_directory = (worktree_path / task.working_directory).resolve()
        stdout_path = task_root / f"attempt-{attempt:02d}-tests.stdout.log"
        stderr_path = task_root / f"attempt-{attempt:02d}-tests.stderr.log"

        if not command:
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            logger.log(
                "tests",
                "skipped",
                "No validation command configured for the task. Skipping tests.",
                attempt=attempt,
                cwd=str(working_directory),
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
            )
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )

        logger.log(
            "tests",
            "started",
            "Running task validation command.",
            attempt=attempt,
            command=command,
            cwd=str(working_directory),
        )
        completed = subprocess.run(
            command,
            cwd=working_directory,
            capture_output=True,
            text=True,
            check=False,
        )
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        logger.log(
            "tests",
            "completed" if completed.returncode == 0 else "failed",
            "Finished task validation command.",
            attempt=attempt,
            returncode=completed.returncode,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        return completed

    def _finalize_task_result(
        self,
        task: TaskDefinition,
        task_root: Path,
        summary_path: Path,
        branch_name: str,
        attempts: int,
        logger: TaskEventLogger,
        status: str,
        pull_request_status: str,
        pull_request_url: str,
    ) -> TaskResult:
        events = _load_jsonl(logger.path)
        documentation = derive_task_documentation(
            events=events,
            status=status,
            pull_request_status=pull_request_status,
            pull_request_url=pull_request_url,
        )
        workflow_state_path = task_root / "workflow-state.json"
        resume_history_path = task_root / "resume-history.json"
        workflow_state_path.write_text(
            json.dumps(documentation["workflow_state"], indent=2),
            encoding="utf-8",
        )
        resume_history_path.write_text(
            json.dumps(documentation["resume_history"], indent=2),
            encoding="utf-8",
        )
        result = TaskResult(
            task_id=task.id,
            title=task.title,
            status=status,
            branch_name=branch_name,
            attempts=attempts,
            log_path=logger.path,
            summary_path=summary_path,
            pull_request_status=pull_request_status,
            pull_request_url=pull_request_url,
            workflow_state_path=workflow_state_path,
            resume_history_path=resume_history_path,
            workflow_state=documentation["workflow_state"],
            resume_history=documentation["resume_history"],
            last_blocker=documentation["last_blocker"],
            resume_count=int(documentation["resume_count"]),
            last_resume_from=str(documentation["last_resume_from"]),
        )
        summary_path.write_text(
            json.dumps(self._serialize_result(result), indent=2),
            encoding="utf-8",
        )
        return result

    def _commit_attempt_changes(
        self,
        task: TaskDefinition,
        attempt: int,
        worktree_path: Path,
        logger: TaskEventLogger,
    ) -> str:
        if not self.git.has_uncommitted_changes(cwd=worktree_path):
            return ""

        commit_message = self._commit_message(task, attempt)
        logger.log(
            "commit",
            "started",
            "Creating a commit for the current task changes.",
            attempt=attempt,
            commit_message=commit_message,
        )
        self.git.stage_all(worktree_path)
        commit_sha = self.git.commit_all(worktree_path, commit_message)
        logger.log(
            "commit",
            "completed",
            "Created a commit for the current task changes.",
            attempt=attempt,
            commit_message=commit_message,
            commit_sha=commit_sha,
        )
        return commit_sha

    def _prepare_task_worktree(
        self,
        task: TaskDefinition,
        branch_name: str,
        worktree_path: Path,
        logger: TaskEventLogger,
        resume_from: Optional[str] = None,
    ) -> Path:
        context = {
            "branch_name": branch_name,
            "base_branch": task.base_branch,
        }
        if resume_from:
            context["resume_from"] = resume_from
        logger.log(
            "branch",
            "started",
            (
                "Preparing the reusable task branch and worktree."
                if not resume_from
                else "Preparing the reusable task branch and worktree for resume."
            ),
            **context,
        )
        prepared_worktree = self.git.prepare_task_worktree(
            branch_name=branch_name,
            base_branch=task.base_branch,
            destination=worktree_path,
        )
        if prepared_worktree.reused_existing:
            branch_message = "Reusing the existing task branch and worktree."
        elif prepared_worktree.branch_created:
            branch_message = "Created the task branch and worktree."
        else:
            branch_message = "Attached the existing task branch to a worktree."
        if resume_from:
            branch_message = f"{branch_message.rstrip('.')} for resume."
        logger.log(
            "branch",
            "completed",
            branch_message,
            branch_name=branch_name,
            worktree_path=str(prepared_worktree.path),
            branch_created=prepared_worktree.branch_created,
            worktree_created=prepared_worktree.worktree_created,
            reused_existing=prepared_worktree.reused_existing,
            **({"resume_from": resume_from} if resume_from else {}),
        )
        return prepared_worktree.path

    def _publish_pull_request(
        self,
        task: TaskDefinition,
        branch_name: str,
        worktree_path: Path,
        logger: TaskEventLogger,
        resume_from: Optional[str] = None,
    ) -> tuple[str, str]:
        start_context = {"branch_name": branch_name}
        if resume_from:
            start_context["resume_from"] = resume_from
        logger.log(
            "pull_request",
            "started",
            "Creating pull request for the task branch.",
            **start_context,
        )
        try:
            pull_request_result = self.pull_request_publisher.publish(
                task=task,
                branch_name=branch_name,
                worktree_path=worktree_path,
                logger=logger,
            )
            pull_request_status = pull_request_result.status
            pull_request_url = pull_request_result.url
        except Exception as error:
            pull_request_status = "failed"
            pull_request_url = ""
            logger.log(
                "pull_request",
                "failed",
                "Pull request creation failed.",
                branch_name=branch_name,
                error=str(error),
                **({"resume_from": resume_from} if resume_from else {}),
            )
        logger.log(
            "review",
            "current" if pull_request_status == "passed" else "skipped",
            (
                "Waiting for human review."
                if pull_request_status == "passed"
                else (
                    "Review is skipped because no pull request was created."
                    if pull_request_status == "skipped"
                    else "Review is skipped because pull request publishing failed."
                )
            ),
            branch_name=branch_name,
            pull_request_url=pull_request_url,
            **({"resume_from": resume_from} if resume_from else {}),
        )
        return pull_request_status, pull_request_url

    def _write_summary(
        self,
        run_root: Path,
        results: List[TaskResult],
        merge_existing: bool = False,
    ) -> None:
        summary_path = run_root / "summary.json"
        entries: Dict[str, Dict[str, Any]] = {}
        if merge_existing and summary_path.exists():
            existing_entries = json.loads(summary_path.read_text(encoding="utf-8"))
            for item in existing_entries:
                if isinstance(item, dict) and item.get("task_id"):
                    entries[str(item["task_id"])] = item

        for result in results:
            entries[result.task_id] = self._serialize_result(result)

        ordered = [entries[task_id] for task_id in sorted(entries)]
        summary_path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")

    @staticmethod
    def _resume_feedback(events: List[Dict[str, Any]]) -> str:
        for event in reversed(events):
            context = event.get("context", {})
            feedback = str(context.get("feedback") or context.get("error") or "").strip()
            if feedback:
                return redact_sensitive_text(feedback)
        return ""

    @staticmethod
    def _resume_stage_label(stage: str) -> str:
        return _resume_stage_label(stage)

    @staticmethod
    def _commit_message(task: TaskDefinition, attempt: int) -> str:
        if attempt == 1:
            return f"feat({task.id}): implement {task.title}"
        return f"fix({task.id}): address validation feedback (attempt {attempt})"

    @staticmethod
    def _serialize_result(result: TaskResult) -> dict:
        data = asdict(result)
        data["log_path"] = str(result.log_path)
        data["summary_path"] = str(result.summary_path)
        data["workflow_state_path"] = str(result.workflow_state_path)
        data["resume_history_path"] = str(result.resume_history_path)
        return data
