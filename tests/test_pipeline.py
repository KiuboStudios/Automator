from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main as automation_main
from overnight_runner.backlog import load_backlog, load_backlog_document
from overnight_runner.executors import CodexTaskExecutor, _build_codex_prompt, resolve_codex_binary
from overnight_runner.git_ops import GitRepository
from overnight_runner.models import (
    ExecutorConfig,
    PullRequestResult,
    TaskAttachment,
    TaskDefinition,
)
from overnight_runner.runner import (
    NoopPullRequestPublisher,
    TaskRunner,
)


class FileWritingExecutor:
    def execute(self, task, attempt, task_root, worktree_path, feedback, logger):
        target = worktree_path / "feature.txt"
        target.write_text(f"attempt {attempt}\n", encoding="utf-8")
        artifact = task_root / f"attempt-{attempt:02d}-executor-note.md"
        artifact.write_text("changed feature.txt\n", encoding="utf-8")
        logger.log("executor", "completed", "Wrote a test change.", attempt=attempt)
        return artifact


class AppendingExecutor:
    def __init__(self) -> None:
        self.invocations = 0

    def execute(self, task, attempt, task_root, worktree_path, feedback, logger):
        self.invocations += 1
        target = worktree_path / "feature.txt"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"run {self.invocations}\n")
        artifact = task_root / f"attempt-{attempt:02d}-executor-note.md"
        artifact.write_text(f"changed feature.txt on run {self.invocations}\n", encoding="utf-8")
        logger.log("executor", "completed", "Appended a reusable test change.", attempt=attempt)
        return artifact


class OvernightRunnerTests(unittest.TestCase):
    def test_resolve_codex_binary_uses_common_fallback_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_codex = Path(temp_dir) / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)

            with patch.dict("os.environ", {}, clear=True):
                with patch(
                    "overnight_runner.executors.shutil.which",
                    return_value=None,
                ):
                    with patch(
                        "overnight_runner.executors._COMMON_CODEX_BINARIES",
                        (str(fake_codex),),
                    ):
                        resolved = resolve_codex_binary()

            self.assertEqual(resolved, str(fake_codex))

    def test_git_repository_parses_github_ssh_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self._init_git_repo(Path(temp_dir) / "repo")
            subprocess.run(
                ["git", "remote", "add", "origin", "git@github.com:KiuboStudios/kiubo-app.git"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )

            remote = GitRepository(repo_root).resolve_github_remote()

            self.assertEqual(remote.host, "github.com")
            self.assertEqual(remote.owner, "KiuboStudios")
            self.assertEqual(remote.repo, "kiubo-app")
            self.assertEqual(remote.https_url, "https://github.com/KiuboStudios/kiubo-app.git")

    def test_load_backlog_resolves_task_attachment_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "tasks.json"
            attachment_path = temp_path / "attachments" / "demo-task" / "mockup.png"
            attachment_path.parent.mkdir(parents=True, exist_ok=True)
            attachment_path.write_bytes(b"fake-image")
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                                "attachments": [
                                    {
                                        "id": "att-1",
                                        "name": "mockup.png",
                                        "relative_path": "attachments/demo-task/mockup.png",
                                        "content_type": "image/png",
                                        "size_bytes": 10,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            tasks = load_backlog(backlog_path)

            self.assertEqual(len(tasks[0].attachments), 1)
            self.assertEqual(tasks[0].attachments[0].path, str(attachment_path.resolve()))
            self.assertEqual(tasks[0].attachments[0].content_type, "image/png")

    def test_codex_executor_adds_image_attachments_and_reference_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            worktree_path = temp_path / "worktree"
            task_root = temp_path / "run-task"
            worktree_path.mkdir(parents=True, exist_ok=True)
            task_root.mkdir(parents=True, exist_ok=True)

            image_path = temp_path / "attachments" / "demo-task" / "mockup.png"
            doc_path = temp_path / "attachments" / "demo-task" / "notes.txt"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"fake-image")
            doc_path.write_text("demo notes", encoding="utf-8")

            task = TaskDefinition(
                id="demo-task",
                title="Demo task",
                description="Use the reference files",
                base_branch="main",
                working_directory=".",
                test_command=[],
                retry_limit=1,
                executor=ExecutorConfig(type="codex", prompt="Check the attachments."),
                attachments=[
                    TaskAttachment(
                        id="img-1",
                        name="mockup.png",
                        path=str(image_path),
                        relative_path="attachments/demo-task/mockup.png",
                        content_type="image/png",
                        size_bytes=10,
                    ),
                    TaskAttachment(
                        id="doc-1",
                        name="notes.txt",
                        path=str(doc_path),
                        relative_path="attachments/demo-task/notes.txt",
                        content_type="text/plain",
                        size_bytes=10,
                    ),
                ],
            )

            prompt = _build_codex_prompt(task, "")
            self.assertIn("Attached reference files:", prompt)
            self.assertIn(str(image_path), prompt)
            self.assertIn(str(doc_path), prompt)

            with patch.dict("os.environ", {}, clear=True):
                executor = CodexTaskExecutor(codex_bin="/opt/homebrew/bin/codex")
                with patch("overnight_runner.executors.subprocess.run") as run_mock:
                    run_mock.return_value = subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout="ok",
                        stderr="",
                    )
                    artifact_path = executor.execute(
                        task=task,
                        attempt=1,
                        task_root=task_root,
                        worktree_path=worktree_path,
                        feedback="",
                        logger=Mock(),
                    )

            self.assertTrue(artifact_path.exists())
            command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
            self.assertIn("--image", command)
            self.assertIn(str(image_path.resolve()), command)
            self.assertIn("--add-dir", command)
            self.assertIn(str(image_path.parent.resolve()), command)
            self.assertIn("--profile", command)
            self.assertEqual(command[command.index("--profile") + 1], "overnight")

    def test_codex_executor_uses_extension_fallback_for_image_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            worktree_path = temp_path / "worktree"
            task_root = temp_path / "run-task"
            worktree_path.mkdir(parents=True, exist_ok=True)
            task_root.mkdir(parents=True, exist_ok=True)

            image_path = temp_path / "attachments" / "demo-task" / "mockup.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"fake-image")

            task = TaskDefinition(
                id="demo-task",
                title="Demo task",
                description="Use the reference files",
                base_branch="main",
                working_directory=".",
                test_command=[],
                retry_limit=1,
                executor=ExecutorConfig(type="codex", prompt="Check the attachments."),
                attachments=[
                    TaskAttachment(
                        id="img-1",
                        name="mockup.png",
                        path=str(image_path),
                        relative_path="attachments/demo-task/mockup.png",
                        content_type="application/octet-stream",
                        size_bytes=10,
                    )
                ],
            )

            with patch.dict("os.environ", {}, clear=True):
                executor = CodexTaskExecutor(codex_bin="/opt/homebrew/bin/codex")
                with patch("overnight_runner.executors.subprocess.run") as run_mock:
                    run_mock.return_value = subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout="ok",
                        stderr=(
                            "session id: 11111111-1111-1111-1111-111111111111\n"
                            + str(image_path)
                        ),
                    )
                    executor.execute(
                        task=task,
                        attempt=1,
                        task_root=task_root,
                        worktree_path=worktree_path,
                        feedback="",
                        logger=Mock(),
                    )

            command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
            self.assertIn("--image", command)
            self.assertIn(str(image_path.resolve()), command)

    def test_codex_executor_strict_attachment_guard_fails_on_anomalies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            worktree_path = temp_path / "worktree"
            task_root = temp_path / "run-task"
            worktree_path.mkdir(parents=True, exist_ok=True)
            task_root.mkdir(parents=True, exist_ok=True)

            image_path = temp_path / "attachments" / "demo-task" / "mockup.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"fake-image")

            task = TaskDefinition(
                id="demo-task",
                title="Demo task",
                description="Use the reference files",
                base_branch="main",
                working_directory=".",
                test_command=[],
                retry_limit=1,
                executor=ExecutorConfig(type="codex", prompt="Check the attachments."),
                attachments=[
                    TaskAttachment(
                        id="img-1",
                        name="mockup.png",
                        path=str(image_path),
                        relative_path="attachments/demo-task/mockup.png",
                        content_type="application/octet-stream",
                        size_bytes=10,
                    )
                ],
            )

            with patch.dict("os.environ", {"CODEX_ATTACHMENT_GUARD": "fail"}, clear=True):
                executor = CodexTaskExecutor(codex_bin="/opt/homebrew/bin/codex")
                with patch("overnight_runner.executors.subprocess.run") as run_mock:
                    run_mock.return_value = subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout="ok",
                        stderr=str(image_path),
                    )
                    with self.assertRaises(RuntimeError):
                        executor.execute(
                            task=task,
                            attempt=1,
                            task_root=task_root,
                            worktree_path=worktree_path,
                            feedback="",
                            logger=Mock(),
                        )

    def test_codex_executor_fails_preflight_when_attachment_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            worktree_path = temp_path / "worktree"
            task_root = temp_path / "run-task"
            worktree_path.mkdir(parents=True, exist_ok=True)
            task_root.mkdir(parents=True, exist_ok=True)

            missing_path = temp_path / "attachments" / "demo-task" / "missing.png"
            task = TaskDefinition(
                id="demo-task",
                title="Demo task",
                description="Use the reference files",
                base_branch="main",
                working_directory=".",
                test_command=[],
                retry_limit=1,
                executor=ExecutorConfig(type="codex", prompt="Check the attachments."),
                attachments=[
                    TaskAttachment(
                        id="img-1",
                        name="missing.png",
                        path=str(missing_path),
                        relative_path="attachments/demo-task/missing.png",
                        content_type="image/png",
                        size_bytes=10,
                    )
                ],
            )

            with patch.dict("os.environ", {}, clear=True):
                executor = CodexTaskExecutor(codex_bin="/opt/homebrew/bin/codex")
                with patch("overnight_runner.executors.subprocess.run") as run_mock:
                    with self.assertRaises(RuntimeError):
                        executor.execute(
                            task=task,
                            attempt=1,
                            task_root=task_root,
                            worktree_path=worktree_path,
                            feedback="",
                            logger=Mock(),
                        )
                    run_mock.assert_not_called()

    def test_git_repository_reports_current_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self._init_git_repo(Path(temp_dir) / "repo")

            current_branch = GitRepository(repo_root).current_branch()

            self.assertEqual(current_branch, "main")

    def test_main_refuses_to_run_outside_main_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "tasks.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                                "base_branch": "main",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            args = automation_main.argparse.Namespace(
                repo_root=str(temp_path),
                backlog=str(backlog_path),
                runs_root=str(temp_path / "runs"),
                worktrees_root=str(temp_path / "worktrees"),
                task_ids=None,
                max_tasks=None,
                run_id=None,
                resume_run_id=None,
                resume_from=None,
            )

            with patch.object(automation_main, "parse_args", return_value=args):
                with patch.object(automation_main.GitRepository, "current_branch", return_value="feature/demo"):
                    with patch.object(automation_main.GitRepository, "has_uncommitted_changes", return_value=False):
                        with patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=True):
                            result = automation_main.main()

            self.assertEqual(result, 1)

    def test_project_backlog_sample_is_self_contained(self) -> None:
        backlog_path = Path(__file__).resolve().parents[1] / "backlog" / "tasks.example.json"

        document = load_backlog_document(backlog_path)

        self.assertEqual(document["defaults"]["executor"]["type"], "codex")
        self.assertIsInstance(document["tasks"], list)

    def test_load_backlog_applies_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backlog_path = Path(temp_dir) / "tasks.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "defaults": {
                            "base_branch": "main",
                            "working_directory": ".",
                            "retry_limit": 2,
                            "executor": {"type": "noop"},
                        },
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                                "test_command": ["${RUNNER_PYTHON}", "-c", "print('ok')"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            tasks = load_backlog(backlog_path)

            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].base_branch, "main")
            self.assertEqual(tasks[0].retry_limit, 2)
            self.assertEqual(tasks[0].executor.type, "noop")
            self.assertEqual(tasks[0].test_command[0], sys.executable)

    def test_load_backlog_allows_missing_test_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backlog_path = Path(temp_dir) / "tasks.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "no-tests",
                                "title": "No tests task",
                                "base_branch": "main",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            tasks = load_backlog(backlog_path)

            self.assertEqual(tasks[0].test_command, [])

    def test_load_backlog_resolves_registered_repository_for_each_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_a = self._init_git_repo(temp_path / "repo-a")
            repo_b = self._init_git_repo(temp_path / "repo-b")
            backlog_path = temp_path / "tasks.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "defaults": {
                            "repository_id": "repo-a",
                        },
                        "repositories": [
                            {"id": "repo-a", "path": str(repo_a)},
                            {"id": "repo-b", "path": str(repo_b)},
                        ],
                        "tasks": [
                            {
                                "id": "multi-repo-task",
                                "title": "Runs on repo b",
                                "repository_id": "repo-b",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            tasks = load_backlog(backlog_path, default_repo_root=temp_path)

            self.assertEqual(tasks[0].repository_id, "repo-b")
            self.assertEqual(tasks[0].repository_path, str(repo_b.resolve()))

    def test_load_backlog_skips_reviewed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backlog_path = Path(temp_dir) / "tasks.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "active-task",
                                "title": "Active task",
                            },
                            {
                                "id": "reviewed-task",
                                "title": "Reviewed task",
                                "reviewed": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            tasks = load_backlog(backlog_path)

            self.assertEqual([task.id for task in tasks], ["active-task"])

    def test_runner_processes_a_task_in_an_isolated_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = self._init_git_repo(temp_path / "repo")
            runs_root = temp_path / "runs"
            worktrees_root = temp_path / "worktrees"

            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                                "base_branch": "main",
                                "working_directory": ".",
                                "retry_limit": 2,
                                "executor": {"type": "noop"},
                                "test_command": [sys.executable, "-c", "print('ok')"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            runner = TaskRunner(
                repo_root=repo_root,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=FileWritingExecutor(),
                pull_request_publisher=NoopPullRequestPublisher(),
            )
            results = runner.run(load_backlog(backlog_path))

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].status, "passed")
            self.assertEqual(results[0].attempts, 1)
            self.assertTrue(results[0].log_path.exists())
            summary = json.loads(results[0].summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "passed")

            show_ref = subprocess.run(
                ["git", "show-ref", "--verify", f"refs/heads/{results[0].branch_name}"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(show_ref.returncode, 0)

    def test_runner_commits_changes_before_validation_and_records_pr_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = self._init_git_repo(temp_path / "repo")
            runs_root = temp_path / "runs"
            worktrees_root = temp_path / "worktrees"
            subprocess.run(
                ["git", "remote", "add", "origin", "git@github.com:KiuboStudios/kiubo-app.git"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )

            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "commit-flow",
                                "title": "Commit flow",
                                "base_branch": "main",
                                "working_directory": ".",
                                "retry_limit": 1,
                                "executor": {"type": "noop"},
                                "test_command": [sys.executable, "-c", "print('ok')"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            class FakePublisher:
                def publish(self, task, branch_name, worktree_path, logger):
                    logger.log(
                        "pull_request",
                        "completed",
                        "Created GitHub pull request.",
                        pr_url="https://github.com/KiuboStudios/kiubo-app/pull/123",
                    )
                    return PullRequestResult(
                        status="passed",
                        url="https://github.com/KiuboStudios/kiubo-app/pull/123",
                        number=123,
                    )

            runner = TaskRunner(
                repo_root=repo_root,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=FileWritingExecutor(),
                pull_request_publisher=FakePublisher(),
            )

            results = runner.run(load_backlog(backlog_path))

            self.assertEqual(results[0].status, "passed")
            self.assertEqual(
                results[0].pull_request_url,
                "https://github.com/KiuboStudios/kiubo-app/pull/123",
            )
            log_content = results[0].log_path.read_text(encoding="utf-8")
            self.assertIn('"step": "commit"', log_content)

            worktree_path = worktrees_root / "commit-flow"
            commit_count = subprocess.run(
                ["git", "rev-list", "--count", "main..HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(commit_count.stdout.strip(), "1")

    def test_runner_reuses_the_same_branch_and_worktree_for_repeated_task_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = self._init_git_repo(temp_path / "repo")
            runs_root = temp_path / "runs"
            worktrees_root = temp_path / "worktrees"

            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "resume-task",
                                "title": "Resume task",
                                "base_branch": "main",
                                "working_directory": ".",
                                "retry_limit": 1,
                                "executor": {"type": "noop"},
                                "test_command": [sys.executable, "-c", "print('ok')"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            runner = TaskRunner(
                repo_root=repo_root,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=AppendingExecutor(),
                pull_request_publisher=NoopPullRequestPublisher(),
            )

            first_results = runner.run_with_id(
                load_backlog(backlog_path),
                run_id="20260410T100000Z",
            )
            second_results = runner.run_with_id(
                load_backlog(backlog_path),
                run_id="20260410T110000Z",
            )

            worktree_path = worktrees_root / "resume-task"
            commit_count = subprocess.run(
                ["git", "rev-list", "--count", "main..HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertEqual(first_results[0].branch_name, "automator/resume-task")
            self.assertEqual(second_results[0].branch_name, "automator/resume-task")
            self.assertEqual(commit_count.stdout.strip(), "2")
            self.assertIn(
                "Reusing the existing task branch and worktree.",
                second_results[0].log_path.read_text(encoding="utf-8"),
            )

    def test_runner_uses_the_task_repository_in_multirepo_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            control_repo = self._init_git_repo(temp_path / "control-repo")
            target_repo = self._init_git_repo(temp_path / "target-repo")
            runs_root = temp_path / "runs"
            worktrees_root = temp_path / "worktrees"

            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "defaults": {
                            "repository_id": "target",
                            "base_branch": "main",
                            "working_directory": ".",
                            "retry_limit": 1,
                            "executor": {"type": "noop"},
                        },
                        "repositories": [
                            {"id": "control", "path": str(control_repo)},
                            {"id": "target", "path": str(target_repo)},
                        ],
                        "tasks": [
                            {
                                "id": "target-task",
                                "title": "Target task",
                                "repository_id": "target",
                                "test_command": [sys.executable, "-c", "print('ok')"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            runner = TaskRunner(
                repo_root=control_repo,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=FileWritingExecutor(),
                pull_request_publisher=NoopPullRequestPublisher(),
            )
            results = runner.run(
                load_backlog(backlog_path, default_repo_root=control_repo)
            )

            self.assertEqual(results[0].status, "passed")
            target_ref = subprocess.run(
                ["git", "show-ref", "--verify", "refs/heads/automator/target-task"],
                cwd=target_repo,
                capture_output=True,
                text=True,
                check=False,
            )
            control_ref = subprocess.run(
                ["git", "show-ref", "--verify", "refs/heads/automator/target-task"],
                cwd=control_repo,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(target_ref.returncode, 0)
            self.assertNotEqual(control_ref.returncode, 0)

    def test_runner_keeps_task_passed_when_pr_publishing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = self._init_git_repo(temp_path / "repo")
            runs_root = temp_path / "runs"
            worktrees_root = temp_path / "worktrees"

            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "publish-warning",
                                "title": "Publish warning",
                                "base_branch": "main",
                                "working_directory": ".",
                                "retry_limit": 1,
                                "executor": {"type": "noop"},
                                "test_command": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            class FailingPublisher:
                def publish(self, task, branch_name, worktree_path, logger):
                    raise RuntimeError("Push rejected with HTTP 403")

            runner = TaskRunner(
                repo_root=repo_root,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=FileWritingExecutor(),
                pull_request_publisher=FailingPublisher(),
            )

            results = runner.run(load_backlog(backlog_path))

            self.assertEqual(results[0].status, "passed")
            self.assertEqual(results[0].pull_request_status, "failed")
            self.assertEqual(results[0].workflow_state["next_resume_stage"], "pull_request")
            self.assertEqual(results[0].last_blocker["stage"], "pull_request")
            self.assertEqual(results[0].resume_count, 0)
            events_content = results[0].log_path.read_text(encoding="utf-8")
            self.assertIn('"step": "pull_request"', events_content)
            self.assertIn('"status": "failed"', events_content)
            self.assertIn("Review is skipped because pull request publishing failed.", events_content)

    def test_runner_can_resume_pull_request_publication_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = self._init_git_repo(temp_path / "repo")
            runs_root = temp_path / "runs"
            worktrees_root = temp_path / "worktrees"
            run_id = "20260410T150747Z"
            run_root = runs_root / run_id
            task_root = run_root / "tasks" / "resume-task"
            task_root.mkdir(parents=True, exist_ok=True)

            task = TaskDefinition(
                id="resume-task",
                title="Resume task",
                description="Resume PR publishing",
                base_branch="main",
                working_directory=".",
                test_command=[],
                retry_limit=1,
                executor=ExecutorConfig(type="noop", prompt=""),
            )
            GitRepository(repo_root).prepare_task_worktree(
                branch_name="automator/resume-task",
                base_branch="main",
                destination=worktrees_root / "resume-task",
            )
            (task_root / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T15:07:47+00:00",
                                "step": "task",
                                "status": "passed",
                                "message": "Validation passed for the task.",
                                "context": {
                                    "task_id": "resume-task",
                                    "title": "Resume task",
                                    "branch_name": "automator/resume-task",
                                    "attempt": 1,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T15:08:21+00:00",
                                "step": "pull_request",
                                "status": "failed",
                                "message": "Pull request creation failed.",
                                "context": {"error": "HTTP 403"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (task_root / "task-summary.json").write_text(
                json.dumps(
                    {
                        "task_id": "resume-task",
                        "title": "Resume task",
                        "status": "passed",
                        "branch_name": "automator/resume-task",
                        "attempts": 1,
                        "pull_request_status": "failed",
                        "pull_request_url": "",
                    }
                ),
                encoding="utf-8",
            )
            (run_root / "summary.json").write_text(
                json.dumps(
                    [
                        {
                            "task_id": "resume-task",
                            "title": "Resume task",
                            "status": "passed",
                            "branch_name": "automator/resume-task",
                            "attempts": 1,
                            "log_path": str(task_root / "events.jsonl"),
                            "summary_path": str(task_root / "task-summary.json"),
                            "pull_request_status": "failed",
                            "pull_request_url": "",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            class FakePublisher:
                def publish(self, task, branch_name, worktree_path, logger):
                    logger.log(
                        "pull_request",
                        "completed",
                        "Created GitHub pull request.",
                        branch_name=branch_name,
                        pr_url="https://github.com/KiuboStudios/kiubo-app/pull/321",
                    )
                    return PullRequestResult(
                        status="passed",
                        url="https://github.com/KiuboStudios/kiubo-app/pull/321",
                        number=321,
                    )

            runner = TaskRunner(
                repo_root=repo_root,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=FileWritingExecutor(),
                pull_request_publisher=FakePublisher(),
            )

            result = runner.resume_task_with_id(task, run_id=run_id, resume_from="pull_request")

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.pull_request_status, "passed")
            self.assertEqual(
                result.pull_request_url,
                "https://github.com/KiuboStudios/kiubo-app/pull/321",
            )
            self.assertEqual(result.resume_count, 1)
            self.assertEqual(result.last_resume_from, "pull_request")
            self.assertEqual(result.resume_history[0]["resume_from"], "pull_request")
            self.assertEqual(result.resume_history[0]["status"], "passed")
            self.assertEqual(result.resume_history[0]["next_resume_stage"], "")
            self.assertEqual(result.workflow_state["next_resume_stage"], "")
            self.assertEqual(result.last_blocker, {})
            self.assertTrue(result.workflow_state_path.exists())
            self.assertTrue(result.resume_history_path.exists())
            summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary[0]["pull_request_status"], "passed")
            self.assertEqual(
                summary[0]["pull_request_url"],
                "https://github.com/KiuboStudios/kiubo-app/pull/321",
            )
            self.assertEqual(summary[0]["resume_count"], 1)
            self.assertEqual(summary[0]["last_resume_from"], "pull_request")
            self.assertEqual(summary[0]["resume_history"][0]["resume_from"], "pull_request")
            self.assertEqual(summary[0]["workflow_state"]["next_resume_stage"], "")
            self.assertEqual(summary[0]["last_blocker"], {})
            event_log = (task_root / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("Resuming task processing from PR.", event_log)
            self.assertIn("Created GitHub pull request.", event_log)

    def test_runner_supports_logs_inside_repo_with_external_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = self._init_git_repo(temp_path / "repo")
            runs_root = repo_root / "automation" / "runs"
            worktrees_root = temp_path / "worktrees"

            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "repo-logs-task",
                                "title": "Repo logs task",
                                "base_branch": "main",
                                "working_directory": ".",
                                "retry_limit": 2,
                                "executor": {"type": "noop"},
                                "test_command": [sys.executable, "-c", "print('ok')"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            runner = TaskRunner(
                repo_root=repo_root,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=FileWritingExecutor(),
                pull_request_publisher=NoopPullRequestPublisher(),
            )
            results = runner.run(load_backlog(backlog_path))

            self.assertEqual(results[0].status, "passed")
            self.assertTrue(results[0].log_path.exists())
            self.assertTrue(results[0].summary_path.exists())

    def test_runner_retries_failed_validations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = self._init_git_repo(temp_path / "repo")
            runs_root = temp_path / "runs"
            worktrees_root = temp_path / "worktrees"

            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "failing-task",
                                "title": "Failing task",
                                "base_branch": "main",
                                "working_directory": ".",
                                "retry_limit": 2,
                                "executor": {"type": "noop"},
                                "test_command": [
                                    sys.executable,
                                    "-c",
                                    "import sys; sys.exit(1)",
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            runner = TaskRunner(
                repo_root=repo_root,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=FileWritingExecutor(),
                pull_request_publisher=NoopPullRequestPublisher(),
            )
            results = runner.run(load_backlog(backlog_path))

            self.assertEqual(results[0].status, "failed")
            self.assertEqual(results[0].attempts, 3)
            stderr_log = results[0].summary_path.parent / "attempt-03-tests.stderr.log"
            self.assertTrue(stderr_log.exists())

    def test_runner_skips_validation_when_task_has_no_test_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = self._init_git_repo(temp_path / "repo")
            runs_root = temp_path / "runs"
            worktrees_root = temp_path / "worktrees"

            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "skip-tests",
                                "title": "Skip tests task",
                                "base_branch": "main",
                                "working_directory": ".",
                                "retry_limit": 2,
                                "executor": {"type": "noop"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            runner = TaskRunner(
                repo_root=repo_root,
                runs_root=runs_root,
                worktrees_root=worktrees_root,
                executor=FileWritingExecutor(),
                pull_request_publisher=NoopPullRequestPublisher(),
            )
            results = runner.run(load_backlog(backlog_path))

            self.assertEqual(results[0].status, "passed")
            self.assertEqual(results[0].attempts, 1)
            events_path = results[0].summary_path.parent / "events.jsonl"
            events_content = events_path.read_text(encoding="utf-8")
            self.assertIn('"step": "tests"', events_content)
            self.assertIn('"status": "skipped"', events_content)

    def _init_git_repo(self, repo_root: Path) -> Path:
        repo_root.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Codex Tester"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "codex@example.com"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        (repo_root / "README.md").write_text("temp repo\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return repo_root


if __name__ == "__main__":
    unittest.main()
