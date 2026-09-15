from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.knowledge.injection import build_injection, knowledge_query  # noqa: E402
from gme_agent.settings.config import AgentConfig, KnowledgeConfig  # noqa: E402
from gme_agent.storage.db import AgentDb  # noqa: E402


INTERFACE = "laws-main_law-derivative_law-evaluate"
SEARCH_BODY = json.dumps(
    {
        "data": [
            {
                "id": "chunk-1",
                "knowledge_id": "doc-1",
                "knowledge_title": "GME-ACIS耦合版本架构设计指导.md",
                "chunk_index": 12,
                "score": 0.0152,
                "content": "ACIS 与 GME 的耦合约定",
            }
        ],
        "success": True,
    },
    ensure_ascii=False,
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.calls += 1  # type: ignore[attr-defined]
        status = self.server.plan.get("status", 200)  # type: ignore[attr-defined]
        body = self.server.plan.get("body", SEARCH_BODY)  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args: object) -> None:
        return


class StubServer:
    def __init__(self, plan: dict | None = None) -> None:
        self.plan = plan or {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.plan = self.plan
        self._server.calls = 0
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def calls(self) -> int:
        return self._server.calls

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/api/v1"

    def __enter__(self) -> "StubServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _config(base_url: str, *, enabled: bool = True, **overrides: object) -> AgentConfig:
    knowledge = KnowledgeConfig.from_dict(
        {
            "enabled": enabled,
            "weknora": {
                "base_url": base_url,
                "api_key_env": "GME_TEST_WEKNORA_KEY",
                "timeout_ms": 2000,
                "knowledge_bases": [
                    {"label": "kb01", "id": "kb-01"},
                    {"label": "kb02", "id": "kb-02"},
                ],
            },
            "budgets": overrides.get("budgets") or {"max_priors": 8, "max_kb_hits": 6, "max_chars": 4000},
            "closed_loop": {"enabled": True, "min_stable_runs": 2},
        }
    )
    return AgentConfig(knowledge=knowledge)


class InjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = AgentDb(":memory:")
        self.addCleanup(self.db.close)
        self.db.create_job(job_id="job-1", job_type="test_generation", title="laws", module="laws", api_name="api_x")
        self.db.upsert_failure(
            failure_id="gmefail-1",
            job_id="job-1",
            test_suite="Laws_ClassTest",
            test_name="T1",
            file="tests/gme/src/laws/law_main_law_test.cpp",
            line=3017,
            reason="The difference between gme_answer and 1.0 is 1",
            reproduce_command="tests.exe --gtest_filter=Laws_ClassTest.T1",
            metadata={"module": "laws", "interface_id": INTERFACE, "api_name": "GME::derivative_law::evaluate"},
        )
        self.db.add_failure_observation(
            run_id="run-1",
            failure_id="gmefail-1",
            job_id="job-1",
            outcome="failed",
            test_suite="Laws_ClassTest",
            test_name="T1",
        )
        self.events: list[tuple[str, str]] = []

    def _emit(self, level: str, message: str) -> None:
        self.events.append((level, message))

    def test_disabled_config_does_nothing(self) -> None:
        with StubServer() as server:
            result = build_injection(
                _config(server.base_url, enabled=False),
                self.db,
                module="laws",
                interface_ids=[INTERFACE],
                on_event=self._emit,
            )

        self.assertIsNone(result.block)
        self.assertEqual(result.artifact_text, "")
        self.assertEqual(result.metadata, {"enabled": False})
        self.assertEqual(server.calls, 0)
        self.assertEqual(self.events, [])
        self.assertNotIn("knowledge", self.db.get_failure("gmefail-1")["metadata"])

    def test_enabled_injection_returns_block_artifact_metadata_and_events(self) -> None:
        import os

        os.environ["GME_TEST_WEKNORA_KEY"] = "stub-key"
        self.addCleanup(os.environ.pop, "GME_TEST_WEKNORA_KEY", None)

        with StubServer() as server:
            result = build_injection(
                _config(server.base_url),
                self.db,
                module="laws",
                interface_ids=[INTERFACE],
                api_names=["GME::derivative_law::evaluate"],
                symbols=["GME::derivative_law::evaluate(double) const"],
                on_event=self._emit,
            )

        self.assertIsNotNone(result.block)
        self.assertIn("### 本地分歧先验（source=local）", result.block)
        self.assertIn("Laws_ClassTest.T1", result.block)
        self.assertIn("### 知识库参照 kb01", result.block)
        self.assertIn("GME-ACIS耦合版本架构设计指导.md", result.block)
        self.assertIn("# 知识注入记录", result.artifact_text)
        self.assertIn('"prior_count": 1', result.artifact_text)

        metadata = result.metadata
        self.assertTrue(metadata["enabled"])
        self.assertEqual(metadata["prior_count"], 1)
        self.assertEqual(metadata["hit_count"], 2)  # kb01 与 kb02 各 1 条 stub 命中
        self.assertEqual([source["label"] for source in metadata["sources"]], ["kb01", "kb02"])
        self.assertEqual(metadata["sources"][0]["hits"], 1)
        self.assertIn("derivative_law", metadata["query"])
        self.assertFalse(metadata["truncated"])

        self.assertTrue(any(message.startswith("knowledge/assembled") for _level, message in self.events))
        knowledge = self.db.get_failure("gmefail-1")["metadata"]["knowledge"]
        self.assertEqual(knowledge["injected_count"], 1)
        self.assertIn("last_injected_at", knowledge)

    def test_injected_count_accumulates_across_tasks(self) -> None:
        import os

        os.environ["GME_TEST_WEKNORA_KEY"] = "stub-key"
        self.addCleanup(os.environ.pop, "GME_TEST_WEKNORA_KEY", None)

        with StubServer() as server:
            config = _config(server.base_url)
            build_injection(config, self.db, module="laws", interface_ids=[INTERFACE])
            build_injection(config, self.db, module="laws", interface_ids=[INTERFACE])

        self.assertEqual(self.db.get_failure("gmefail-1")["metadata"]["knowledge"]["injected_count"], 2)

    def test_missing_api_key_degrades_to_priors_only(self) -> None:
        with StubServer() as server:
            result = build_injection(
                _config(server.base_url),
                self.db,
                module="laws",
                interface_ids=[INTERFACE],
                on_event=self._emit,
            )

        self.assertIsNotNone(result.block)
        self.assertIn("### 检索状态", result.block)
        self.assertIn("kb01: API key is not set", result.block)
        self.assertIn("kb02: API key is not set", result.block)
        self.assertEqual(result.metadata["hit_count"], 0)
        self.assertEqual(server.calls, 0)
        self.assertTrue(any(level == "warn" for level, _message in self.events))

    def test_transport_failure_never_raises(self) -> None:
        import os

        os.environ["GME_TEST_WEKNORA_KEY"] = "stub-key"
        self.addCleanup(os.environ.pop, "GME_TEST_WEKNORA_KEY", None)

        result = build_injection(
            _config("http://127.0.0.1:1/api/v1"),
            self.db,
            module="laws",
            interface_ids=[INTERFACE],
            on_event=self._emit,
        )

        self.assertIsNotNone(result.block)
        self.assertIn("### 检索状态", result.block)
        self.assertEqual(result.metadata["hit_count"], 0)

    def test_internal_error_is_caught_and_reported(self) -> None:
        calls: list[str] = []

        def broken_collect(*_args: object, **_kwargs: object) -> list:
            raise RuntimeError("boom")

        with mock.patch("gme_agent.knowledge.injection.collect_priors", broken_collect):
            result = build_injection(
                _config("http://127.0.0.1:1/api/v1"),
                self.db,
                module="laws",
                on_event=lambda level, message: calls.append(f"{level}:{message}"),
            )

        self.assertIsNone(result.block)
        self.assertIn("RuntimeError: boom", result.metadata["error"])
        self.assertTrue(any(call.startswith("warn:knowledge/injection-failed") for call in calls))

    def test_a_raising_event_callback_does_not_escape(self) -> None:
        import os

        os.environ["GME_TEST_WEKNORA_KEY"] = "stub-key"
        self.addCleanup(os.environ.pop, "GME_TEST_WEKNORA_KEY", None)

        def broken_emit(_level: str, _message: str) -> None:
            raise RuntimeError("database is closed")

        with StubServer() as server:
            result = build_injection(
                _config(server.base_url),
                self.db,
                module="laws",
                interface_ids=[INTERFACE],
                on_event=broken_emit,
            )

        self.assertIsNotNone(result.block)
        self.assertEqual(result.metadata["prior_count"], 1)
        self.assertNotIn("error", result.metadata)


class KnowledgeQueryTests(unittest.TestCase):
    def test_query_uses_module_symbols_and_a_gme_acis_suffix(self) -> None:
        query = knowledge_query(
            "laws",
            symbols=["outcome api_make_cubic(double)", "int law::zero(double) const", "third", "fourth"],
            api_names=["GME::derivative_law::evaluate"],
        )

        self.assertTrue(query.startswith("laws "))
        self.assertIn("outcome api_make_cubic(double)", query)
        self.assertIn("int law::zero(double) const", query)
        self.assertIn("third", query)
        self.assertNotIn("fourth", query)
        self.assertTrue(query.endswith("GME ACIS"))

    def test_query_is_bounded_and_tolerates_empty_input(self) -> None:
        self.assertEqual(knowledge_query("", symbols=[], api_names=[]), "GME ACIS")
        self.assertLessEqual(len(knowledge_query("laws", symbols=["x" * 500])), 300)

    def test_a_long_symbol_list_still_keeps_the_suffix(self) -> None:
        symbol = (
            "outline api_make_cubic_with_a_long_name(double, double, double, double, "
            "double, double, double, double, law *&) const"
        )

        query = knowledge_query("laws", symbols=[symbol, symbol + " b", symbol + " c"])

        # Guard the guard: if the symbol ever shrinks below the point where three of
        # them overflow the cap, this test stops proving anything and must fail loudly.
        self.assertGreater(len(symbol) * 3, 300)
        self.assertTrue(query.startswith("laws "))
        self.assertLessEqual(len(query), 300)
        self.assertTrue(query.endswith("GME ACIS"))


if __name__ == "__main__":
    unittest.main()
