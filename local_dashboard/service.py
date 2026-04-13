from __future__ import annotations

import base64
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from uuid import uuid4

from overnight_runner.backlog import (
    build_task_definition,
    load_backlog_document,
    save_backlog_document,
)
from overnight_runner.executors import resolve_codex_binary
from overnight_runner.git_ops import GitCommandError, GitRepository, redact_sensitive_text
from overnight_runner.runner import (
    TASK_BRANCH_PREFIX,
    create_run_id,
    derive_task_documentation,
    task_branch_name,
    task_worktree_name,
)

PIPELINE_STAGES = [
    {"id": "backlog", "label": "Backlog"},
    {"id": "agent_execution", "label": "Agent execution"},
    {"id": "branch", "label": "Branch"},
    {"id": "code", "label": "Code"},
    {"id": "tests", "label": "Tests"},
    {"id": "pull_request", "label": "PR"},
    {"id": "review", "label": "Review"},
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail_text(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) <= max_chars:
        return content
    return content[-max_chars:]


def _load_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _truncate_text(text: str, max_chars: int = 320) -> str:
    content = text.strip()
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 3].rstrip() + "..."


def _pluralize(count: int, singular: str, plural: Optional[str] = None) -> str:
    label = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {label}"


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _safe_filename(value: str) -> str:
    source_name = Path(str(value or "")).name.strip() or "attachment"
    sanitized = "".join(
        character if character.isalnum() or character in {".", "-", "_"} else "-"
        for character in source_name
    ).strip(".-_")
    return sanitized[:120] or "attachment"


def _guess_content_type(filename: str, content_type: str = "") -> str:
    normalized = str(content_type or "").strip()
    if normalized:
        return normalized
    guessed_type, _ = mimetypes.guess_type(filename)
    return guessed_type or "application/octet-stream"


def _attachment_kind(content_type: str) -> str:
    return "image" if str(content_type or "").startswith("image/") else "file"


def _infer_task_status(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "pending"

    for event in reversed(events):
        if event["step"] == "task" and event["status"] == "passed":
            return "passed"
        if event["step"] == "task" and event["status"] in {"failed", "error"}:
            return "failed"

    for event in reversed(events):
        if event["status"] in {"started", "retrying"}:
            return "running"
    return "running"


def _latest_attempt(events: List[Dict[str, Any]]) -> int:
    attempts = [
        int(event.get("context", {}).get("attempt"))
        for event in events
        if event.get("context", {}).get("attempt") is not None
    ]
    return max(attempts, default=0)


def _task_title_from_events(task_id: str, events: List[Dict[str, Any]]) -> str:
    for event in events:
        context = event.get("context", {})
        if context.get("task_id") == task_id or event.get("step") == "task":
            title = context.get("title")
            if title:
                return title
    return task_id


def _task_branch_from_events(events: List[Dict[str, Any]]) -> str:
    for event in events:
        branch_name = event.get("context", {}).get("branch_name")
        if branch_name:
            return branch_name
    return ""


def _task_log_snapshot(task_dir: Path, events: List[Dict[str, Any]]) -> Dict[str, str]:
    attempt = _latest_attempt(events)
    if attempt == 0:
        return {"stdout": "", "stderr": ""}

    stdout_path = task_dir / f"attempt-{attempt:02d}-tests.stdout.log"
    stderr_path = task_dir / f"attempt-{attempt:02d}-tests.stderr.log"
    return {
        "stdout": _tail_text(stdout_path),
        "stderr": _tail_text(stderr_path),
    }


def _new_pipeline_stages() -> List[Dict[str, str]]:
    return [
        {"id": stage["id"], "label": stage["label"], "status": "pending", "detail": ""}
        for stage in PIPELINE_STAGES
    ]


def _stage_lookup(stages: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    return {stage["id"]: stage for stage in stages}


def _latest_event(events: List[Dict[str, Any]], step: str) -> Optional[Dict[str, Any]]:
    for event in reversed(events):
        if event.get("step") == step:
            return event
    return None


def _task_latest_event(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    events = [event for event in task.get("events", []) if isinstance(event, dict)]
    latest: Optional[Dict[str, Any]] = None
    latest_timestamp: Optional[datetime] = None
    for event in events:
        event_timestamp = _parse_iso_timestamp(event.get("timestamp"))
        if latest is None:
            latest = event
            latest_timestamp = event_timestamp
            continue
        if event_timestamp and (latest_timestamp is None or event_timestamp > latest_timestamp):
            latest = event
            latest_timestamp = event_timestamp
    return latest


def _event_detail(event: Optional[Dict[str, Any]]) -> str:
    if not event:
        return ""
    context = event.get("context", {})
    detail = str(context.get("feedback") or context.get("error") or event.get("message", "")).strip()
    return redact_sensitive_text(detail)


def _summarize_run_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    tasks = detail.get("tasks", []) or []
    return {
        "run_id": str(detail.get("run_id") or ""),
        "status": str(detail.get("status") or "unknown"),
        "is_running": bool(detail.get("is_running")),
        "started_at": str(detail.get("started_at") or ""),
        "exit_code": detail.get("exit_code"),
        "task_count": len(tasks),
        "passed_count": len([task for task in tasks if task.get("status") == "passed"]),
        "failed_count": len([task for task in tasks if task.get("status") == "failed"]),
        "running_count": len([task for task in tasks if task.get("status") == "running"]),
    }


def _run_activity_snapshot(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tasks = [task for task in detail.get("tasks", []) if isinstance(task, dict)]
    candidates = [task for task in tasks if task.get("status") == "running"] or tasks

    selected_task: Optional[Dict[str, Any]] = None
    selected_event: Optional[Dict[str, Any]] = None
    selected_timestamp: Optional[datetime] = None
    fallback_timestamp = _parse_iso_timestamp(detail.get("started_at"))

    for task in candidates:
        event = _task_latest_event(task)
        event_timestamp = _parse_iso_timestamp((event or {}).get("timestamp"))
        comparison_timestamp = event_timestamp or fallback_timestamp
        if selected_task is None:
            selected_task = task
            selected_event = event
            selected_timestamp = comparison_timestamp
            continue
        if comparison_timestamp and (
            selected_timestamp is None or comparison_timestamp > selected_timestamp
        ):
            selected_task = task
            selected_event = event
            selected_timestamp = comparison_timestamp

    if selected_task is None:
        run_id = str(detail.get("run_id") or "")
        if not run_id:
            return None
        return {
            "run_id": run_id,
            "task_id": "",
            "title": f"Run {run_id}",
            "summary": (
                "The runner process is active."
                if detail.get("is_running")
                else "No task activity has been recorded for this run yet."
            ),
            "stage": "",
            "timestamp": str(detail.get("started_at") or ""),
            "status": str(detail.get("status") or ""),
        }

    pipeline = selected_task.get("pipeline", {}) or {}
    summary = str(pipeline.get("summary") or "").strip() or _event_detail(selected_event)
    if not summary:
        summary = (
            "The runner is currently processing this task."
            if detail.get("is_running")
            else "This was the latest task activity recorded for the run."
        )

    return {
        "run_id": str(detail.get("run_id") or ""),
        "task_id": str(selected_task.get("task_id") or ""),
        "title": str(selected_task.get("title") or selected_task.get("task_id") or "Task"),
        "summary": summary,
        "stage": str(pipeline.get("current_stage") or (selected_event or {}).get("step") or ""),
        "timestamp": str((selected_event or {}).get("timestamp") or detail.get("started_at") or ""),
        "status": str(selected_task.get("status") or ""),
    }


def _map_stage_status(raw_status: str) -> str:
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


def _apply_event_to_stage(stage: Dict[str, str], event: Dict[str, Any]) -> None:
    stage["status"] = _map_stage_status(event.get("status", ""))
    stage["detail"] = _event_detail(event)


def _default_pipeline_state() -> Dict[str, Any]:
    stages = _new_pipeline_stages()
    stage_map = _stage_lookup(stages)
    stage_map["backlog"]["status"] = "current"
    stage_map["backlog"]["detail"] = "Task is waiting in the backlog."
    return {
        "overall_status": "pending",
        "summary": "Waiting in Backlog",
        "current_stage": "backlog",
        "failure_stage": "",
        "failure_detail": "",
        "latest_run_id": "",
        "latest_run_status": "pending",
        "stages": stages,
    }


def _resume_label(stage: str) -> str:
    labels = {
        "branch": "Branch",
        "code": "Code",
        "tests": "Tests",
        "pull_request": "PR",
    }
    return labels.get(stage, stage)


def _task_resume_state(task: Dict[str, Any]) -> Dict[str, Any]:
    task_status = str(task.get("status", "") or "")
    pull_request_status = str(task.get("pull_request_status", "") or "")
    if task_status == "passed" and pull_request_status == "passed":
        return {
            "available": False,
            "stage": "",
            "label": "",
        }

    pipeline = task.get("pipeline", {}) or {}
    workflow_state = task.get("workflow_state", {}) or {}
    failure_stage = str(pipeline.get("failure_stage", "") or "")
    current_stage = str(pipeline.get("current_stage", "") or "")
    workflow_stage = str(workflow_state.get("next_resume_stage", "") or "")

    if workflow_stage in {"branch", "code", "tests", "pull_request"}:
        stage = workflow_stage
    elif failure_stage in {"branch", "code", "tests", "pull_request"}:
        stage = failure_stage
    elif current_stage == "pull_request" and task.get("pull_request_status") != "passed":
        stage = "pull_request"
    elif current_stage in {"branch", "code", "tests"}:
        stage = current_stage
    else:
        stage = ""

    return {
        "available": bool(stage),
        "stage": stage,
        "label": f"Resume from {_resume_label(stage)}" if stage else "",
    }


def _build_pipeline_state(task: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    events = task.get("events", [])
    task_status = task.get("status", "pending")
    stages = _new_pipeline_stages()
    stage_map = _stage_lookup(stages)

    stage_map["backlog"]["status"] = "passed"
    stage_map["backlog"]["detail"] = "Task moved out of the backlog into execution."

    if events:
        stage_map["agent_execution"]["status"] = "passed"
        stage_map["agent_execution"]["detail"] = "The automation runner picked up the task."

    agent_event = _latest_event(events, "agent_execution")
    if agent_event:
        _apply_event_to_stage(stage_map["agent_execution"], agent_event)

    branch_event = _latest_event(events, "branch")
    if branch_event:
        _apply_event_to_stage(stage_map["branch"], branch_event)
    elif _latest_event(events, "attempt") or _latest_event(events, "tests") or _latest_event(events, "executor"):
        stage_map["branch"]["status"] = "passed"
        stage_map["branch"]["detail"] = "Task branch and worktree were created."
    elif task_status == "failed":
        stage_map["branch"]["status"] = "failed"
        stage_map["branch"]["detail"] = _event_detail(_latest_event(events, "task")) or (
            "The task failed before code execution started."
        )
    elif task_status == "running":
        stage_map["branch"]["status"] = "running"
        stage_map["branch"]["detail"] = "Preparing task branch and worktree."

    code_event = _latest_event(events, "code")
    if code_event:
        _apply_event_to_stage(stage_map["code"], code_event)
    elif _latest_event(events, "executor"):
        stage_map["code"]["status"] = "passed"
        stage_map["code"]["detail"] = "Code generation and application completed."
    elif task_status == "running" and _latest_event(events, "attempt"):
        stage_map["code"]["status"] = "running"
        stage_map["code"]["detail"] = "Generating and applying code changes."
    elif (
        task_status == "failed"
        and stage_map["branch"]["status"] == "passed"
        and not _latest_event(events, "tests")
    ):
        stage_map["code"]["status"] = "failed"
        stage_map["code"]["detail"] = _event_detail(_latest_event(events, "task")) or (
            "The code step failed before tests started."
        )

    tests_event = _latest_event(events, "tests")
    if tests_event:
        _apply_event_to_stage(stage_map["tests"], tests_event)
    elif task_status == "running" and stage_map["code"]["status"] == "passed":
        stage_map["tests"]["status"] = "running"
        stage_map["tests"]["detail"] = "Running validation."

    pull_request_event = _latest_event(events, "pull_request")
    if pull_request_event:
        _apply_event_to_stage(stage_map["pull_request"], pull_request_event)
    elif task_status == "passed":
        stage_map["pull_request"]["status"] = "running"
        stage_map["pull_request"]["detail"] = "Publishing the pull request."

    review_event = _latest_event(events, "review")
    if review_event:
        _apply_event_to_stage(stage_map["review"], review_event)
    elif stage_map["pull_request"]["status"] == "passed":
        stage_map["review"]["status"] = "current"
        stage_map["review"]["detail"] = "Waiting for human review."
    elif stage_map["pull_request"]["status"] == "skipped":
        stage_map["review"]["status"] = "skipped"
        stage_map["review"]["detail"] = "No pull request was created, so review is skipped."

    failure_stage = next((stage for stage in stages if stage["status"] == "failed"), None)
    current_stage = next(
        (stage for stage in stages if stage["status"] in {"running", "current"}),
        None,
    )

    if failure_stage:
        if task_status == "passed" and failure_stage["id"] in {"pull_request", "review"}:
            overall_status = "warning"
            summary = f"Passed, but {failure_stage['label']} failed"
        else:
            overall_status = "failed"
            summary = f"Failed at {failure_stage['label']}"
        current_stage_id = failure_stage["id"]
        failure_stage_id = failure_stage["id"]
        failure_detail = failure_stage["detail"]
    elif current_stage:
        overall_status = "passed" if task_status == "passed" else (
            "running" if task_status == "running" else "pending"
        )
        summary = (
            f"Waiting for {current_stage['label']}"
            if current_stage["status"] == "current"
            else f"Running {current_stage['label']}"
        )
        current_stage_id = current_stage["id"]
        failure_stage_id = ""
        failure_detail = ""
    else:
        overall_status = "passed" if task_status == "passed" else task_status
        if stage_map["review"]["status"] == "current":
            summary = "Waiting for Review"
            current_stage_id = "review"
        elif stage_map["pull_request"]["status"] == "skipped":
            summary = "Passed through Tests"
            current_stage_id = "tests"
        else:
            summary = "Completed"
            current_stage_id = "review"
        failure_stage_id = ""
        failure_detail = ""

    return {
        "overall_status": overall_status,
        "summary": summary,
        "current_stage": current_stage_id,
        "failure_stage": failure_stage_id,
        "failure_detail": failure_detail,
        "latest_run_id": run_id,
        "latest_run_status": task_status,
        "stages": stages,
    }


@dataclass
class ActiveRun:
    run_id: str
    command: List[str]
    started_at: str
    process: subprocess.Popen
    stdout_path: Path
    stderr_path: Path
    task_ids: List[str]

    def is_running(self) -> bool:
        return self.process.poll() is None

    def exit_code(self) -> Optional[int]:
        return self.process.poll()


class DashboardService:
    def __init__(
        self,
        repo_root: Path,
        backlog_path: Path,
        runs_root: Path,
        worktrees_root: Path,
        runner_entrypoint: Optional[Path] = None,
    ) -> None:
        self.repo_root = repo_root
        self.backlog_path = backlog_path
        self.backlog_root = backlog_path.parent
        self.attachments_root = self.backlog_root / "attachments"
        self.runs_root = runs_root
        self.worktrees_root = worktrees_root
        default_runner_entrypoint = Path(__file__).resolve().parents[1] / "main.py"
        self.runner_entrypoint = (runner_entrypoint or default_runner_entrypoint).resolve()
        self.git_repo = GitRepository(repo_root)
        self._active_runs: Dict[str, ActiveRun] = {}
        self._lock = threading.Lock()

    def _development_flow_guard(self) -> Optional[str]:
        current_branch = self.git_repo.current_branch()
        if current_branch != "main":
            return (
                "Development flow can only start from the `main` branch. "
                f"Current branch: `{current_branch}`."
            )
        if self.git_repo.has_uncommitted_changes():
            return (
                "Development flow can only start with a clean working tree. "
                "Commit or stash your local changes first."
            )
        return None

    def _runner_entrypoint_guard(self) -> Optional[str]:
        if self.runner_entrypoint.exists():
            return None
        return (
            "Automation runner entrypoint is missing at "
            f"`{self.runner_entrypoint}`. Restart the dashboard with "
            "`--runner-entrypoint /absolute/path/to/main.py`."
        )

    def _branch_cleanup_guard(self) -> Optional[str]:
        with self._lock:
            if any(run.is_running() for run in self._active_runs.values()):
                return "Wait for active runs to finish before removing merged branches."
        return None

    def _clear_runs_guard(self) -> Optional[str]:
        with self._lock:
            if any(run.is_running() for run in self._active_runs.values()):
                return "Wait for active runs to finish before clearing runs."
        return None

    def _resume_guard(self, run_id: str, task_id: str) -> Optional[str]:
        with self._lock:
            for active_run in self._active_runs.values():
                if not active_run.is_running():
                    continue
                if active_run.run_id == run_id:
                    return "Wait for the current run to finish before resuming this task."
                if not active_run.task_ids or task_id in active_run.task_ids:
                    return (
                        f"Task `{task_id}` is already being processed by run "
                        f"`{active_run.run_id}`."
                    )
        return None

    def _merged_task_branch_names(self, base_branch: str = "main") -> List[str]:
        try:
            current_branch = self.git_repo.current_branch()
            branch_names = self.git_repo.list_merged_branches(base_branch)
        except GitCommandError:
            return []
        return [
            branch_name
            for branch_name in branch_names
            if branch_name.startswith(f"{TASK_BRANCH_PREFIX}/")
            and branch_name not in {base_branch, current_branch}
        ]

    def get_overview(self) -> Dict[str, Any]:
        with self._lock:
            active_run_ids = sorted(
                [run_id for run_id, run in self._active_runs.items() if run.is_running()],
                reverse=True,
            )
        active_run_count = len(active_run_ids)
        codex_cli_path = resolve_codex_binary() or ""
        github_token_available = bool(os.environ.get("GITHUB_TOKEN", "").strip())
        codex_cli_available = bool(codex_cli_path)
        current_branch = self.git_repo.current_branch()
        development_flow_error = self._development_flow_guard()
        runner_entrypoint_error = self._runner_entrypoint_guard()
        branch_cleanup_error = self._branch_cleanup_guard()
        merged_task_branch_names = self._merged_task_branch_names()
        latest_run_ids = self._list_run_ids(limit=1)
        run_details = {
            run_id: self.get_run(run_id)
            for run_id in sorted(set(active_run_ids + latest_run_ids), reverse=True)
        }
        active_runs = [
            _summarize_run_detail(run_details[run_id])
            for run_id in active_run_ids
            if run_id in run_details
        ]
        active_task_count = sum(run["task_count"] for run in active_runs)
        running_task_count = sum(run["running_count"] for run in active_runs)
        failed_task_count = sum(run["failed_count"] for run in active_runs)

        latest_run = None
        latest_detail = None
        if latest_run_ids:
            latest_detail = run_details.get(latest_run_ids[0])
            if latest_detail:
                latest_run = _summarize_run_detail(latest_detail)

        activity_candidates = [
            activity
            for activity in (_run_activity_snapshot(run_details[run_id]) for run_id in active_run_ids)
            if activity
        ]
        current_activity = None
        if activity_candidates:
            current_activity = max(
                activity_candidates,
                key=lambda item: _parse_iso_timestamp(item.get("timestamp"))
                or datetime.min.replace(tzinfo=timezone.utc),
            )
        elif latest_detail:
            current_activity = _run_activity_snapshot(latest_detail)

        launch_blockers = []
        if development_flow_error:
            launch_blockers.append(development_flow_error)
        if runner_entrypoint_error:
            launch_blockers.append(runner_entrypoint_error)
        if not github_token_available:
            launch_blockers.append(
                "The dashboard process does not currently have GITHUB_TOKEN loaded."
            )
        if not codex_cli_available:
            launch_blockers.append(
                "Codex CLI is not available on PATH for the dashboard process."
            )
        runner_launch_ready = not launch_blockers

        if active_run_count:
            runner_state = "running"
            runner_summary = (
                f"{_pluralize(active_run_count, 'active run')} across "
                f"{_pluralize(active_task_count, 'task')}."
            )
        elif latest_run and latest_run["status"] in {"failed", "warning"}:
            runner_state = "attention"
            runner_summary = (
                f"No active runs. Latest run {latest_run['run_id']} finished with "
                f"{latest_run['status']}."
            )
        elif runner_launch_ready:
            runner_state = "idle"
            runner_summary = (
                "No active runs. The runner is idle and ready to start the next backlog task."
            )
        else:
            runner_state = "blocked"
            runner_summary = (
                "No active runs. New launches are blocked until the environment checks pass."
            )

        return {
            "repo_root": str(self.repo_root),
            "backlog_path": str(self.backlog_path),
            "runs_root": str(self.runs_root),
            "worktrees_root": str(self.worktrees_root),
            "observed_at": _utc_now(),
            "dashboard_accessible": True,
            "current_branch": current_branch,
            "dirty_worktree": self.git_repo.has_uncommitted_changes(),
            "development_flow_allowed": not bool(development_flow_error),
            "development_flow_error": development_flow_error or "",
            "github_token_available": github_token_available,
            "codex_cli_available": codex_cli_available,
            "codex_cli_path": codex_cli_path,
            "active_run_count": active_run_count,
            "active_task_count": active_task_count,
            "running_task_count": running_task_count,
            "failed_task_count": failed_task_count,
            "active_runs": active_runs,
            "latest_run": latest_run,
            "current_activity": current_activity or {},
            "runner_state": runner_state,
            "runner_summary": runner_summary,
            "runner_launch_ready": runner_launch_ready,
            "runner_launch_blockers": launch_blockers,
            "quota_telemetry_available": False,
            "quota_telemetry_summary": (
                "Rate limits and remaining credits are not exposed by the local runner "
                "or the Codex CLI."
            ),
            "merged_task_branch_count": len(merged_task_branch_names),
            "branch_cleanup_allowed": not bool(branch_cleanup_error),
            "branch_cleanup_error": branch_cleanup_error or "",
        }

    def _task_attachment_dir(self, task_id: str) -> Path:
        return self.attachments_root / task_worktree_name(task_id)

    def _prune_attachment_dirs(self, task_id: str) -> None:
        attachment_dir = self._task_attachment_dir(task_id)
        if attachment_dir.exists():
            try:
                attachment_dir.rmdir()
            except OSError:
                return
        if self.attachments_root.exists():
            try:
                self.attachments_root.rmdir()
            except OSError:
                return

    def _resolve_attachment_path(self, attachment: Dict[str, Any]) -> Path:
        relative_path = str(attachment.get("relative_path", "")).strip()
        if not relative_path:
            raise ValueError("Attachment metadata is missing its relative path.")
        attachment_path = (self.backlog_root / relative_path).resolve()
        attachments_root = self.attachments_root.resolve()
        if attachment_path != attachments_root and attachments_root not in attachment_path.parents:
            raise ValueError("Attachment path escapes the backlog attachments directory.")
        return attachment_path

    def _serialize_attachment(self, task_id: str, attachment: Dict[str, Any]) -> Dict[str, Any]:
        attachment_path = self._resolve_attachment_path(attachment)
        content_type = _guess_content_type(
            str(attachment.get("name") or attachment_path.name),
            str(attachment.get("content_type") or ""),
        )
        size_bytes = int(
            attachment.get("size_bytes")
            or (attachment_path.stat().st_size if attachment_path.exists() else 0)
        )
        return {
            "id": str(attachment.get("id") or ""),
            "name": str(attachment.get("name") or attachment_path.name),
            "relative_path": str(attachment.get("relative_path") or ""),
            "content_type": content_type,
            "size_bytes": size_bytes,
            "kind": _attachment_kind(content_type),
            "url": (
                f"/api/tasks/{quote(task_id, safe='')}/attachments/"
                f"{quote(str(attachment.get('id') or ''), safe='')}/content"
            ),
        }

    def _serialize_task_attachments(
        self,
        task_id: str,
        attachments: Any,
    ) -> List[Dict[str, Any]]:
        if not isinstance(attachments, list):
            return []
        serialized = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            try:
                serialized.append(self._serialize_attachment(task_id, attachment))
            except ValueError:
                continue
        return serialized

    @staticmethod
    def _decode_new_attachment_bytes(payload: Dict[str, Any]) -> bytes:
        data_base64 = str(payload.get("data_base64") or "").strip()
        if not data_base64:
            raise ValueError("Each new attachment must include base64-encoded content.")
        try:
            return base64.b64decode(data_base64, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("One of the attachments could not be decoded.") from error

    def _store_new_attachment(
        self,
        task_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        attachment_id = uuid4().hex[:12]
        filename = _safe_filename(str(payload.get("name") or "attachment"))
        content = self._decode_new_attachment_bytes(payload)
        content_type = _guess_content_type(filename, str(payload.get("content_type") or ""))
        attachment_dir = self._task_attachment_dir(task_id)
        attachment_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{attachment_id}-{filename}"
        attachment_path = attachment_dir / stored_name
        attachment_path.write_bytes(content)
        return {
            "id": attachment_id,
            "name": filename,
            "relative_path": attachment_path.relative_to(self.backlog_root).as_posix(),
            "content_type": content_type,
            "size_bytes": len(content),
        }

    def _delete_task_attachment_files(self, attachments: Any) -> None:
        if not isinstance(attachments, list):
            return
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            try:
                attachment_path = self._resolve_attachment_path(attachment)
            except ValueError:
                continue
            if attachment_path.exists():
                attachment_path.unlink()

    def _reconcile_task_attachments(
        self,
        task_id: str,
        payload: Dict[str, Any],
        existing_task: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        existing_attachments = existing_task.get("attachments", []) if existing_task else []
        existing_by_id = {
            str(attachment.get("id")): attachment
            for attachment in existing_attachments
            if isinstance(attachment, dict) and attachment.get("id")
        }

        raw_kept_ids = payload.get("kept_attachment_ids")
        kept_attachment_ids = (
            [str(item).strip() for item in raw_kept_ids if str(item).strip()]
            if isinstance(raw_kept_ids, list)
            else list(existing_by_id.keys())
        )

        attachments: List[Dict[str, Any]] = []
        seen_attachment_ids = set()
        for attachment_id in kept_attachment_ids:
            attachment = existing_by_id.get(attachment_id)
            if not attachment or attachment_id in seen_attachment_ids:
                continue
            attachments.append(attachment)
            seen_attachment_ids.add(attachment_id)

        raw_new_attachments = payload.get("new_attachments", [])
        if raw_new_attachments not in (None, "") and not isinstance(raw_new_attachments, list):
            raise ValueError("New attachments must be provided as a list.")
        for attachment_payload in raw_new_attachments or []:
            if not isinstance(attachment_payload, dict):
                raise ValueError("Each new attachment must be an object.")
            attachments.append(self._store_new_attachment(task_id, attachment_payload))

        removed_attachment_ids = set(existing_by_id) - seen_attachment_ids
        if removed_attachment_ids:
            self._delete_task_attachment_files(
                [existing_by_id[attachment_id] for attachment_id in sorted(removed_attachment_ids)]
            )
        if not attachments:
            self._prune_attachment_dirs(task_id)
        return attachments

    def read_task_attachment(self, task_id: str, attachment_id: str) -> Dict[str, Any]:
        document = load_backlog_document(self.backlog_path)
        task = next(
            (item for item in document.get("tasks", []) if item.get("id") == task_id),
            None,
        )
        if task is None:
            raise ValueError(f"Unknown task id: {task_id}")
        attachment = next(
            (
                item
                for item in task.get("attachments", [])
                if isinstance(item, dict) and item.get("id") == attachment_id
            ),
            None,
        )
        if attachment is None:
            raise ValueError(f"Unknown attachment id `{attachment_id}` for task `{task_id}`.")

        attachment_path = self._resolve_attachment_path(attachment)
        if not attachment_path.is_file():
            raise ValueError(
                f"Attachment `{attachment_id}` for task `{task_id}` is missing from disk."
            )
        return {
            "body": attachment_path.read_bytes(),
            "content_type": _guess_content_type(
                str(attachment.get("name") or attachment_path.name),
                str(attachment.get("content_type") or ""),
            ),
            "filename": str(attachment.get("name") or attachment_path.name),
        }

    def get_backlog(self) -> Dict[str, Any]:
        document = load_backlog_document(self.backlog_path)
        latest_task_runs = self._latest_task_runs()
        if self._auto_mark_tasks_reviewed(document, latest_task_runs):
            save_backlog_document(self.backlog_path, document)
        enriched_tasks = []
        for raw_task in document.get("tasks", []):
            task = dict(raw_task)
            task["reviewed"] = bool(task.get("reviewed", False))
            task["attachments"] = self._serialize_task_attachments(
                task["id"],
                task.get("attachments", []),
            )
            task["branch"] = self._task_branch_snapshot(task["id"])
            task["branch_name"] = task["branch"]["name"]
            latest_run = latest_task_runs.get(task["id"])
            task["pipeline"] = (
                _build_pipeline_state(latest_run["task"], latest_run["run_id"])
                if latest_run
                else _default_pipeline_state()
            )
            enriched_tasks.append(task)
        document["tasks"] = enriched_tasks
        return document

    def upsert_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        document = load_backlog_document(self.backlog_path)
        existing = document.get("tasks", [])
        requested_task_id = str(payload.get("id", "")).strip()
        existing_task = next(
            (task for task in existing if task.get("id") == requested_task_id),
            None,
        )
        raw_task = self._normalize_task_payload(
            payload,
            document.get("defaults", {}),
            existing_task=existing_task,
            backlog_root=self.backlog_root,
        )
        attachments = self._reconcile_task_attachments(
            task_id=raw_task["id"],
            payload=payload,
            existing_task=existing_task,
        )
        if attachments:
            raw_task["attachments"] = attachments
        else:
            raw_task.pop("attachments", None)
        filtered = [task for task in existing if task.get("id") != raw_task["id"]]
        filtered.append(raw_task)
        filtered.sort(key=lambda task: task["id"])
        document["tasks"] = filtered
        save_backlog_document(self.backlog_path, document)
        return {
            **raw_task,
            "attachments": self._serialize_task_attachments(raw_task["id"], attachments),
        }

    def set_task_reviewed(self, task_id: str, reviewed: bool) -> Dict[str, Any]:
        document = load_backlog_document(self.backlog_path)
        tasks = document.get("tasks", [])

        updated_task: Optional[Dict[str, Any]] = None
        for task in tasks:
            if task.get("id") != task_id:
                continue
            if reviewed:
                task["reviewed"] = True
            else:
                task.pop("reviewed", None)
            updated_task = task
            break

        if updated_task is None:
            raise ValueError(f"Unknown task id: {task_id}")

        save_backlog_document(self.backlog_path, document)
        updated_task = dict(updated_task)
        updated_task["reviewed"] = reviewed
        return updated_task

    def delete_task(self, task_id: str) -> None:
        document = load_backlog_document(self.backlog_path)
        task_to_delete = next(
            (task for task in document.get("tasks", []) if task.get("id") == task_id),
            None,
        )
        document["tasks"] = [
            task for task in document.get("tasks", []) if task.get("id") != task_id
        ]
        save_backlog_document(self.backlog_path, document)
        if task_to_delete is not None:
            self._delete_task_attachment_files(task_to_delete.get("attachments", []))
            self._prune_attachment_dirs(task_id)

    def start_run(
        self,
        task_ids: Optional[List[str]] = None,
        max_tasks: Optional[int] = None,
    ) -> Dict[str, Any]:
        development_flow_error = self._development_flow_guard()
        if development_flow_error:
            raise ValueError(development_flow_error)

        runner_entrypoint_error = self._runner_entrypoint_guard()
        if runner_entrypoint_error:
            raise ValueError(runner_entrypoint_error)

        if not os.environ.get("GITHUB_TOKEN", "").strip():
            raise ValueError(
                "The dashboard process does not have GITHUB_TOKEN in its environment. "
                "Export it and restart `python3 dashboard.py` before launching a run."
            )

        backlog = self.get_backlog()
        active_tasks = [task for task in backlog.get("tasks", []) if not task.get("reviewed")]
        known_task_ids = {task["id"] for task in active_tasks}
        selected_task_ids = task_ids or []
        missing = sorted(set(selected_task_ids) - known_task_ids)
        if missing:
            raise ValueError(f"Unknown task ids: {', '.join(missing)}")
        if not known_task_ids:
            raise ValueError(
                "No backlog tasks are ready to run. Move a task back from Done or create a new one."
            )

        run_id = create_run_id()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        run_root = self.runs_root / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        stdout_path = run_root / "runner.stdout.log"
        stderr_path = run_root / "runner.stderr.log"

        command = [
            sys.executable,
            str(self.runner_entrypoint),
            "--repo-root",
            str(self.repo_root),
            "--backlog",
            str(self.backlog_path),
            "--runs-root",
            str(self.runs_root),
            "--worktrees-root",
            str(self.worktrees_root),
            "--run-id",
            run_id,
        ]
        for task_id in selected_task_ids:
            command.extend(["--task-id", task_id])
        if max_tasks is not None:
            command.extend(["--max-tasks", str(max_tasks)])

        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=self.repo_root,
                env=os.environ.copy(),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()

        active_run = ActiveRun(
            run_id=run_id,
            command=command,
            started_at=_utc_now(),
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            task_ids=selected_task_ids,
        )
        with self._lock:
            self._active_runs[run_id] = active_run

        return self.get_run(run_id)

    def cleanup_merged_task_branches(self, base_branch: str = "main") -> Dict[str, Any]:
        branch_cleanup_error = self._branch_cleanup_guard()
        if branch_cleanup_error:
            raise ValueError(branch_cleanup_error)

        deleted_branches: List[str] = []
        removed_worktrees: List[str] = []
        skipped: List[Dict[str, str]] = []

        for branch_name in self._merged_task_branch_names(base_branch):
            worktree = self.git_repo.find_worktree_by_branch(branch_name)
            if worktree and self.git_repo.has_uncommitted_changes(cwd=worktree.path):
                skipped.append(
                    {
                        "branch_name": branch_name,
                        "reason": "Skipped because the task worktree has uncommitted changes.",
                        "worktree_path": str(worktree.path),
                    }
                )
                continue

            try:
                if worktree:
                    self.git_repo.remove_worktree(worktree.path, force=False)
                    removed_worktrees.append(str(worktree.path))
                self.git_repo.delete_branch(branch_name)
                deleted_branches.append(branch_name)
            except GitCommandError as error:
                skipped.append(
                    {
                        "branch_name": branch_name,
                        "reason": str(error),
                        "worktree_path": str(worktree.path) if worktree else "",
                    }
                )

        return {
            "base_branch": base_branch,
            "deleted_branches": deleted_branches,
            "removed_worktrees": removed_worktrees,
            "skipped": skipped,
        }

    def clear_runs(self) -> Dict[str, Any]:
        clear_runs_error = self._clear_runs_guard()
        if clear_runs_error:
            raise ValueError(clear_runs_error)

        deleted_run_ids: List[str] = []
        if self.runs_root.exists():
            for path in sorted(self.runs_root.iterdir()):
                if not path.is_dir():
                    continue
                shutil.rmtree(path)
                deleted_run_ids.append(path.name)

        with self._lock:
            for run_id in deleted_run_ids:
                self._active_runs.pop(run_id, None)
            finished_run_ids = [
                run_id for run_id, run in self._active_runs.items() if not run.is_running()
            ]
            for run_id in finished_run_ids:
                self._active_runs.pop(run_id, None)

        return {
            "deleted_run_count": len(deleted_run_ids),
            "deleted_run_ids": deleted_run_ids,
        }

    def resume_run_task(
        self,
        run_id: str,
        task_id: str,
    ) -> Dict[str, Any]:
        if not os.environ.get("GITHUB_TOKEN", "").strip():
            raise ValueError(
                "The dashboard process does not have GITHUB_TOKEN in its environment. "
                "Export it and restart `python3 dashboard.py` before resuming a task."
            )

        runner_entrypoint_error = self._runner_entrypoint_guard()
        if runner_entrypoint_error:
            raise ValueError(runner_entrypoint_error)

        resume_error = self._resume_guard(run_id, task_id)
        if resume_error:
            raise ValueError(resume_error)

        backlog = self.get_backlog()
        selected_task = next(
            (task for task in backlog.get("tasks", []) if task.get("id") == task_id),
            None,
        )
        if not selected_task:
            raise ValueError(f"Unknown task id: {task_id}")

        run = self.get_run(run_id)
        if run["is_running"]:
            raise ValueError("Wait for the current run to finish before resuming this task.")

        selected_run_task = next(
            (task for task in run.get("tasks", []) if task.get("task_id") == task_id),
            None,
        )
        if not selected_run_task:
            raise ValueError(f"Run `{run_id}` does not contain task `{task_id}`.")

        resume = selected_run_task.get("resume") or _task_resume_state(selected_run_task)
        resume_stage = str(resume.get("stage") or "")
        if not resume_stage:
            raise ValueError(
                f"Task `{task_id}` does not have any pending automated work to resume."
            )

        run_root = self.runs_root / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        task_root = run_root / "tasks" / task_worktree_name(task_id)
        task_root.mkdir(parents=True, exist_ok=True)
        stdout_path = run_root / "runner.stdout.log"
        stderr_path = run_root / "runner.stderr.log"
        request_timestamp = _utc_now()
        last_blocker = selected_run_task.get("last_blocker") or {}
        workflow_state = selected_run_task.get("workflow_state") or {}
        resume_request = {
            "requested_at": request_timestamp,
            "run_id": run_id,
            "task_id": task_id,
            "resume_stage": resume_stage,
            "resume_label": resume.get("label") or "",
            "branch_name": selected_run_task.get("branch_name") or "",
            "task_status": selected_run_task.get("status") or "",
            "pull_request_status": selected_run_task.get("pull_request_status") or "",
            "last_blocker": last_blocker,
            "workflow_state": workflow_state,
        }
        _append_jsonl(task_root / "resume-requests.jsonl", resume_request)
        command = [
            sys.executable,
            str(self.runner_entrypoint),
            "--repo-root",
            str(self.repo_root),
            "--backlog",
            str(self.backlog_path),
            "--runs-root",
            str(self.runs_root),
            "--worktrees-root",
            str(self.worktrees_root),
            "--run-id",
            run_id,
            "--resume-run-id",
            run_id,
            "--resume-from",
            resume_stage,
            "--task-id",
            task_id,
        ]

        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        failure_stage = str(last_blocker.get("stage") or "")
        failure_detail = _truncate_text(
            redact_sensitive_text(str(last_blocker.get("detail") or ""))
        )
        resume_banner = (
            "\n"
            f"RESUME task_id={task_id} "
            f"stage={resume_stage} "
            f"requested_at={request_timestamp} "
            f"branch={selected_run_task.get('branch_name') or ''} "
            f"previous_status={selected_run_task.get('status') or ''} "
            f"previous_failure_stage={failure_stage or 'none'} "
            f"previous_failure_detail={failure_detail or 'none'}\n"
        )
        stdout_handle.write(resume_banner)
        stderr_handle.write(resume_banner)
        stdout_handle.flush()
        stderr_handle.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=self.repo_root,
                env=os.environ.copy(),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()

        active_run = ActiveRun(
            run_id=run_id,
            command=command,
            started_at=_utc_now(),
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            task_ids=[task_id],
        )
        with self._lock:
            self._active_runs[run_id] = active_run

        return self.get_run(run_id)

    def list_runs(self, limit: int = 20) -> Dict[str, Any]:
        run_ids = {path.name for path in self.runs_root.iterdir() if path.is_dir()} if self.runs_root.exists() else set()
        with self._lock:
            run_ids.update(self._active_runs.keys())

        ordered = sorted(run_ids, reverse=True)[:limit]
        runs = [self._build_run_summary(run_id) for run_id in ordered]
        return {"runs": runs}

    def get_run(self, run_id: str) -> Dict[str, Any]:
        run_root = self.runs_root / run_id
        summary = _load_json(run_root / "summary.json") or []
        task_summaries = {
            item["task_id"]: item for item in summary if isinstance(item, dict) and item.get("task_id")
        }

        tasks_dir = run_root / "tasks"
        tasks = []
        if tasks_dir.exists():
            for task_dir in sorted(path for path in tasks_dir.iterdir() if path.is_dir()):
                task_summary = _load_json(task_dir / "task-summary.json")
                events = _load_jsonl(task_dir / "events.jsonl")
                summary_defaults = task_summary or {}
                task_id = task_summary["task_id"] if task_summary else task_dir.name
                merged_summary = task_summaries.get(task_id, task_summary or {})
                documentation = derive_task_documentation(
                    events=events,
                    status=str(merged_summary.get("status") or summary_defaults.get("status") or _infer_task_status(events)),
                    pull_request_status=str(
                        merged_summary.get("pull_request_status")
                        or summary_defaults.get("pull_request_status")
                        or "skipped"
                    ),
                    pull_request_url=str(
                        merged_summary.get("pull_request_url")
                        or summary_defaults.get("pull_request_url")
                        or ""
                    ),
                )
                resume_request_log_path = task_dir / "resume-requests.jsonl"
                task = {
                    "task_id": task_id,
                    "title": merged_summary.get("title") or _task_title_from_events(task_id, events),
                    "status": merged_summary.get("status") or _infer_task_status(events),
                    "branch_name": merged_summary.get("branch_name") or _task_branch_from_events(events),
                    "attempts": merged_summary.get("attempts", _latest_attempt(events)),
                    "pull_request_status": merged_summary.get("pull_request_status", "skipped"),
                    "pull_request_url": merged_summary.get("pull_request_url", ""),
                    "workflow_state": merged_summary.get("workflow_state") or documentation["workflow_state"],
                    "resume_history": merged_summary.get("resume_history") or documentation["resume_history"],
                    "last_blocker": merged_summary.get("last_blocker") or documentation["last_blocker"],
                    "resume_count": merged_summary.get("resume_count", documentation["resume_count"]),
                    "last_resume_from": merged_summary.get(
                        "last_resume_from",
                        documentation["last_resume_from"],
                    ),
                    "workflow_state_path": merged_summary.get("workflow_state_path", ""),
                    "resume_history_path": merged_summary.get("resume_history_path", ""),
                    "resume_requests": _load_jsonl(resume_request_log_path)[-10:],
                    "resume_request_log_path": str(resume_request_log_path)
                    if resume_request_log_path.exists()
                    else "",
                    "events": events[-20:],
                    "logs": _task_log_snapshot(task_dir, events),
                    "task_root": str(task_dir),
                }
                task["pipeline"] = _build_pipeline_state(task, run_id)
                task["resume"] = _task_resume_state(task)
                tasks.append(task)

        runner_stdout_path = run_root / "runner.stdout.log"
        runner_stderr_path = run_root / "runner.stderr.log"
        with self._lock:
            active_run = self._active_runs.get(run_id)
        is_running = active_run.is_running() if active_run else False
        exit_code = active_run.exit_code() if active_run else None

        if is_running:
            status = "running"
        elif tasks:
            all_tasks_passed = all(task["status"] == "passed" for task in tasks)
            any_publish_failed = any(task["pull_request_status"] == "failed" for task in tasks)
            if all_tasks_passed and any_publish_failed:
                status = "warning"
            else:
                status = "passed" if all_tasks_passed else "failed"
        elif exit_code == 0:
            status = "passed"
        elif exit_code is not None:
            status = "failed"
        else:
            status = "unknown"

        return {
            "run_id": run_id,
            "status": status,
            "is_running": is_running,
            "exit_code": exit_code,
            "started_at": active_run.started_at if active_run else "",
            "command": active_run.command if active_run else [],
            "runner_stdout": _tail_text(runner_stdout_path),
            "runner_stderr": _tail_text(runner_stderr_path),
            "tasks": tasks,
        }

    @staticmethod
    def _auto_mark_tasks_reviewed(
        document: Dict[str, Any],
        latest_task_runs: Dict[str, Dict[str, Any]],
    ) -> bool:
        updated = False
        for task in document.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if bool(task.get("reviewed")):
                continue
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                continue
            latest_run = latest_task_runs.get(task_id) or {}
            latest_task = latest_run.get("task") if isinstance(latest_run, dict) else None
            if not isinstance(latest_task, dict):
                continue
            if (
                str(latest_task.get("status") or "") == "passed"
                and str(latest_task.get("pull_request_status") or "") == "passed"
            ):
                task["reviewed"] = True
                updated = True
        return updated

    def _list_run_ids(self, limit: int = 50) -> List[str]:
        run_ids = (
            {path.name for path in self.runs_root.iterdir() if path.is_dir()}
            if self.runs_root.exists()
            else set()
        )
        with self._lock:
            run_ids.update(self._active_runs.keys())
        return sorted(run_ids, reverse=True)[:limit]

    def _latest_task_runs(self, limit: int = 50) -> Dict[str, Dict[str, Any]]:
        latest_runs: Dict[str, Dict[str, Any]] = {}
        for run_id in self._list_run_ids(limit):
            detail = self.get_run(run_id)
            for task in detail.get("tasks", []):
                if task["task_id"] not in latest_runs:
                    latest_runs[task["task_id"]] = {
                        "run_id": run_id,
                        "task": task,
                    }
        return latest_runs

    def _build_run_summary(self, run_id: str) -> Dict[str, Any]:
        detail = self.get_run(run_id)
        tasks = detail.get("tasks", [])
        return {
            "run_id": run_id,
            "status": detail["status"],
            "is_running": detail["is_running"],
            "started_at": detail["started_at"],
            "task_count": len(tasks),
            "passed_count": len([task for task in tasks if task["status"] == "passed"]),
            "failed_count": len([task for task in tasks if task["status"] == "failed"]),
            "running_count": len([task for task in tasks if task["status"] == "running"]),
        }

    def _task_branch_snapshot(self, task_id: str) -> Dict[str, Any]:
        branch_name = task_branch_name(task_id)
        preferred_worktree_path = (self.worktrees_root / task_worktree_name(task_id)).resolve()
        snapshot = {
            "name": branch_name,
            "exists_locally": False,
            "has_worktree": False,
            "checked_out": False,
            "status": "new",
            "worktree_path": str(preferred_worktree_path),
        }

        try:
            active_worktree = self.git_repo.find_worktree_by_branch(branch_name)
            branch_exists = self.git_repo.branch_exists(branch_name)
        except GitCommandError:
            return snapshot

        snapshot["exists_locally"] = branch_exists
        if active_worktree is not None:
            snapshot["has_worktree"] = True
            snapshot["checked_out"] = True
            snapshot["status"] = "ready"
            snapshot["worktree_path"] = str(active_worktree.path)
            return snapshot

        if preferred_worktree_path.exists():
            snapshot["has_worktree"] = True
            try:
                current_branch = self.git_repo.current_branch(cwd=preferred_worktree_path)
            except GitCommandError:
                current_branch = ""
            if current_branch == branch_name:
                snapshot["checked_out"] = True
                snapshot["status"] = "ready"
            else:
                snapshot["status"] = "attention"
            return snapshot

        if branch_exists:
            snapshot["status"] = "branch_only"
        return snapshot

    @staticmethod
    def _normalize_task_payload(
        payload: Dict[str, Any],
        defaults: Dict[str, Any],
        existing_task: Optional[Dict[str, Any]] = None,
        backlog_root: Optional[Path] = None,
    ) -> Dict[str, Any]:
        task_id = str(payload.get("id", "")).strip()
        title = str(payload.get("title", "")).strip()
        test_command = payload.get("test_command", "")
        reviewed = bool(payload.get("reviewed", (existing_task or {}).get("reviewed", False)))
        if not task_id:
            raise ValueError("Task id is required.")
        if not title:
            raise ValueError("Task title is required.")

        if test_command in (None, ""):
            normalized_command = []
        elif isinstance(test_command, str):
            normalized_command = shlex.split(test_command)
        elif isinstance(test_command, list) and all(isinstance(item, str) for item in test_command):
            normalized_command = test_command
        else:
            raise ValueError("Task test_command must be a string, a list of strings, or empty.")

        raw_task = {
            "id": task_id,
            "title": title,
            "description": str(payload.get("description", "")).strip(),
            "base_branch": str(payload.get("base_branch") or defaults.get("base_branch", "main")).strip(),
            "working_directory": str(
                payload.get("working_directory") or defaults.get("working_directory", ".")
            ).strip(),
            "retry_limit": int(payload.get("retry_limit", defaults.get("retry_limit", 2))),
            "executor": {
                "type": str(
                    (payload.get("executor") or {}).get("type")
                    or defaults.get("executor", {}).get("type", "codex")
                ).strip(),
                "prompt": str((payload.get("executor") or {}).get("prompt", "")).strip(),
            },
            "test_command": normalized_command,
        }
        if reviewed:
            raw_task["reviewed"] = True
        build_task_definition(defaults, raw_task, backlog_root=backlog_root)
        return raw_task
