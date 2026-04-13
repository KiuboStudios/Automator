from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse


def redact_sensitive_text(text: str) -> str:
    redacted = text
    redacted = re.sub(
        r"(AUTHORIZATION:\s*basic\s+)[^\s]+",
        r"\1[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        r"(Authorization:\s*Bearer\s+)[^\s]+",
        r"\1[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(r"github_pat_[A-Za-z0-9_]+", "[REDACTED_GITHUB_TOKEN]", redacted)
    return redacted


@dataclass
class GitCommandError(RuntimeError):
    message: str
    command: List[str]
    stderr: str

    def __str__(self) -> str:
        sanitized_command = [redact_sensitive_text(part) for part in self.command]
        sanitized_stderr = redact_sensitive_text(self.stderr)
        return f"{self.message}: {' '.join(sanitized_command)}\n{sanitized_stderr}".strip()


@dataclass(frozen=True)
class GitHubRemote:
    host: str
    owner: str
    repo: str
    remote_name: str
    fetch_url: str

    @property
    def repository_full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def https_url(self) -> str:
        return f"https://{self.host}/{self.repository_full_name}.git"

    @property
    def api_base_url(self) -> str:
        if self.host == "github.com":
            return "https://api.github.com"
        return f"https://{self.host}/api/v3"


@dataclass(frozen=True)
class GitWorktree:
    path: Path
    branch_name: str = ""


@dataclass(frozen=True)
class PreparedTaskWorktree:
    path: Path
    branch_name: str
    branch_created: bool
    worktree_created: bool
    reused_existing: bool


class GitRepository:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def _run(
        self,
        command: List[str],
        cwd: Optional[Path] = None,
        message: str = "Git command failed",
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            cwd=cwd or self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitCommandError(
                message=message,
                command=command,
                stderr=completed.stderr or completed.stdout,
            )
        return completed

    def has_uncommitted_changes(self, cwd: Optional[Path] = None) -> bool:
        command = ["git", "status", "--short"]
        completed = self._run(
            command,
            cwd=cwd,
            message="Failed to inspect repository status",
        )
        return bool(completed.stdout.strip())

    def current_branch(self, cwd: Optional[Path] = None) -> str:
        completed = self._run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            message="Failed to resolve current branch",
        )
        return completed.stdout.strip()

    def create_task_worktree(
        self,
        branch_name: str,
        base_branch: str,
        destination: Path,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "git",
            "worktree",
            "add",
            "-b",
            branch_name,
            str(destination),
            base_branch,
        ]
        self._run(command, message="Failed to create task worktree")
        return destination

    def list_merged_branches(self, base_ref: str, cwd: Optional[Path] = None) -> List[str]:
        completed = self._run(
            ["git", "branch", "--format=%(refname:short)", "--merged", base_ref],
            cwd=cwd,
            message="Failed to list merged branches",
        )
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

    def branch_exists(self, branch_name: str, cwd: Optional[Path] = None) -> bool:
        completed = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=cwd or self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return True
        if completed.returncode == 1:
            return False
        raise GitCommandError(
            message="Failed to inspect whether the task branch exists",
            command=["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            stderr=completed.stderr or completed.stdout,
        )

    def list_worktrees(self, cwd: Optional[Path] = None) -> List[GitWorktree]:
        completed = self._run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd,
            message="Failed to list git worktrees",
        )
        worktrees: List[GitWorktree] = []
        current_path: Optional[Path] = None
        current_branch = ""
        for line in completed.stdout.splitlines():
            if not line.strip():
                if current_path is not None:
                    worktrees.append(
                        GitWorktree(
                            path=current_path.resolve(),
                            branch_name=current_branch,
                        )
                    )
                current_path = None
                current_branch = ""
                continue

            if line.startswith("worktree "):
                current_path = Path(line.split(" ", 1)[1])
            elif line.startswith("branch refs/heads/"):
                current_branch = line.split("branch refs/heads/", 1)[1]

        if current_path is not None:
            worktrees.append(
                GitWorktree(
                    path=current_path.resolve(),
                    branch_name=current_branch,
                )
            )
        return worktrees

    def find_worktree_by_branch(
        self,
        branch_name: str,
        cwd: Optional[Path] = None,
    ) -> Optional[GitWorktree]:
        return next(
            (worktree for worktree in self.list_worktrees(cwd=cwd) if worktree.branch_name == branch_name),
            None,
        )

    def prepare_task_worktree(
        self,
        branch_name: str,
        base_branch: str,
        destination: Path,
    ) -> PreparedTaskWorktree:
        preferred_destination = destination.resolve()
        existing_worktree = self.find_worktree_by_branch(branch_name)
        if existing_worktree is not None:
            return PreparedTaskWorktree(
                path=existing_worktree.path,
                branch_name=branch_name,
                branch_created=False,
                worktree_created=False,
                reused_existing=True,
            )

        if preferred_destination.exists():
            try:
                existing_branch_name = self.current_branch(cwd=preferred_destination)
            except GitCommandError as error:
                raise GitCommandError(
                    message="Task worktree path already exists but is not a valid git worktree",
                    command=error.command,
                    stderr=(
                        f"Path `{preferred_destination}` must be cleaned up or converted back into a "
                        "git worktree before this task can resume."
                    ),
                ) from error
            if existing_branch_name == branch_name:
                return PreparedTaskWorktree(
                    path=preferred_destination,
                    branch_name=branch_name,
                    branch_created=False,
                    worktree_created=False,
                    reused_existing=True,
                )
            raise GitCommandError(
                message="Task worktree path is already in use by a different branch",
                command=["git", "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=(
                    f"Expected branch `{branch_name}` at `{preferred_destination}`, "
                    f"but found `{existing_branch_name}`."
                ),
            )

        preferred_destination.parent.mkdir(parents=True, exist_ok=True)
        if self.branch_exists(branch_name):
            self._run(
                ["git", "worktree", "add", str(preferred_destination), branch_name],
                message="Failed to attach the existing task branch to a worktree",
            )
            return PreparedTaskWorktree(
                path=preferred_destination,
                branch_name=branch_name,
                branch_created=False,
                worktree_created=True,
                reused_existing=False,
            )

        self.create_task_worktree(
            branch_name=branch_name,
            base_branch=base_branch,
            destination=preferred_destination,
        )
        return PreparedTaskWorktree(
            path=preferred_destination,
            branch_name=branch_name,
            branch_created=True,
            worktree_created=True,
            reused_existing=False,
        )

    def remove_worktree(self, destination: Path, force: bool = True) -> None:
        command = ["git", "worktree", "remove"]
        if force:
            command.append("--force")
        command.append(str(destination))
        self._run(command, message="Failed to remove task worktree")

    def delete_branch(
        self,
        branch_name: str,
        cwd: Optional[Path] = None,
        force: bool = False,
    ) -> None:
        command = ["git", "branch", "-D" if force else "-d", branch_name]
        self._run(command, cwd=cwd, message="Failed to delete task branch")

    def stage_all(self, cwd: Path) -> None:
        self._run(["git", "add", "--all"], cwd=cwd, message="Failed to stage task changes")

    def commit_all(self, cwd: Path, message: str) -> str:
        self._run(
            ["git", "commit", "-m", message],
            cwd=cwd,
            message="Failed to create a task commit",
        )
        return self.current_head(cwd)

    def current_head(self, cwd: Path) -> str:
        completed = self._run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            message="Failed to resolve HEAD",
        )
        return completed.stdout.strip()

    def count_commits_ahead(self, cwd: Path, base_ref: str) -> int:
        completed = self._run(
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            cwd=cwd,
            message="Failed to count commits ahead of the base branch",
        )
        return int(completed.stdout.strip() or "0")

    def get_remote_url(self, remote_name: str = "origin", cwd: Optional[Path] = None) -> str:
        completed = self._run(
            ["git", "remote", "get-url", remote_name],
            cwd=cwd,
            message=f"Failed to resolve remote `{remote_name}`",
        )
        return completed.stdout.strip()

    def resolve_github_remote(
        self,
        remote_name: str = "origin",
        cwd: Optional[Path] = None,
    ) -> GitHubRemote:
        remote_url = self.get_remote_url(remote_name=remote_name, cwd=cwd)
        ssh_match = re.match(
            r"^(?:ssh://)?git@(?P<host>[^/:]+)[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
            remote_url,
        )
        if ssh_match:
            return GitHubRemote(
                host=ssh_match.group("host"),
                owner=ssh_match.group("owner"),
                repo=ssh_match.group("repo"),
                remote_name=remote_name,
                fetch_url=remote_url,
            )

        parsed = urlparse(remote_url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.scheme in {"http", "https", "ssh"} and len(path_parts) >= 2:
            repo = path_parts[1]
            if repo.endswith(".git"):
                repo = repo[:-4]
            return GitHubRemote(
                host=parsed.hostname or "github.com",
                owner=path_parts[0],
                repo=repo,
                remote_name=remote_name,
                fetch_url=remote_url,
            )

        raise GitCommandError(
            message="Unsupported GitHub remote format",
            command=["git", "remote", "get-url", remote_name],
            stderr=remote_url,
        )

    def push_branch_with_token(
        self,
        cwd: Path,
        branch_name: str,
        token: str,
        remote_name: str = "origin",
    ) -> GitHubRemote:
        remote = self.resolve_github_remote(remote_name=remote_name, cwd=cwd)
        credentials = base64.b64encode(f"codex:{token}".encode("utf-8")).decode("ascii")
        extraheader = (
            f"http.https://{remote.host}/.extraheader="
            f"AUTHORIZATION: basic {credentials}"
        )
        command = [
            "git",
            "-c",
            extraheader,
            "push",
            "--set-upstream",
            remote.https_url,
            f"HEAD:refs/heads/{branch_name}",
        ]
        self._run(command, cwd=cwd, message="Failed to push the task branch to GitHub")
        return remote

    def push_branch(
        self,
        cwd: Path,
        branch_name: str,
        remote_name: str = "origin",
    ) -> GitHubRemote:
        remote = self.resolve_github_remote(remote_name=remote_name, cwd=cwd)
        command = [
            "git",
            "push",
            "--set-upstream",
            remote_name,
            f"HEAD:refs/heads/{branch_name}",
        ]
        self._run(command, cwd=cwd, message="Failed to push the task branch to GitHub")
        return remote
