from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.harness.runner import HarnessRunner  # noqa: E402
from gme_agent.settings.config import AgentConfig  # noqa: E402


IS_WINDOWS = sys.platform == "win32"
PWSH_TOOL = "pwsh"


def completed_response(text: str) -> dict[str, Any]:
    return {"kind": "text", "text": text}


def pwsh_tool_call(command: str) -> dict[str, Any]:
    return {
        "kind": "tool",
        "call_id": "gme-e2e-pwsh",
        "command": command,
        "description": "Run the scripted GME e2e file edit.",
    }


class ScriptedOpenAIEndpoint:
    """Loopback-only OpenAI-compatible SSE endpoint with scripted turns."""

    def __init__(self, turns: list[dict[str, Any]]):
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            endpoint: ScriptedOpenAIEndpoint

            def do_POST(self) -> None:  # noqa: N802
                endpoint = self.endpoint
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                endpoint.requests.append(
                    {
                        "authorization": self.headers.get("authorization"),
                        "body": body,
                    }
                )
                if not endpoint.turns:
                    raise AssertionError("scripted model endpoint received an unexpected extra request")
                turn = endpoint.turns.pop(0)
                chunks = (
                    _tool_call_chunks(
                        turn["call_id"],
                        PWSH_TOOL,
                        {"command": turn["command"], "description": turn["description"]},
                    )
                    if turn["kind"] == "tool"
                    else _text_chunks(turn["text"])
                )
                payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                encoded = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        Handler.endpoint = self
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "ScriptedOpenAIEndpoint":
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def advertised_tools(self, index: int) -> set[str]:
        tools = self.requests[index]["body"].get("tools")
        names: set[str] = set()
        for tool in tools or []:
            function = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.add(function["name"])
        return names


def _text_chunks(text: str) -> list[dict[str, Any]]:
    return [
        {"choices": [{"delta": {"role": "assistant", "content": None}}]},
        {"choices": [{"delta": {"content": text}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]


def _tool_call_chunks(call_id: str, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"choices": [{"delta": {"role": "assistant", "content": None}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


def assert_history_contains(request: dict[str, Any], *fragments: str) -> None:
    messages = request["body"].get("messages")
    assert isinstance(messages, list), "model request has no messages"
    serialized = json.dumps(messages, ensure_ascii=False)
    for fragment in fragments:
        assert fragment in serialized, f"resumed history is missing {fragment!r}"


@unittest.skipUnless(IS_WINDOWS, "the scripted PowerShell tool scenario targets Windows")
class HarnessRunnerEndToEndTests(unittest.TestCase):
    """Drive the installed DS Harness runtime through a keyless mock model."""

    def test_installed_runtime_edits_file_and_resumes_session(self) -> None:
        try:
            import deepseek_harness  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("deepseek-harness-sdk is not installed")

        with tempfile.TemporaryDirectory(prefix="gme-harness-e2e-") as raw_root:
            worktree = Path(raw_root, "worktree")
            dsh_home = Path(raw_root, "dsh-home")
            worktree.mkdir()
            dsh_home.mkdir()

            with ScriptedOpenAIEndpoint(
                turns=[
                    pwsh_tool_call("Set-Content -LiteralPath marker.txt -Value first"),
                    completed_response("first complete"),
                    completed_response("second complete"),
                ]
            ) as server:
                patch_path = Path(raw_root, "e2e.patch.yml")
                patch_path.write_text(
                    "# Temporary trusted test patch mirroring the production GME\n"
                    "# sandbox, approval, and permission posture, plus the loopback\n"
                    "# mock model endpoint.\n"
                    "- id: sandbox-policy\n"
                    "  config:\n"
                    "    mode: workspace-write\n"
                    "- id: approval\n"
                    "  config:\n"
                    "    policy: never\n"
                    "- id: permission\n"
                    "  config:\n"
                    "    presets:\n"
                    "      gme-coding:\n"
                    "        sandbox: workspace-write\n"
                    "        approval: never\n"
                    "        name: gme-coding\n"
                    "        description: Write inside the job worktree without interactive approval.\n"
                    "    defaultPreset: gme-coding\n"
                    "- id: llm-deepseek\n"
                    "  config:\n"
                    f"    baseURL: {server.url}\n",
                    encoding="utf-8",
                )

                def runner_for() -> HarnessRunner:
                    return HarnessRunner(AgentConfig(dsh_home=str(dsh_home)), lambda _level, _message: None)

                with mock.patch.dict(
                    os.environ,
                    {"DEEPSEEK_API_KEY": "gme-e2e-keyless-test-key"},
                ), mock.patch.object(
                    HarnessRunner,
                    "_sdk_patch_path",
                    new=staticmethod(lambda: patch_path),
                ):
                    first = runner_for().run("write marker", worktree)
                    second = runner_for().run(
                        "report marker", worktree, session_id=first.session_id
                    )

            self.assertEqual(first.finish_reason, "completed")
            self.assertEqual(first.final_response, "first complete")
            self.assertTrue(first.session_id.startswith("gme-"), first.session_id)
            self.assertEqual((worktree / "marker.txt").read_text(encoding="utf-8").strip(), "first")
            self.assertEqual(second.session_id, first.session_id)
            self.assertEqual(second.final_response, "second complete")

            self.assertEqual(len(server.requests), 3)
            self.assertIn(PWSH_TOOL, server.advertised_tools(0))
            self.assertEqual(
                server.requests[0]["authorization"],
                "Bearer gme-e2e-keyless-test-key",
            )
            assert_history_contains(server.requests[-1], "write marker", "first complete")


if __name__ == "__main__":
    unittest.main()
