from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import unquote, urlparse

from automation.local_dashboard.service import DashboardService


def _static_file(path: Path, content_type: str) -> Tuple[bytes, str]:
    return path.read_bytes(), content_type


class DashboardRequestHandler(BaseHTTPRequestHandler):
    service_factory: Optional[Callable[[], DashboardService]] = None
    static_root: Optional[Path] = None

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._serve_static("index.html", "text/html; charset=utf-8")
                return
            if self._serve_requested_static(parsed.path):
                return
            if parsed.path.startswith("/api/tasks/") and "/attachments/" in parsed.path:
                path_parts = [part for part in parsed.path.split("/") if part]
                if (
                    len(path_parts) == 6
                    and path_parts[0] == "api"
                    and path_parts[1] == "tasks"
                    and path_parts[3] == "attachments"
                    and path_parts[5] == "content"
                ):
                    task_id = unquote(path_parts[2]).strip()
                    attachment_id = unquote(path_parts[4]).strip()
                    attachment = self._service().read_task_attachment(task_id, attachment_id)
                    self._send_bytes(
                        attachment["body"],
                        attachment["content_type"],
                        filename=attachment["filename"],
                    )
                    return
            if parsed.path == "/api/overview":
                self._send_json(self._service().get_overview())
                return
            if parsed.path == "/api/backlog":
                self._send_json(self._service().get_backlog())
                return
            if parsed.path == "/api/runs":
                self._send_json(self._service().list_runs())
                return
            if parsed.path.startswith("/api/runs/"):
                run_id = unquote(parsed.path.split("/api/runs/", 1)[1]).strip("/")
                self._send_json(self._service().get_run(run_id))
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            payload = self._read_json()
            if parsed.path == "/api/tasks":
                task = self._service().upsert_task(payload)
                self._send_json({"task": task}, status=HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/reviewed"):
                path_parts = [part for part in parsed.path.split("/") if part]
                if len(path_parts) != 4 or path_parts[0] != "api" or path_parts[1] != "tasks":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                task_id = unquote(path_parts[2]).strip()
                task = self._service().set_task_reviewed(
                    task_id=task_id,
                    reviewed=bool(payload.get("reviewed")),
                )
                self._send_json({"task": task}, status=HTTPStatus.CREATED)
                return
            if parsed.path == "/api/runs":
                run = self._service().start_run(
                    task_ids=payload.get("task_ids") or None,
                    max_tasks=payload.get("max_tasks"),
                )
                self._send_json(run, status=HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/resume"):
                path_parts = [part for part in parsed.path.split("/") if part]
                if len(path_parts) != 5 or path_parts[0] != "api" or path_parts[1] != "runs":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                run_id = unquote(path_parts[2]).strip()
                task_id = unquote(path_parts[3]).strip()
                run = self._service().resume_run_task(run_id=run_id, task_id=task_id)
                self._send_json(run, status=HTTPStatus.CREATED)
                return
            if parsed.path == "/api/repo/remove-merged-branches":
                result = self._service().cleanup_merged_task_branches()
                self._send_json(result)
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/runs":
                result = self._service().clear_runs()
                self._send_json(result)
                return
            if parsed.path.startswith("/api/tasks/"):
                task_id = unquote(parsed.path.split("/api/tasks/", 1)[1]).strip("/")
                self._service().delete_task(task_id)
                self._send_json({"deleted": task_id})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args) -> None:
        return

    def _service(self) -> DashboardService:
        assert self.service_factory is not None
        return self.service_factory()

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        return json.loads(raw)

    def _serve_requested_static(self, request_path: str) -> bool:
        assert self.static_root is not None
        name = request_path.lstrip("/")
        if not name:
            return False

        static_root = self.static_root.resolve()
        path = (static_root / name).resolve()
        if static_root not in path.parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return True
        if not path.is_file():
            return False

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type = f"{content_type}; charset=utf-8"
        self._serve_static_path(path, content_type)
        return True

    def _serve_static(self, name: str, content_type: str) -> None:
        assert self.static_root is not None
        path = self.static_root / name
        self._serve_static_path(path, content_type)

    def _serve_static_path(self, path: Path, content_type: str) -> None:
        body, detected_type = _static_file(path, content_type)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", detected_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        filename: str = "",
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        if filename:
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str,
    port: int,
    service: DashboardService,
    static_root: Path,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundDashboardRequestHandler",
        (DashboardRequestHandler,),
        {
            "service_factory": staticmethod(lambda: service),
            "static_root": static_root,
        },
    )
    return ThreadingHTTPServer((host, port), handler)
