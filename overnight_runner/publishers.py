from __future__ import annotations

import json
import os
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

from overnight_runner.git_ops import GitCommandError, GitRepository
from overnight_runner.models import EventLogger, PullRequestResult, TaskDefinition


class NoopPullRequestPublisher:
    def publish(
        self,
        task: TaskDefinition,
        branch_name: str,
        worktree_path: Path,
        logger: EventLogger,
    ) -> PullRequestResult:
        logger.log(
            "pull_request",
            "skipped",
            "PR publishing is disabled by the current publisher implementation.",
            branch_name=branch_name,
            task_id=task.id,
        )
        return PullRequestResult(status="skipped")


class GitHubPullRequestPublisher:
    def __init__(
        self,
        repo_root: Path,
        remote_name: str = "origin",
        token_env_var: str = "GITHUB_TOKEN",
        draft: bool = True,
    ) -> None:
        self.repo_root = repo_root
        self.remote_name = remote_name
        self.token_env_var = token_env_var
        self.draft = draft
        self.git = GitRepository(repo_root)

    def publish(
        self,
        task: TaskDefinition,
        branch_name: str,
        worktree_path: Path,
        logger: EventLogger,
    ) -> PullRequestResult:
        token = os.environ.get(self.token_env_var, "").strip()
        if not token:
            raise RuntimeError(
                f"{self.token_env_var} is not set. "
                "Set a fine-grained GitHub personal access token before running automation."
            )

        remote = self.git.resolve_github_remote(self.remote_name, cwd=worktree_path)
        logger.log(
            "push",
            "started",
            "Pushing the task branch to GitHub.",
            branch_name=branch_name,
            repository=remote.repository_full_name,
        )
        try:
            self.git.push_branch_with_token(
                cwd=worktree_path,
                branch_name=branch_name,
                token=token,
                remote_name=self.remote_name,
            )
        except GitCommandError as push_error:
            if not self._should_retry_with_remote(remote):
                raise
            logger.log(
                "push",
                "retrying",
                "Token-authenticated push failed. Retrying with the configured git remote credentials.",
                branch_name=branch_name,
                repository=remote.repository_full_name,
                error=str(push_error),
            )
            self.git.push_branch(
                cwd=worktree_path,
                branch_name=branch_name,
                remote_name=self.remote_name,
            )
        logger.log(
            "push",
            "completed",
            "Pushed the task branch to GitHub.",
            branch_name=branch_name,
            repository=remote.repository_full_name,
        )

        payload = {
            "title": task.title,
            "head": branch_name,
            "base": task.base_branch,
            "body": self._pull_request_body(task, branch_name),
            "draft": self.draft,
        }
        endpoint = f"{remote.api_base_url}/repos/{remote.repository_full_name}/pulls"
        req = request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        try:
            with request.urlopen(req) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as http_error:
            body = http_error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                "GitHub pull request creation failed. "
                f"HTTP {http_error.code}: {body}"
            ) from http_error

        pr_url = str(response_payload.get("html_url", ""))
        pr_number = int(response_payload.get("number", 0))
        logger.log(
            "pull_request",
            "completed",
            "Created GitHub pull request.",
            branch_name=branch_name,
            repository=remote.repository_full_name,
            pr_url=pr_url,
            pr_number=pr_number,
            draft=self.draft,
        )
        return PullRequestResult(status="passed", url=pr_url, number=pr_number)

    def _should_retry_with_remote(self, remote) -> bool:
        remote_url = remote.fetch_url.strip()
        if remote_url.startswith("git@"):
            return True
        parsed = urlparse(remote_url)
        return parsed.scheme == "ssh"

    def _pull_request_body(self, task: TaskDefinition, branch_name: str) -> str:
        validation = (
            " ".join(task.test_command) if task.test_command else "No validation command configured"
        )
        parts = [
            "## Automation Summary",
            "",
            f"- Task ID: `{task.id}`",
            f"- Base branch: `{task.base_branch}`",
            f"- Branch: `{branch_name}`",
            f"- Validation: `{validation}`",
            "",
            "## Task Description",
            "",
            task.description or "No task description was provided.",
            "",
            "_Generated automatically with Codex and the local overnight runner._",
        ]
        return "\n".join(parts)
