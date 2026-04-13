from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from overnight_runner.backlog import load_backlog
from overnight_runner.executors import (
    CodexTaskExecutor,
    DelegatingTaskExecutor,
    NoopTaskExecutor,
)
from overnight_runner.git_ops import GitRepository
from overnight_runner.publishers import GitHubPullRequestPublisher
from overnight_runner.runner import TaskRunner


def _default_worktrees_root(repo_root: Path) -> Path:
    return Path(tempfile.gettempdir()) / "kiubo-automation-worktrees" / repo_root.name


def parse_args() -> argparse.Namespace:
    default_backlog = REPO_ROOT / "backlog" / "tasks.json"
    default_runs_root = REPO_ROOT / "runs"
    default_worktrees_root = _default_worktrees_root(REPO_ROOT)

    parser = argparse.ArgumentParser(
        description="Run backlog tasks in reusable git task worktrees."
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Path to the git repository root.",
    )
    parser.add_argument(
        "--backlog",
        default=str(default_backlog),
        help="Path to the backlog JSON file.",
    )
    parser.add_argument(
        "--runs-root",
        default=str(default_runs_root),
        help="Directory where run logs and artifacts are stored.",
    )
    parser.add_argument(
        "--worktrees-root",
        default=str(default_worktrees_root),
        help="Directory where reusable git task worktrees are created.",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        help="Run only the matching task id. Repeat to run multiple tasks.",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Limit how many tasks are processed from the backlog.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional run id. Used by the local dashboard to monitor a run in real time.",
    )
    parser.add_argument(
        "--resume-run-id",
        default=None,
        help="Resume a task inside an existing run directory.",
    )
    parser.add_argument(
        "--resume-from",
        default=None,
        choices=["branch", "code", "tests", "pull_request"],
        help="Stage to resume from when --resume-run-id is provided.",
    )
    return parser.parse_args()


def _select_tasks(tasks, task_ids, max_tasks):
    selected = tasks
    if task_ids:
        allowed = set(task_ids)
        selected = [task for task in selected if task.id in allowed]
    if max_tasks is not None:
        selected = selected[:max_tasks]
    return selected


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    backlog_path = Path(args.backlog).resolve()
    runs_root = Path(args.runs_root).resolve()
    worktrees_root = Path(args.worktrees_root).resolve()
    git_repo = GitRepository(repo_root)

    try:
        tasks = load_backlog(backlog_path)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    tasks = _select_tasks(tasks, args.task_ids, args.max_tasks)
    if not tasks:
        print("No tasks selected from the backlog.", file=sys.stderr)
        return 1
    if bool(args.resume_run_id) != bool(args.resume_from):
        print(
            "Error: --resume-run-id and --resume-from must be provided together.",
            file=sys.stderr,
        )
        return 1
    if args.resume_run_id and len(tasks) != 1:
        print(
            "Error: resume mode requires exactly one selected task.",
            file=sys.stderr,
        )
        return 1
    if not args.resume_run_id:
        current_branch = git_repo.current_branch()
        if current_branch != "main":
            print(
                "Error: development flow can only start from the `main` branch. "
                f"Current branch: `{current_branch}`.",
                file=sys.stderr,
            )
            return 1
        if git_repo.has_uncommitted_changes():
            print(
                "Error: development flow can only start with a clean working tree. "
                "Commit or stash your local changes first.",
                file=sys.stderr,
            )
            return 1
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        print(
            "Error: GITHUB_TOKEN is not set. Use a fine-grained GitHub personal access token "
            "with repository Contents and Pull requests write permissions.",
            file=sys.stderr,
        )
        return 1

    runner = TaskRunner(
        repo_root=repo_root,
        runs_root=runs_root,
        worktrees_root=worktrees_root,
        executor=DelegatingTaskExecutor(
            executors={
                "codex": CodexTaskExecutor(),
                "noop": NoopTaskExecutor(),
            },
            default_executor_type="codex",
        ),
        pull_request_publisher=GitHubPullRequestPublisher(repo_root=repo_root),
    )
    if args.resume_run_id:
        target_run_id = args.run_id or args.resume_run_id
        results = [
            runner.resume_task_with_id(
                tasks[0],
                run_id=target_run_id,
                resume_from=args.resume_from,
            )
        ]
    else:
        results = runner.run_with_id(tasks, run_id=args.run_id)

    for result in results:
        print(
            f"{result.status.upper():<6} "
            f"{result.task_id} "
            f"branch={result.branch_name} "
            f"attempts={result.attempts} "
            f"log={result.log_path}"
        )

    return 0 if all(result.status == "passed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
