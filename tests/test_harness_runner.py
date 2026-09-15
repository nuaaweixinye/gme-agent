from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.harness.runner import (  # noqa: E402
    MAX_TOOL_EVENTS_PER_RUN,
    TOOL_EVENT_TAIL_CHARACTERS,
    STDERR_TAIL_CHARACTERS,
    HarnessRunner,
    tool_notification_events,
)
from gme_agent.settings.config import AgentConfig  # noqa: E402


SDK_PATCH_YML = ROOT / "backend" / "gme_agent" / "harness" / "sdk.patch.yml"


class FakeRunResult:
    def __init__(self, *, final_response: str = "agent finished", finish_reason: str | None = "completed") -> None:
        self.session_id = "fake-session"
        self.final_response = final_response
        self.finish_reason = finish_reason
        self.events: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []

    def __str__(self) -> str:
        return f"FakeRunResult(finish_reason={self.finish_reason!r})"


class FakeHarnessConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeHarness:
    def __init__(self, config: FakeHarnessConfig, spec: dict[str, Any]) -> None:
        self.config = config
        self.spec = spec
        self.closed = False
        spec["instances"].append(self)

    def __enter__(self) -> "FakeHarness":
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def run(
        self,
        input: Any,
        *,
        session_id: str | None = None,
        resume_if_exists: bool = False,
        on_notification: Any = None,
    ) -> FakeRunResult:
        self.spec["run_calls"].append(
            {
                "input": input,
                "session_id": session_id,
                "resume_if_exists": resume_if_exists,
                "on_notification": on_notification,
            }
        )
        self.spec["order"].append("run")
        error = self.spec.get("error")
        if error is not None:
            raise error
        for notification in self.spec.get("notifications") or []:
            if on_notification is not None:
                on_notification(notification)
        return self.spec["result"]


def _notification(kind: str, data: dict[str, Any], *, session_id: str = "fake-session") -> Any:
    return types.SimpleNamespace(
        method="session.event",
        payload={"sessionId": session_id, "event": {"type": kind, "data": data}},
    )


def _spec(result: FakeRunResult | None = None, error: Exception | None = None) -> dict[str, Any]:
    return {
        "result": result or FakeRunResult(),
        "error": error,
        "run_calls": [],
        "order": [],
        "instances": [],
        "notifications": [],
    }


def _fake_sdk_module(spec: dict[str, Any]) -> types.ModuleType:
    module = types.ModuleType("deepseek_harness")
    module.DeepSeekHarness = lambda config: FakeHarness(config, spec)
    module.DeepSeekHarnessConfig = FakeHarnessConfig
    return module


class HarnessRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gme-harness-runner-")
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name, "worktree")
        self.worktree.mkdir()
        self.dsh_home = Path(self._tmp.name, "dsh-home")
        self.dsh_home.mkdir()
        self.events: list[tuple[str, str]] = []

    def _emit(self, level: str, message: str) -> None:
        self.events.append((level, message))

    def _runner(self, **overrides: Any) -> HarnessRunner:
        config = AgentConfig(dsh_home=str(self.dsh_home), **overrides)
        return HarnessRunner(config, self._emit)

    def test_run_builds_sdk_config_and_resumes_session(self) -> None:
        spec = _spec()
        started: list[str] = []

        def on_session_started(session_id: str) -> None:
            started.append(session_id)
            spec["order"].append("session_started")

        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            result = runner.run(
                "write the tests",
                self.worktree,
                skill_names=[],
                on_session_started=on_session_started,
            )

        self.assertEqual(len(spec["run_calls"]), 1)
        call = spec["run_calls"][0]
        self.assertTrue(call["resume_if_exists"])
        self.assertTrue(call["session_id"].startswith("gme-"))
        self.assertEqual(result.session_id, call["session_id"])
        self.assertEqual(result.finish_reason, "completed")
        self.assertEqual(result.final_response, "agent finished")
        self.assertIn("FakeRunResult", result.raw)

        self.assertEqual(started, [call["session_id"]])
        self.assertLess(spec["order"].index("session_started"), spec["order"].index("run"))

        instance = spec["instances"][0]
        expected = {
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
            "cwd": str(self.worktree.resolve()),
            "runtime_cwd": str(self.worktree.resolve()),
            "profile": "sdk",
            "dsh_home": str(self.dsh_home.resolve()),
            "patches": (str(SDK_PATCH_YML.resolve()),),
        }
        for key, value in expected.items():
            self.assertEqual(instance.config.kwargs.get(key), value, key)
        self.assertTrue(instance.closed)

    def test_run_reuses_stored_session_id(self) -> None:
        spec = _spec()
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            result = runner.run("continue", self.worktree, session_id="gme-stored-1")
        self.assertEqual(result.session_id, "gme-stored-1")
        self.assertEqual(spec["run_calls"][0]["session_id"], "gme-stored-1")

    def test_run_prepends_named_skills_once_and_cleans_staging(self) -> None:
        spec = _spec()
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            runner.run(
                "generate tests",
                self.worktree,
                skill_names=["gme-test-generation", "gme-test-generation"],
            )
        call = spec["run_calls"][0]
        self.assertIsInstance(call["input"], str)
        self.assertTrue(call["input"].startswith("$gme-test-generation"))
        self.assertEqual(call["input"].count("$gme-test-generation"), 1)
        self.assertIn("generate tests", call["input"])
        self.assertFalse((self.worktree / ".agents").exists())

    def test_run_without_builtin_skills_sends_plain_prompt(self) -> None:
        spec = _spec()
        runner = self._runner(use_builtin_skills=False)
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            runner.run("generate tests", self.worktree, skill_names=["gme-test-generation"])
        self.assertEqual(spec["run_calls"][0]["input"], "generate tests")

    def test_run_raises_when_agent_disabled(self) -> None:
        runner = self._runner(agent_enabled=False)
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            runner.run("write the tests", self.worktree)

    def test_run_accepts_max_tokens_finish_reason_with_warning(self) -> None:
        spec = _spec(result=FakeRunResult(finish_reason="max-tokens"))
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            result = runner.run("write the tests", self.worktree)
        self.assertEqual(result.finish_reason, "max-tokens")
        self.assertTrue(any(level == "warn" for level, _message in self.events))

    def test_run_raises_for_error_finish_reason(self) -> None:
        spec = _spec(result=FakeRunResult(finish_reason="error"))
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            with self.assertRaisesRegex(RuntimeError, "error"):
                runner.run("write the tests", self.worktree)

    def test_run_raises_for_aborted_finish_reason(self) -> None:
        spec = _spec(result=FakeRunResult(finish_reason="aborted"))
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            with self.assertRaisesRegex(RuntimeError, "aborted"):
                runner.run("write the tests", self.worktree)

    def test_run_raises_for_missing_finish_reason(self) -> None:
        spec = _spec(result=FakeRunResult(finish_reason=None))
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            with self.assertRaisesRegex(RuntimeError, "finish reason"):
                runner.run("write the tests", self.worktree)

    def test_sdk_failure_is_redacted_and_bounded(self) -> None:
        diagnostic = "\n".join(f"diagnostic-line-{i}" for i in range(1200))
        stderr = (
            "DeepSeek Harness SDK runtime failed.\n"
            "authorization: Bearer abc123def456\n"
            f"{diagnostic}\n"
            "api_key=secret-value\n"
        )
        spec = _spec(error=RuntimeError(stderr))
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            with self.assertRaises(RuntimeError) as ctx:
                runner.run("write the tests", self.worktree)

        message = str(ctx.exception)
        self.assertNotIn("secret-value", message)
        self.assertNotIn("abc123def456", message)
        self.assertIn("api_key=[REDACTED]", message)
        self.assertIn("diagnostic-line-1199", message)
        self.assertNotIn("diagnostic-line-5\n", message)
        self.assertIn("earlier characters omitted", message)
        self.assertLessEqual(len(message), STDERR_TAIL_CHARACTERS + 200)
        self.assertTrue(spec["instances"][0].closed)

    def test_trusted_sdk_patch_keeps_workspace_write_and_disables_approval(self) -> None:
        text = SDK_PATCH_YML.read_text(encoding="utf-8")
        self.assertIn("- id: sandbox-policy", text)
        self.assertIn("mode: workspace-write", text)
        self.assertIn("- id: approval", text)
        self.assertIn("policy: never", text)
        self.assertIn("- id: permission", text)
        self.assertIn("defaultPreset: gme-coding", text)

    def test_run_passes_a_notification_callback_and_records_tool_events(self) -> None:
        spec = _spec()
        spec["notifications"] = [
            _notification(
                "tool/call",
                {"turn": 1, "step": 1, "callId": "call-1", "name": "read", "arguments": '{"file_path":"tests/gme/src/laws/x.cpp"}'},
            ),
            _notification(
                "tool/result",
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "call-1",
                                "content": [{"type": "text", "text": "line one\nline two"}],
                            }
                        ]
                    }
                },
            ),
        ]
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            runner.run("write the tests", self.worktree, skill_names=[])

        self.assertTrue(callable(spec["run_calls"][0]["on_notification"]))
        messages = [message for _level, message in self.events]
        self.assertTrue(any(message.startswith("agent/tool-call read ") for message in messages))
        self.assertTrue(any(message.startswith("agent/tool-result call-1 ") for message in messages))
        self.assertTrue(any("line one" in message for message in messages))

    def test_tool_event_text_is_redacted_and_bounded(self) -> None:
        spec = _spec()
        spec["notifications"] = [
            _notification(
                "tool/call",
                {"name": "bash", "arguments": "api_key=secret-value " + "x" * 5000},
            )
        ]
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            runner.run("write the tests", self.worktree, skill_names=[])

        message = [text for _level, text in self.events if text.startswith("agent/tool-call bash")][0]
        self.assertNotIn("secret-value", message)
        self.assertIn("api_key=[REDACTED]", message)
        self.assertLessEqual(len(message), TOOL_EVENT_TAIL_CHARACTERS + 80)

    def test_tool_events_are_capped_per_run(self) -> None:
        spec = _spec()
        spec["notifications"] = [
            _notification("tool/call", {"name": f"tool-{index}", "arguments": "{}"})
            for index in range(MAX_TOOL_EVENTS_PER_RUN + 5)
        ]
        runner = self._runner()
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            runner.run("write the tests", self.worktree, skill_names=[])

        recorded = [message for _level, message in self.events if message.startswith("agent/tool-call")]
        self.assertEqual(len(recorded), MAX_TOOL_EVENTS_PER_RUN)
        self.assertTrue(any("suppressed" in message for _level, message in self.events))

    def test_failing_emit_does_not_break_the_agent_run(self) -> None:
        spec = _spec()
        spec["notifications"] = [_notification("tool/call", {"name": "read", "arguments": "{}"})]
        config = AgentConfig(dsh_home=str(self.dsh_home))
        calls: list[str] = []

        def emit(level: str, message: str) -> None:
            if message.startswith("agent/"):
                calls.append(message)
                raise RuntimeError("database is closed")
            self.events.append((level, message))

        runner = HarnessRunner(config, emit)
        with mock.patch.dict(sys.modules, {"deepseek_harness": _fake_sdk_module(spec)}):
            result = runner.run("write the tests", self.worktree, skill_names=[])

        self.assertEqual(result.finish_reason, "completed")
        # The emitter rejects every `agent/` line, so the summary line this runner
        # tries to record is observable only through the rejected calls: a broken
        # job-event writer must not turn a finished turn into a failed task.
        self.assertEqual(calls[0], "agent/tool-call read {}")
        self.assertTrue(any(message.startswith("agent/tool-event-errors") for message in calls))
        self.assertEqual(len(calls), 2)


class ToolNotificationMappingTests(unittest.TestCase):
    def test_non_session_notifications_are_ignored(self) -> None:
        self.assertEqual(tool_notification_events(types.SimpleNamespace(method="session.status", payload={})), [])
        self.assertEqual(tool_notification_events(types.SimpleNamespace(method="session.event", payload={})), [])
        self.assertEqual(tool_notification_events(types.SimpleNamespace(method="session.event", payload={"event": 5})), [])

    def test_unknown_event_types_are_ignored(self) -> None:
        notification = _notification("assistant/message", {"message": {"content": []}})
        self.assertEqual(tool_notification_events(notification), [])

    def test_tool_call_and_result_are_mapped(self) -> None:
        call = tool_notification_events(_notification("tool/call", {"name": "grep", "arguments": '{"pattern":"x"}'}))
        result = tool_notification_events(
            _notification(
                "tool/result",
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "call-9",
                                "isError": True,
                                "content": [{"type": "text", "text": "no matches"}],
                            }
                        ]
                    }
                },
            )
        )

        self.assertEqual(call, [("info", 'agent/tool-call grep {"pattern":"x"}')])
        self.assertEqual(result, [("warn", "agent/tool-result call-9 no matches")])


class AgentConfigHarnessFieldsTests(unittest.TestCase):
    def test_defaults_target_deepseek_harness(self) -> None:
        config = AgentConfig()
        self.assertEqual(config.provider, "deepseek-official")
        self.assertEqual(config.model, "deepseek-v4-flash")
        self.assertEqual(config.dsh_profile, "sdk")
        self.assertEqual(config.dsh_bin, "")
        self.assertTrue(config.agent_enabled)
        self.assertFalse(hasattr(config, "sandbox"))
        self.assertFalse(hasattr(config, "approval_policy"))
        self.assertFalse(hasattr(config, "codex_enabled"))

    def test_resolved_expands_dsh_home(self) -> None:
        config = AgentConfig(dsh_home="~/.dsh")
        self.assertEqual(Path(config.resolved().dsh_home), Path("~/.dsh").expanduser())


if __name__ == "__main__":
    unittest.main()
