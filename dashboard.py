from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_dashboard.server import create_server
from local_dashboard.service import DashboardService


def _default_backlog_path(repo_root: Path) -> Path:
    return repo_root / "tasks.json"


def _default_runs_root(repo_root: Path) -> Path:
    return repo_root / "automation" / "runs"


def _default_worktrees_root(repo_root: Path) -> Path:
    return Path(tempfile.gettempdir()) / "kiubo-automation-worktrees" / repo_root.name


def parse_args() -> argparse.Namespace:
    default_runner_entrypoint = REPO_ROOT / "main.py"

    parser = argparse.ArgumentParser(description="Run the local automation dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on.")
    parser.add_argument("--repo-root", default=str(Path.cwd()), help="Path to the target git repository root.")
    parser.add_argument(
        "--backlog",
        default=None,
        help="Path to the backlog JSON file. Defaults to <repo-root>/tasks.json.",
    )
    parser.add_argument(
        "--runs-root",
        default=None,
        help="Directory where run logs are stored. Defaults to <repo-root>/automation/runs.",
    )
    parser.add_argument(
        "--worktrees-root",
        default=None,
        help="Directory where disposable git worktrees are created. Defaults to /tmp/kiubo-automation-worktrees/<repo-name>.",
    )
    parser.add_argument(
        "--runner-entrypoint",
        default=str(default_runner_entrypoint),
        help="Path to the automation runner entrypoint script.",
    )

    parsed = parser.parse_args()
    repo_root = Path(parsed.repo_root).expanduser().resolve()
    if parsed.backlog is None:
        parsed.backlog = str(_default_backlog_path(repo_root))
    if parsed.runs_root is None:
        parsed.runs_root = str(_default_runs_root(repo_root))
    if parsed.worktrees_root is None:
        parsed.worktrees_root = str(_default_worktrees_root(repo_root))
    return parsed


def main() -> int:
    args = parse_args()
    service = DashboardService(
        repo_root=Path(args.repo_root).resolve(),
        backlog_path=Path(args.backlog).resolve(),
        runs_root=Path(args.runs_root).resolve(),
        worktrees_root=Path(args.worktrees_root).resolve(),
        runner_entrypoint=Path(args.runner_entrypoint).resolve(),
    )
    static_root = Path(__file__).resolve().parent / "local_dashboard" / "static"
    server = create_server(args.host, args.port, service, static_root)
    print(f"Automation dashboard available at http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
