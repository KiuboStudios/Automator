from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.local_dashboard.server import create_server
from automation.local_dashboard.service import DashboardService


def _default_worktrees_root(repo_root: Path) -> Path:
    return Path(tempfile.gettempdir()) / "kiubo-automation-worktrees" / repo_root.name


def parse_args() -> argparse.Namespace:
    default_backlog = REPO_ROOT / "automation" / "backlog" / "tasks.json"
    default_runs_root = REPO_ROOT / "automation" / "runs"
    default_worktrees_root = _default_worktrees_root(REPO_ROOT)

    parser = argparse.ArgumentParser(description="Run the local automation dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="Path to the git repository root.")
    parser.add_argument("--backlog", default=str(default_backlog), help="Path to the backlog JSON file.")
    parser.add_argument("--runs-root", default=str(default_runs_root), help="Directory where run logs are stored.")
    parser.add_argument(
        "--worktrees-root",
        default=str(default_worktrees_root),
        help="Directory where disposable git worktrees are created.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service = DashboardService(
        repo_root=Path(args.repo_root).resolve(),
        backlog_path=Path(args.backlog).resolve(),
        runs_root=Path(args.runs_root).resolve(),
        worktrees_root=Path(args.worktrees_root).resolve(),
    )
    static_root = Path(__file__).resolve().parent / "local_dashboard" / "static"
    server = create_server(args.host, args.port, service, static_root)
    print(f"Automation dashboard available at http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
