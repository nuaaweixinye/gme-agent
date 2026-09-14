from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from dataclasses import asdict
import hmac
import json

from ..services.orchestrator import JobAlreadyActiveError, Orchestrator
from ..services.interface_catalog_service import (
    list_selectable_interface_catalogs,
    selectable_interface_catalog,
)
from ..settings.config import AgentConfig, config_from_json, load_config, save_config
from ..settings.options import load_config_options
from ..settings.validation import validate_config
from ..storage.db import AgentDb


MIN_API_TOKEN_LENGTH = 32
LOOPBACK_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1"}
SUPPORTED_REASONING_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]


def load_harness_model_options(config: AgentConfig) -> dict[str, Any]:
    model = str(config.model or "").strip() or "deepseek-v4-flash"
    return {
        "models": [
            {
                "id": model,
                "display_name": model,
                "description": "当前配置的 DeepSeek Harness 模型",
                "default_reasoning_effort": str(config.reasoning_effort or ""),
                "supported_reasoning_efforts": SUPPORTED_REASONING_EFFORTS,
            }
        ],
        "reasoning_efforts": SUPPORTED_REASONING_EFFORTS,
        "error": "",
    }


def api_token_matches(expected: str, authorization: str) -> bool:
    scheme, separator, supplied = str(authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(str(expected), supplied.strip())


def is_allowed_origin(origin: str) -> bool:
    value = str(origin or "").strip()
    if not value:
        return True
    if value == "null":
        return True
    parsed = urlparse(value)
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_ORIGIN_HOSTS


class ServerState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.db = AgentDb(self.config.database_path)
        self.orchestrator = Orchestrator(self.config, self.db)

    def update_config(self, data: dict[str, Any]) -> AgentConfig:
        self.config = config_from_json(data, self.config)
        save_config(self.config_path, self.config)
        self.db = AgentDb(self.config.database_path)
        self.orchestrator = Orchestrator(self.config, self.db)
        return self.config


def create_server(config_path: Path, host: str, port: int, api_token: str) -> ThreadingHTTPServer:
    if len(str(api_token or "")) < MIN_API_TOKEN_LENGTH:
        raise RuntimeError(f"GME_AGENT_API_TOKEN must contain at least {MIN_API_TOKEN_LENGTH} characters.")
    state = ServerState(config_path)

    class Handler(ApiHandler):
        pass

    Handler.server_state = state
    Handler.api_token = api_token
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.agent_state = state  # type: ignore[attr-defined]
    return httpd


def run_server(config_path: Path, host: str, port: int, api_token: str) -> None:
    httpd = create_server(config_path, host, port, api_token)

    print(f"GME Test Agent backend listening on http://{host}:{port}")
    httpd.serve_forever()


class ApiHandler(BaseHTTPRequestHandler):
    server_state: ServerState
    api_token: str

    def do_OPTIONS(self) -> None:
        if not self._origin_is_allowed():
            self._send_error(HTTPStatus.FORBIDDEN, "Browser origin is not allowed.")
            return
        self._send_json({"ok": True})

    def do_GET(self) -> None:
        if not self._authorize():
            return
        try:
            path, query = self._path()
            if path == "/api/health":
                self._send_json({"ok": True, "authenticated": True})
            elif path == "/api/config":
                self._send_json(asdict(self.server_state.config))
            elif path == "/api/validate":
                self._send_json(validate_config(self.server_state.config))
            elif path == "/api/options":
                self._send_json(load_config_options(self.server_state.config))
            elif path == "/api/models":
                self._send_json(load_harness_model_options(self.server_state.config))
            elif path == "/api/interface-catalogs":
                self._send_json(list_selectable_interface_catalogs())
            elif path.startswith("/api/interface-catalogs/"):
                module = path.split("/")[3]
                self._send_json(selectable_interface_catalog(module))
            elif path == "/api/jobs":
                self._send_json({"jobs": self.server_state.orchestrator.list_jobs_with_runtime_state()})
            elif path == "/api/failures":
                self._send_json({"failures": self.server_state.db.list_failures()})
            elif path.startswith("/api/failures/") and path.endswith("/observations"):
                failure_id = path.split("/")[3]
                self._send_json(
                    {"observations": self.server_state.db.list_failure_observations(failure_id)}
                )
            elif path.startswith("/api/failures/"):
                failure_id = path.split("/")[3]
                self._send_json(self.server_state.db.get_failure(failure_id))
            elif path.startswith("/api/jobs/") and path.endswith("/test-results"):
                job_id = path.split("/")[3]
                self._send_json(
                    {"results": self.server_state.db.list_test_case_results(job_id)}
                )
            elif path.startswith("/api/jobs/") and path.endswith("/events"):
                job_id = path.split("/")[3]
                after = int(query.get("after", ["0"])[0])
                self._send_json({"events": self.server_state.db.list_events(job_id, after)})
            elif path.startswith("/api/jobs/") and path.endswith("/artifacts"):
                job_id = path.split("/")[3]
                self._send_json(self._job_artifacts(job_id))
            elif path.startswith("/api/jobs/"):
                job_id = path.split("/")[3]
                self._send_json(self.server_state.orchestrator.job_with_runtime_state(job_id))
            else:
                self._send_error(HTTPStatus.NOT_FOUND, f"Unknown endpoint: {path}")
        except JobAlreadyActiveError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        if not self._authorize():
            return
        try:
            path, _ = self._path()
            data = self._read_json()
            if path == "/api/config":
                config = self.server_state.update_config(data)
                self._send_json(asdict(config))
            elif path == "/api/jobs/test-generation/retry":
                result = self.server_state.orchestrator.retry_test_generation_jobs(
                    list(data.get("job_ids") or [])
                )
                self._send_json(result, HTTPStatus.ACCEPTED)
            elif path == "/api/jobs/test-generation/batch":
                result = self.server_state.orchestrator.create_test_generation_jobs_batch(
                    module=str(data.get("module") or "gme"),
                    interface_ids=data.get("interface_ids"),
                    batch_size=int(data.get("batch_size") or 5),
                )
                self._send_json(result, HTTPStatus.ACCEPTED)
            elif path == "/api/jobs/test-generation":
                job = self.server_state.orchestrator.create_test_generation_job(
                    module=str(data.get("module") or "gme"),
                    api_name=str(data.get("api_name") or ""),
                    interface_ids=data.get("interface_ids"),
                )
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "extend-tests"):
                job = self.server_state.orchestrator.extend_test_generation_job(
                    job_id,
                    api_name=str(data.get("api_name") or ""),
                    interface_ids=data.get("interface_ids"),
                )
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "retry-tests"):
                job = self.server_state.orchestrator.retry_test_generation_job(job_id)
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "run-tests"):
                job = self.server_state.orchestrator.run_tests_for_job(
                    job_id,
                    gtest_filter=str(data.get("gtest_filter") or "*"),
                )
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "build"):
                job = self.server_state.orchestrator.build_job(job_id)
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "memory-audit"):
                job = self.server_state.orchestrator.run_memory_audit_for_job(
                    job_id,
                    gtest_filter=str(data.get("gtest_filter") or "*"),
                )
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "create-pr"):
                job = self.server_state.orchestrator.create_pr_for_job(job_id)
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "skip-pr"):
                job = self.server_state.orchestrator.create_skip_pr_for_job(job_id)
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "selected-tests-pr"):
                job = self.server_state.orchestrator.create_selected_tests_pr_for_job(
                    job_id,
                    list(data.get("tests") or []),
                )
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := (_match_job_action(path, "generated-tests", "remove") or _match_job_action(path, "generated-tests", "delete")):
                job = self.server_state.orchestrator.delete_generated_tests_for_job(
                    job_id,
                    list(data.get("tests") or []),
                )
                self._send_json(job)
            elif job_id := _match_job_action(path, "cleanup"):
                job = self.server_state.orchestrator.cleanup_job_worktree(job_id)
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif job_id := _match_job_action(path, "delete"):
                result = self.server_state.orchestrator.delete_job(
                    job_id,
                    cleanup_worktree=bool(data.get("cleanup_worktree", True)),
                    delete_artifacts=bool(data.get("delete_artifacts", True)),
                )
                self._send_json(result)
            elif path == "/api/fix-jobs":
                job = self.server_state.orchestrator.create_fix_job_for_failures(
                    list(data.get("failure_ids") or [])
                )
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif failure_id := _match_failure_action(path, "fix"):
                failure_ids = list(data.get("failure_ids") or [])
                job = self.server_state.orchestrator.create_fix_job_for_failures(
                    failure_ids or [failure_id]
                )
                self._send_json(job, HTTPStatus.ACCEPTED)
            elif failure_id := _match_failure_action(path, "status"):
                failure = self.server_state.orchestrator.update_failure_status(
                    failure_id,
                    str(data.get("status") or "open"),
                )
                self._send_json(failure)
            else:
                self._send_error(HTTPStatus.NOT_FOUND, f"Unknown endpoint: {path}")
        except JobAlreadyActiveError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _path(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def _authorize(self) -> bool:
        if not self._origin_is_allowed():
            self._send_error(HTTPStatus.FORBIDDEN, "Browser origin is not allowed.")
            return False
        if not api_token_matches(self.api_token, self.headers.get("Authorization") or ""):
            self._send_error(HTTPStatus.UNAUTHORIZED, "A valid GME Test Agent API token is required.")
            return False
        return True

    def _origin_is_allowed(self) -> bool:
        return is_allowed_origin(self.headers.get("Origin") or "")

    def _send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin") or ""
        if origin and is_allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status)

    def _job_artifacts(self, job_id: str) -> dict[str, Any]:
        job = self.server_state.db.get_job(job_id)
        metadata = job.get("metadata") or {}
        artifact_dir = Path(metadata.get("artifact_dir") or Path(self.server_state.config.artifact_root) / job_id)
        names = [
            "manifest.json",
            "diff.patch",
            "agent_result.txt",
            "agent_extend_result.txt",
            "agent_skip_result.txt",
            "gtest_output.txt",
            "gtest_output_after_skip.txt",
            "gtest_output_selected_pr.txt",
            "memory_audit_results.json",
            "memory_audit_results.csv",
            "memory_audit_output.txt",
            "gtest_reproduce_before_fix.txt",
            "gtest_verify_after_fix.txt",
            "gtest_full_after_fix.txt",
            "format_check_output.txt",
            "test_generation_prompt.md",
            "test_generation_extend_prompt.md",
            "bug_fix_prompt.md",
            "skip_prompt.md",
        ]
        files = []
        contents: dict[str, str] = {}
        if artifact_dir.exists():
            for path in sorted(artifact_dir.iterdir()):
                if path.is_file():
                    stat = path.stat()
                    files.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime})
            for name in names:
                path = artifact_dir / name
                if path.exists() and path.is_file():
                    contents[name] = path.read_text(encoding="utf-8", errors="replace")
        worktree_path = str(job.get("worktree_path") or "")
        if worktree_path:
            notes_dir = Path(worktree_path) / ".gme-agent"
            if notes_dir.exists():
                for path in sorted([*notes_dir.glob("*.md"), *notes_dir.glob("*.json")]):
                    key = f".gme-agent/{path.name}"
                    files.append({"name": key, "size": path.stat().st_size})
                    contents[key] = path.read_text(encoding="utf-8", errors="replace")
        return {
            "artifact_dir": str(artifact_dir),
            "files": files,
            "contents": contents,
        }


def _match_job_action(path: str, *action: str) -> str:
    parts = [part for part in path.split("/") if part]
    expected_len = 3 + len(action)
    if len(parts) != expected_len or parts[:2] != ["api", "jobs"] or parts[3:] != list(action):
        return ""
    return parts[2]


def _match_failure_action(path: str, *action: str) -> str:
    parts = [part for part in path.split("/") if part]
    expected_len = 3 + len(action)
    if len(parts) != expected_len or parts[:2] != ["api", "failures"] or parts[3:] != list(action):
        return ""
    return parts[2]
