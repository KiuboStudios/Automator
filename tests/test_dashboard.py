from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.local_dashboard.service import DashboardService
from automation.overnight_runner.git_ops import GitWorktree


class DashboardServiceTests(unittest.TestCase):
    def test_upsert_task_persists_and_serves_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            saved = service.upsert_task(
                {
                    "id": "demo-task",
                    "title": "Demo task",
                    "new_attachments": [
                        {
                            "name": "mockup.png",
                            "content_type": "image/png",
                            "data_base64": base64.b64encode(b"fake-image").decode("ascii"),
                        },
                        {
                            "name": "notes.txt",
                            "content_type": "text/plain",
                            "data_base64": base64.b64encode(b"demo notes").decode("ascii"),
                        },
                    ],
                }
            )

            self.assertEqual(len(saved["attachments"]), 2)
            self.assertEqual(saved["attachments"][0]["kind"], "image")
            self.assertEqual(saved["attachments"][1]["kind"], "file")

            image_attachment = saved["attachments"][0]
            attachment_path = temp_path / image_attachment["relative_path"]
            self.assertTrue(attachment_path.exists())

            served_attachment = service.read_task_attachment("demo-task", image_attachment["id"])
            self.assertEqual(served_attachment["body"], b"fake-image")
            self.assertEqual(served_attachment["content_type"], "image/png")

    def test_upsert_task_removes_unkept_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            initial = service.upsert_task(
                {
                    "id": "demo-task",
                    "title": "Demo task",
                    "new_attachments": [
                        {
                            "name": "mockup.png",
                            "content_type": "image/png",
                            "data_base64": base64.b64encode(b"fake-image").decode("ascii"),
                        },
                        {
                            "name": "notes.txt",
                            "content_type": "text/plain",
                            "data_base64": base64.b64encode(b"demo notes").decode("ascii"),
                        },
                    ],
                }
            )

            removed_attachment = initial["attachments"][0]
            kept_attachment = initial["attachments"][1]
            updated = service.upsert_task(
                {
                    "id": "demo-task",
                    "title": "Demo task",
                    "kept_attachment_ids": [kept_attachment["id"]],
                    "new_attachments": [],
                }
            )

            self.assertEqual([item["id"] for item in updated["attachments"]], [kept_attachment["id"]])
            self.assertFalse((temp_path / removed_attachment["relative_path"]).exists())
            self.assertTrue((temp_path / kept_attachment["relative_path"]).exists())

    def test_get_overview_reports_dashboard_environment_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            with patch.object(service.git_repo, "current_branch", return_value="main"):
                with patch.object(service.git_repo, "has_uncommitted_changes", return_value=False):
                    with patch.object(
                        service.git_repo,
                        "list_merged_branches",
                        return_value=["main", "automator/demo-task", "automator/other-task"],
                    ):
                        with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}, clear=True):
                            with patch(
                                "automation.local_dashboard.service.resolve_codex_binary",
                                return_value="/opt/homebrew/bin/codex",
                            ):
                                overview = service.get_overview()

            self.assertFalse(overview["dirty_worktree"])
            self.assertEqual(overview["current_branch"], "main")
            self.assertTrue(overview["development_flow_allowed"])
            self.assertTrue(overview["github_token_available"])
            self.assertTrue(overview["codex_cli_available"])
            self.assertEqual(overview["codex_cli_path"], "/opt/homebrew/bin/codex")
            self.assertEqual(overview["merged_task_branch_count"], 2)
            self.assertTrue(overview["branch_cleanup_allowed"])
            self.assertTrue(overview["dashboard_accessible"])
            self.assertEqual(overview["runner_state"], "idle")
            self.assertTrue(overview["runner_launch_ready"])
            self.assertEqual(overview["active_runs"], [])
            self.assertIsNone(overview["latest_run"])
            self.assertFalse(overview["quota_telemetry_available"])

    def test_get_overview_reports_running_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )
            run_id = "20260410T103000Z"
            active_run = Mock()
            active_run.is_running.return_value = True
            service._active_runs[run_id] = active_run
            run_detail = {
                "run_id": run_id,
                "status": "running",
                "is_running": True,
                "started_at": "2026-04-10T10:30:00+00:00",
                "exit_code": None,
                "tasks": [
                    {
                        "task_id": "demo-task",
                        "title": "Demo task",
                        "status": "running",
                        "pipeline": {
                            "summary": "Running validation",
                            "current_stage": "tests",
                        },
                        "events": [
                            {
                                "timestamp": "2026-04-10T10:31:00+00:00",
                                "step": "tests",
                                "status": "started",
                                "message": "Running validation.",
                            }
                        ],
                    }
                ],
            }

            with patch.object(service.git_repo, "current_branch", return_value="main"):
                with patch.object(service.git_repo, "has_uncommitted_changes", return_value=False):
                    with patch.object(service.git_repo, "list_merged_branches", return_value=["main"]):
                        with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}, clear=True):
                            with patch(
                                "automation.local_dashboard.service.resolve_codex_binary",
                                return_value="/opt/homebrew/bin/codex",
                            ):
                                with patch.object(service, "_list_run_ids", return_value=[run_id]):
                                    with patch.object(service, "get_run", return_value=run_detail):
                                        overview = service.get_overview()

            self.assertEqual(overview["runner_state"], "running")
            self.assertEqual(overview["active_run_count"], 1)
            self.assertEqual(overview["active_task_count"], 1)
            self.assertEqual(overview["running_task_count"], 1)
            self.assertTrue(overview["runner_launch_ready"])
            self.assertEqual(overview["latest_run"]["run_id"], run_id)
            self.assertEqual(overview["current_activity"]["task_id"], "demo-task")
            self.assertEqual(overview["current_activity"]["summary"], "Running validation")

    def test_start_run_requires_main_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            with patch.object(service.git_repo, "current_branch", return_value="feature/test"):
                with self.assertRaisesRegex(ValueError, "Current branch: `feature/test`"):
                    service.start_run(task_ids=["demo-task"])

    def test_start_run_requires_clean_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            with patch.object(service.git_repo, "current_branch", return_value="main"):
                with patch.object(service.git_repo, "has_uncommitted_changes", return_value=True):
                    with self.assertRaisesRegex(ValueError, "clean working tree"):
                        service.start_run(task_ids=["demo-task"])

    def test_get_backlog_redacts_authorization_headers_in_failure_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            run_id = "20260410T103000Z"
            run_root = temp_path / "runs" / run_id
            task_root = run_root / "tasks" / "demo-task"
            task_root.mkdir(parents=True, exist_ok=True)
            (task_root / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T10:30:00+00:00",
                                "step": "task",
                                "status": "started",
                                "message": "Starting task processing.",
                                "context": {
                                    "task_id": "demo-task",
                                    "title": "Demo task",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T10:30:05+00:00",
                                "step": "pull_request",
                                "status": "failed",
                                "message": "Pull request creation failed.",
                                "context": {
                                    "error": "Failed to push the task branch to GitHub: git -c "
                                    "http.https://github.com/.extraheader=AUTHORIZATION: basic "
                                    "Y29kZXg6Z2l0aHViX3BhdF9hYmMxMjM push"
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T10:30:06+00:00",
                                "step": "task",
                                "status": "error",
                                "message": "Task processing raised an unexpected error.",
                                "context": {
                                    "error": "Failed to push the task branch to GitHub.",
                                },
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
                        "task_id": "demo-task",
                        "title": "Demo task",
                        "status": "failed",
                        "branch_name": "automator/demo-task-20260410t103000z",
                        "attempts": 1,
                        "pull_request_status": "failed",
                    }
                ),
                encoding="utf-8",
            )
            (run_root / "summary.json").write_text(
                json.dumps(
                    [
                        {
                            "task_id": "demo-task",
                            "title": "Demo task",
                            "status": "failed",
                            "branch_name": "automator/demo-task-20260410t103000z",
                            "attempts": 1,
                            "log_path": str(task_root / "events.jsonl"),
                            "summary_path": str(task_root / "task-summary.json"),
                            "pull_request_status": "failed",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            backlog = service.get_backlog()
            pipeline = backlog["tasks"][0]["pipeline"]
            pull_request_stage = next(
                stage for stage in pipeline["stages"] if stage["id"] == "pull_request"
            )

            self.assertIn("[REDACTED]", pull_request_stage["detail"])
            self.assertNotIn("Y29kZXg6", pull_request_stage["detail"])
            self.assertNotIn("Y29kZXg6", pipeline["failure_detail"])

    def test_get_backlog_marks_publish_failures_as_warning_after_task_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            run_id = "20260410T104500Z"
            run_root = temp_path / "runs" / run_id
            task_root = run_root / "tasks" / "demo-task"
            task_root.mkdir(parents=True, exist_ok=True)
            (task_root / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T10:45:00+00:00",
                                "step": "task",
                                "status": "passed",
                                "message": "Validation passed for the task.",
                                "context": {
                                    "task_id": "demo-task",
                                    "title": "Demo task",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T10:45:05+00:00",
                                "step": "pull_request",
                                "status": "failed",
                                "message": "Pull request creation failed.",
                                "context": {"error": "Push rejected with HTTP 403"},
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
                        "task_id": "demo-task",
                        "title": "Demo task",
                        "status": "passed",
                        "branch_name": "automator/demo-task-20260410t104500z",
                        "attempts": 1,
                        "pull_request_status": "failed",
                    }
                ),
                encoding="utf-8",
            )
            (run_root / "summary.json").write_text(
                json.dumps(
                    [
                        {
                            "task_id": "demo-task",
                            "title": "Demo task",
                            "status": "passed",
                            "branch_name": "automator/demo-task-20260410t104500z",
                            "attempts": 1,
                            "log_path": str(task_root / "events.jsonl"),
                            "summary_path": str(task_root / "task-summary.json"),
                            "pull_request_status": "failed",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            backlog = service.get_backlog()
            pipeline = backlog["tasks"][0]["pipeline"]
            run = service.get_run(run_id)

            self.assertEqual(pipeline["overall_status"], "warning")
            self.assertEqual(pipeline["failure_stage"], "pull_request")
            self.assertEqual(run["status"], "warning")
            self.assertTrue(run["tasks"][0]["resume"]["available"])
            self.assertEqual(run["tasks"][0]["resume"]["stage"], "pull_request")
            self.assertEqual(run["tasks"][0]["workflow_state"]["next_resume_stage"], "pull_request")
            self.assertEqual(run["tasks"][0]["last_blocker"]["stage"], "pull_request")

    def test_get_backlog_auto_marks_task_done_when_pull_request_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            run_id = "20260410T151000Z"
            run_root = temp_path / "runs" / run_id
            task_root = run_root / "tasks" / "demo-task"
            task_root.mkdir(parents=True, exist_ok=True)
            (task_root / "events.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-10T15:10:00+00:00",
                        "step": "task",
                        "status": "passed",
                        "message": "Validation passed for the task.",
                        "context": {"task_id": "demo-task", "title": "Demo task"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (task_root / "task-summary.json").write_text(
                json.dumps(
                    {
                        "task_id": "demo-task",
                        "title": "Demo task",
                        "status": "passed",
                        "branch_name": "automator/demo-task",
                        "attempts": 1,
                        "pull_request_status": "passed",
                        "pull_request_url": "https://github.com/KiuboStudios/kiubo-app/pull/1",
                    }
                ),
                encoding="utf-8",
            )
            (run_root / "summary.json").write_text(
                json.dumps(
                    [
                        {
                            "task_id": "demo-task",
                            "title": "Demo task",
                            "status": "passed",
                            "branch_name": "automator/demo-task",
                            "attempts": 1,
                            "log_path": str(task_root / "events.jsonl"),
                            "summary_path": str(task_root / "task-summary.json"),
                            "pull_request_status": "passed",
                            "pull_request_url": "https://github.com/KiuboStudios/kiubo-app/pull/1",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            backlog = service.get_backlog()
            persisted = json.loads(backlog_path.read_text(encoding="utf-8"))

            self.assertTrue(backlog["tasks"][0]["reviewed"])
            self.assertTrue(persisted["tasks"][0]["reviewed"])

    def test_resume_run_task_relaunches_the_same_run_from_the_failed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
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
                                "retry_limit": 1,
                                "executor": {"type": "codex", "prompt": ""},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            run_id = "20260410T104500Z"
            run_root = temp_path / "runs" / run_id
            task_root = run_root / "tasks" / "demo-task"
            task_root.mkdir(parents=True, exist_ok=True)
            (task_root / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T10:45:00+00:00",
                                "step": "task",
                                "status": "passed",
                                "message": "Validation passed for the task.",
                                "context": {
                                    "task_id": "demo-task",
                                    "title": "Demo task",
                                    "branch_name": "automator/demo-task",
                                    "attempt": 1,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-10T10:45:05+00:00",
                                "step": "pull_request",
                                "status": "failed",
                                "message": "Pull request creation failed.",
                                "context": {"error": "Push rejected with HTTP 403"},
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
                        "task_id": "demo-task",
                        "title": "Demo task",
                        "status": "passed",
                        "branch_name": "automator/demo-task",
                        "attempts": 1,
                        "pull_request_status": "failed",
                    }
                ),
                encoding="utf-8",
            )
            (run_root / "summary.json").write_text(
                json.dumps(
                    [
                        {
                            "task_id": "demo-task",
                            "title": "Demo task",
                            "status": "passed",
                            "branch_name": "automator/demo-task",
                            "attempts": 1,
                            "log_path": str(task_root / "events.jsonl"),
                            "summary_path": str(task_root / "task-summary.json"),
                            "pull_request_status": "failed",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            process = Mock()
            process.poll.return_value = None

            with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}, clear=True):
                with patch.object(
                    service,
                    "get_backlog",
                    return_value={"tasks": [{"id": "demo-task", "title": "Demo task"}]},
                ):
                    with patch(
                        "automation.local_dashboard.service.subprocess.Popen",
                        return_value=process,
                    ) as popen:
                        resumed_run = service.resume_run_task(run_id=run_id, task_id="demo-task")

            self.assertTrue(resumed_run["is_running"])
            self.assertEqual(resumed_run["tasks"][0]["resume"]["stage"], "pull_request")
            self.assertEqual(
                resumed_run["tasks"][0]["resume_requests"][-1]["resume_stage"],
                "pull_request",
            )
            command = popen.call_args.kwargs["args"] if "args" in popen.call_args.kwargs else popen.call_args.args[0]
            self.assertIn("--resume-run-id", command)
            self.assertIn("--resume-from", command)
            self.assertIn("pull_request", command)
            self.assertEqual(service._active_runs[run_id].task_ids, ["demo-task"])
            request_log = (task_root / "resume-requests.jsonl").read_text(encoding="utf-8")
            self.assertIn('"resume_stage": "pull_request"', request_log)

    def test_start_run_requires_github_token_in_dashboard_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            with patch.object(service.git_repo, "current_branch", return_value="main"):
                with patch.object(service.git_repo, "has_uncommitted_changes", return_value=False):
                    with patch.dict(os.environ, {}, clear=True):
                        with self.assertRaisesRegex(ValueError, "restart `python3 automation/dashboard.py`"):
                            service.start_run(task_ids=["demo-task"])

    def test_upsert_and_delete_task_updates_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            saved = service.upsert_task(
                {
                    "id": "demo-task",
                    "title": "Demo task",
                    "description": "Task from dashboard",
                    "test_command": "python3 -c \"print('ok')\"",
                    "base_branch": "main",
                    "working_directory": ".",
                    "retry_limit": 2,
                    "executor": {"type": "codex", "prompt": ""},
                }
            )

            self.assertEqual(saved["id"], "demo-task")
            self.assertEqual(saved["test_command"], ["python3", "-c", "print('ok')"])

            backlog = service.get_backlog()
            self.assertEqual(len(backlog["tasks"]), 1)
            self.assertEqual(backlog["tasks"][0]["id"], "demo-task")

            service.delete_task("demo-task")
            self.assertEqual(service.get_backlog()["tasks"], [])

    def test_upsert_task_preserves_reviewed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                                "description": "Reviewed already",
                                "reviewed": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            saved = service.upsert_task(
                {
                    "id": "demo-task",
                    "title": "Demo task",
                    "description": "Edited from dashboard",
                    "test_command": "",
                    "base_branch": "main",
                    "working_directory": ".",
                    "retry_limit": 2,
                    "executor": {"type": "codex", "prompt": ""},
                }
            )

            self.assertTrue(saved["reviewed"])
            self.assertTrue(service.get_backlog()["tasks"][0]["reviewed"])

    def test_set_task_reviewed_moves_task_between_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            reviewed = service.set_task_reviewed("demo-task", True)
            self.assertTrue(reviewed["reviewed"])
            self.assertTrue(service.get_backlog()["tasks"][0]["reviewed"])

            active = service.set_task_reviewed("demo-task", False)
            self.assertFalse(active["reviewed"])
            self.assertFalse(service.get_backlog()["tasks"][0]["reviewed"])

    def test_start_run_rejects_backlog_with_only_reviewed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "reviewed-task",
                                "title": "Reviewed task",
                                "reviewed": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            with patch.object(service.git_repo, "current_branch", return_value="main"):
                with patch.object(service.git_repo, "has_uncommitted_changes", return_value=False):
                    with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}, clear=True):
                        with self.assertRaisesRegex(ValueError, "No backlog tasks are ready to run"):
                            service.start_run()

    def test_upsert_task_allows_empty_test_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            saved = service.upsert_task(
                {
                    "id": "task-without-tests",
                    "title": "Task without tests",
                    "description": "Dashboard task",
                    "test_command": "",
                    "base_branch": "main",
                    "working_directory": ".",
                    "retry_limit": 2,
                    "executor": {"type": "codex", "prompt": ""},
                }
            )

            self.assertEqual(saved["test_command"], [])

    def test_get_backlog_exposes_reusable_branch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
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
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            with patch.object(
                service.git_repo,
                "find_worktree_by_branch",
                return_value=GitWorktree(
                    path=(temp_path / "worktrees" / "demo-task").resolve(),
                    branch_name="automator/demo-task",
                ),
            ):
                with patch.object(service.git_repo, "branch_exists", return_value=True):
                    backlog = service.get_backlog()

            branch = backlog["tasks"][0]["branch"]
            self.assertEqual(branch["name"], "automator/demo-task")
            self.assertTrue(branch["exists_locally"])
            self.assertTrue(branch["checked_out"])
            self.assertEqual(branch["status"], "ready")

    def test_cleanup_merged_task_branches_removes_clean_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )
            worktree = GitWorktree(
                path=(temp_path / "worktrees" / "demo-task").resolve(),
                branch_name="automator/demo-task",
            )

            with patch.object(service, "_merged_task_branch_names", return_value=["automator/demo-task"]):
                with patch.object(service.git_repo, "find_worktree_by_branch", return_value=worktree):
                    with patch.object(service.git_repo, "has_uncommitted_changes", return_value=False):
                        with patch.object(service.git_repo, "remove_worktree") as remove_worktree:
                            with patch.object(service.git_repo, "delete_branch") as delete_branch:
                                result = service.cleanup_merged_task_branches()

            self.assertEqual(result["deleted_branches"], ["automator/demo-task"])
            self.assertEqual(result["removed_worktrees"], [str(worktree.path)])
            self.assertEqual(result["skipped"], [])
            remove_worktree.assert_called_once_with(worktree.path, force=False)
            delete_branch.assert_called_once_with("automator/demo-task")

    def test_cleanup_merged_task_branches_skips_dirty_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )
            worktree = GitWorktree(
                path=(temp_path / "worktrees" / "demo-task").resolve(),
                branch_name="automator/demo-task",
            )

            with patch.object(service, "_merged_task_branch_names", return_value=["automator/demo-task"]):
                with patch.object(service.git_repo, "find_worktree_by_branch", return_value=worktree):
                    with patch.object(service.git_repo, "has_uncommitted_changes", return_value=True):
                        with patch.object(service.git_repo, "remove_worktree") as remove_worktree:
                            with patch.object(service.git_repo, "delete_branch") as delete_branch:
                                result = service.cleanup_merged_task_branches()

            self.assertEqual(result["deleted_branches"], [])
            self.assertEqual(len(result["skipped"]), 1)
            self.assertIn("uncommitted changes", result["skipped"][0]["reason"])
            remove_worktree.assert_not_called()
            delete_branch.assert_not_called()

    def test_cleanup_merged_task_branches_requires_no_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )
            service._active_runs["demo"] = Mock()
            service._active_runs["demo"].is_running.return_value = True

            with self.assertRaisesRegex(ValueError, "active runs to finish"):
                service.cleanup_merged_task_branches()

    def test_clear_runs_removes_run_directories_and_finished_run_handles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runs_root = temp_path / "runs"
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=runs_root,
                worktrees_root=temp_path / "worktrees",
            )
            run_ids = ["20260410T100000Z", "20260410T101000Z"]
            for run_id in run_ids:
                run_root = runs_root / run_id
                run_root.mkdir(parents=True, exist_ok=True)
                (run_root / "runner.stdout.log").write_text("ok\n", encoding="utf-8")
            (runs_root / ".gitkeep").write_text("", encoding="utf-8")

            finished_run = Mock()
            finished_run.is_running.return_value = False
            service._active_runs[run_ids[0]] = finished_run

            result = service.clear_runs()

            self.assertEqual(result["deleted_run_count"], 2)
            self.assertEqual(result["deleted_run_ids"], sorted(run_ids))
            self.assertFalse((runs_root / run_ids[0]).exists())
            self.assertFalse((runs_root / run_ids[1]).exists())
            self.assertTrue((runs_root / ".gitkeep").exists())
            self.assertEqual(service._active_runs, {})

    def test_clear_runs_requires_no_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )
            service._active_runs["demo"] = Mock()
            service._active_runs["demo"].is_running.return_value = True

            with self.assertRaisesRegex(ValueError, "active runs to finish"):
                service.clear_runs()

    def test_get_run_reads_task_summaries_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            run_id = "20260409T150000Z"
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=temp_path / "backlog.json",
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            run_root = temp_path / "runs" / run_id
            task_root = run_root / "tasks" / "demo-task"
            task_root.mkdir(parents=True, exist_ok=True)
            (run_root / "runner.stdout.log").write_text("runner ok\n", encoding="utf-8")
            (run_root / "runner.stderr.log").write_text("", encoding="utf-8")
            (task_root / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:00:00+00:00",
                                "step": "task",
                                "status": "started",
                                "message": "Starting task processing.",
                                "context": {
                                    "task_id": "demo-task",
                                    "title": "Demo task",
                                    "branch_name": "automator/demo-task-20260409t150000z",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:00:01+00:00",
                                "step": "branch",
                                "status": "completed",
                                "message": "Created task branch and worktree.",
                                "context": {
                                    "branch_name": "automator/demo-task-20260409t150000z",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:00:03+00:00",
                                "step": "code",
                                "status": "completed",
                                "message": "Completed code generation and application step.",
                                "context": {"attempt": 1},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:00:05+00:00",
                                "step": "tests",
                                "status": "completed",
                                "message": "Finished task validation command.",
                                "context": {"attempt": 1},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:00:06+00:00",
                                "step": "pull_request",
                                "status": "completed",
                                "message": "Created GitHub pull request.",
                                "context": {
                                    "pr_url": "https://github.com/KiuboStudios/kiubo-app/pull/1",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:00:07+00:00",
                                "step": "review",
                                "status": "current",
                                "message": "Waiting for human review.",
                                "context": {},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:00:10+00:00",
                                "step": "task",
                                "status": "passed",
                                "message": "Validation passed for the task.",
                                "context": {"attempt": 1},
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
                        "task_id": "demo-task",
                        "title": "Demo task",
                        "status": "passed",
                        "branch_name": "automator/demo-task-20260409t150000z",
                        "attempts": 1,
                        "pull_request_status": "passed",
                        "pull_request_url": "https://github.com/KiuboStudios/kiubo-app/pull/1",
                    }
                ),
                encoding="utf-8",
            )
            (task_root / "attempt-01-tests.stdout.log").write_text("ok\n", encoding="utf-8")
            (task_root / "attempt-01-tests.stderr.log").write_text("", encoding="utf-8")
            (run_root / "summary.json").write_text(
                json.dumps(
                    [
                        {
                            "task_id": "demo-task",
                            "title": "Demo task",
                            "status": "passed",
                            "branch_name": "automator/demo-task-20260409t150000z",
                            "attempts": 1,
                            "log_path": str(task_root / "events.jsonl"),
                            "summary_path": str(task_root / "task-summary.json"),
                            "pull_request_status": "passed",
                            "pull_request_url": "https://github.com/KiuboStudios/kiubo-app/pull/1",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            run = service.get_run(run_id)

            self.assertEqual(run["status"], "passed")
            self.assertEqual(run["tasks"][0]["task_id"], "demo-task")
            self.assertEqual(run["tasks"][0]["logs"]["stdout"], "ok\n")
            self.assertEqual(run["runner_stdout"], "runner ok\n")
            self.assertEqual(run["tasks"][0]["pipeline"]["overall_status"], "passed")
            self.assertEqual(run["tasks"][0]["pipeline"]["stages"][2]["status"], "passed")
            self.assertEqual(run["tasks"][0]["pipeline"]["stages"][4]["status"], "passed")
            self.assertEqual(run["tasks"][0]["pipeline"]["stages"][5]["status"], "passed")
            self.assertEqual(run["tasks"][0]["pull_request_url"], "https://github.com/KiuboStudios/kiubo-app/pull/1")
            self.assertFalse(run["tasks"][0]["resume"]["available"])
            self.assertEqual(run["tasks"][0]["resume"]["stage"], "")

    def test_get_backlog_marks_the_stage_where_a_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backlog_path = temp_path / "backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "demo-task",
                                "title": "Demo task",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = DashboardService(
                repo_root=temp_path,
                backlog_path=backlog_path,
                runs_root=temp_path / "runs",
                worktrees_root=temp_path / "worktrees",
            )

            run_id = "20260409T151000Z"
            run_root = temp_path / "runs" / run_id
            task_root = run_root / "tasks" / "demo-task"
            task_root.mkdir(parents=True, exist_ok=True)
            (task_root / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:10:00+00:00",
                                "step": "task",
                                "status": "started",
                                "message": "Starting task processing.",
                                "context": {
                                    "task_id": "demo-task",
                                    "title": "Demo task",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:10:01+00:00",
                                "step": "branch",
                                "status": "completed",
                                "message": "Created task branch and worktree.",
                                "context": {},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:10:03+00:00",
                                "step": "code",
                                "status": "completed",
                                "message": "Completed code generation and application step.",
                                "context": {"attempt": 1},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:10:05+00:00",
                                "step": "tests",
                                "status": "failed",
                                "message": "Finished task validation command.",
                                "context": {
                                    "attempt": 1,
                                    "returncode": 1,
                                    "stderr_path": "attempt-01-tests.stderr.log",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-09T15:10:06+00:00",
                                "step": "task",
                                "status": "failed",
                                "message": "Validation failed and retry budget is exhausted.",
                                "context": {"attempt": 1, "feedback": "Tests failed"},
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
                        "task_id": "demo-task",
                        "title": "Demo task",
                        "status": "failed",
                        "branch_name": "automator/demo-task-20260409t151000z",
                        "attempts": 1,
                        "pull_request_status": "skipped",
                    }
                ),
                encoding="utf-8",
            )
            (run_root / "summary.json").write_text(
                json.dumps(
                    [
                        {
                            "task_id": "demo-task",
                            "title": "Demo task",
                            "status": "failed",
                            "branch_name": "automator/demo-task-20260409t151000z",
                            "attempts": 1,
                            "log_path": str(task_root / "events.jsonl"),
                            "summary_path": str(task_root / "task-summary.json"),
                            "pull_request_status": "skipped",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            backlog = service.get_backlog()
            pipeline = backlog["tasks"][0]["pipeline"]

            self.assertEqual(pipeline["overall_status"], "failed")
            self.assertEqual(pipeline["failure_stage"], "tests")
            self.assertEqual(pipeline["summary"], "Failed at Tests")


if __name__ == "__main__":
    unittest.main()
