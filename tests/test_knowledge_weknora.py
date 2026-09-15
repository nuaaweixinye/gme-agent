from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.knowledge.weknora import WeKnoraClient  # noqa: E402


KB = {"kb03": "kb-03-id", "kb02": "kb-02-id"}

SEARCH_BODY = json.dumps(
    {
        "data": [
            {
                "id": "chunk-1",
                "knowledge_id": "doc-1",
                "knowledge_title": "GME-ACIS耦合版本架构设计指导.md",
                "knowledge_filename": "design.md",
                "chunk_index": 12,
                "score": 0.015162852112676055,
                "content": "ACIS 与 GME 的耦合约定……",
            },
            {
                "id": "chunk-2",
                "knowledge_id": "doc-1",
                "knowledge_filename": "design.md",
                "chunk_index": 13,
                "score": 0.0138,
                "content": "第二条命中",
            },
            {"id": "chunk-empty", "knowledge_id": "doc-2", "content": "   "},
        ],
        "success": True,
    },
    ensure_ascii=False,
)


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "GmeKnowledgeStub/1.0"

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "api_key": self.headers.get("X-API-Key"),
                "body": json.loads(raw or "{}"),
            }
        )
        plan = self.server.plan  # type: ignore[attr-defined]
        if plan.get("sleep_ms"):
            time.sleep(plan["sleep_ms"] / 1000)
        status = plan.get("status", 200)
        body = plan.get("body", SEARCH_BODY)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args: object) -> None:
        return


class StubWeKnora:
    def __init__(self, plan: dict | None = None) -> None:
        self.plan = plan or {}
        self.requests: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self._server.plan = self.plan
        self._server.requests = self.requests
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/api/v1"

    def __enter__(self) -> "StubWeKnora":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _client(stub: StubWeKnora, *, api_key: str = "test-key", timeout_ms: int = 3000) -> WeKnoraClient:
    return WeKnoraClient(base_url=stub.base_url, api_key=api_key, sources=KB, timeout_ms=timeout_ms)


class WeKnoraClientTests(unittest.TestCase):
    def test_successful_search_posts_one_knowledge_base_and_labels_hits(self) -> None:
        with StubWeKnora() as stub:
            outcome = _client(stub).search("kb03", "derivative_law 容差", max_results=6)

        self.assertFalse(outcome.degraded)
        self.assertEqual(outcome.source, "kb03")
        self.assertEqual([hit.chunk_id for hit in outcome.hits], ["chunk-1", "chunk-2"])
        self.assertEqual(outcome.hits[0].source, "kb03")
        self.assertEqual(outcome.hits[0].document, "GME-ACIS耦合版本架构设计指导.md")
        self.assertEqual(outcome.hits[0].chunk_index, 12)
        self.assertAlmostEqual(outcome.hits[0].score, 0.015162852112676055)
        self.assertEqual(outcome.hits[1].document, "design.md")

        request = stub.requests[0]
        self.assertEqual(request["path"], "/api/v1/knowledge-search")
        self.assertEqual(request["api_key"], "test-key")
        self.assertEqual(request["body"], {"query": "derivative_law 容差", "knowledge_base_ids": [KB["kb03"]]})

    def test_max_results_truncates_client_side(self) -> None:
        with StubWeKnora() as stub:
            outcome = _client(stub).search("kb03", "query", max_results=1)

        self.assertEqual(len(outcome.hits), 1)

    def test_max_results_zero_returns_no_hits(self) -> None:
        with StubWeKnora() as stub:
            outcome = _client(stub).search("kb03", "query", max_results=0)

        self.assertFalse(outcome.degraded)
        self.assertEqual(outcome.hits, ())
        self.assertEqual(len(stub.requests), 1)

    def test_a_server_error_body_echoing_the_key_is_redacted(self) -> None:
        plan = {"status": 500, "body": '{"error": "rejected X-API-Key super-secret"}'}
        with StubWeKnora(plan) as stub:
            outcome = _client(stub, api_key="super-secret").search("kb03", "query", max_results=6)

        self.assertTrue(outcome.degraded)
        self.assertIn("HTTP 500", outcome.error)
        self.assertIn("[REDACTED]", outcome.error)
        self.assertNotIn("super-secret", outcome.error)

    def test_unauthorized_is_a_degraded_outcome_without_the_key(self) -> None:
        plan = {"status": 401, "body": '{"error": "Unauthorized: invalid API key"}'}
        with StubWeKnora(plan) as stub:
            outcome = _client(stub, api_key="super-secret").search("kb03", "query", max_results=6)

        self.assertTrue(outcome.degraded)
        self.assertEqual(outcome.hits, ())
        self.assertIn("HTTP 401", outcome.error)
        self.assertNotIn("super-secret", outcome.error)

    def test_server_error_is_degraded(self) -> None:
        plan = {
            "status": 500,
            "body": '{"error": {"code": 1007, "message": "knowledge base not found"}, "success": false}',
        }
        with StubWeKnora(plan) as stub:
            outcome = _client(stub).search("kb03", "query", max_results=6)

        self.assertTrue(outcome.degraded)
        self.assertIn("knowledge base not found", outcome.error)

    def test_timeout_is_degraded_and_bounded(self) -> None:
        with StubWeKnora({"sleep_ms": 2000}) as stub:
            started = time.monotonic()
            outcome = _client(stub, timeout_ms=300).search("kb03", "query", max_results=6)
            elapsed = time.monotonic() - started

        self.assertTrue(outcome.degraded)
        self.assertIn("timed out after 300 ms", outcome.error)
        self.assertLess(elapsed, 1.5)

    def test_non_json_body_is_degraded(self) -> None:
        with StubWeKnora({"body": "<html>proxy</html>"}) as stub:
            outcome = _client(stub).search("kb03", "query", max_results=6)

        self.assertTrue(outcome.degraded)
        self.assertIn("JSONDecodeError", outcome.error)

    def test_missing_api_key_never_calls_the_server(self) -> None:
        with StubWeKnora() as stub:
            outcome = _client(stub, api_key="").search("kb03", "query", max_results=6)

        self.assertTrue(outcome.degraded)
        self.assertIn("API key", outcome.error)
        self.assertEqual(stub.requests, [])

    def test_an_unset_base_url_degrades_with_a_clear_message(self) -> None:
        # The shipped default base URL is empty (a real deployment address lives in
        # the git-ignored config.local.json), so enabling the feature without
        # configuring it must say so rather than guess an address.
        client = WeKnoraClient(base_url="", api_key="test-key", sources=KB, timeout_ms=3000)

        outcome = client.search("kb03", "query", max_results=6)

        self.assertTrue(outcome.degraded)
        self.assertIn("base URL is not configured", outcome.error)

    def test_blank_query_and_unknown_source_short_circuit(self) -> None:
        with StubWeKnora() as stub:
            client = _client(stub)
            blank = client.search("kb03", "   ", max_results=6)
            unknown = client.search("kb99", "query", max_results=6)

        self.assertFalse(blank.degraded)
        self.assertEqual(blank.hits, ())
        self.assertTrue(unknown.degraded)
        self.assertIn("No knowledge base id configured", unknown.error)
        self.assertEqual(stub.requests, [])

    def test_search_all_returns_one_outcome_per_configured_source_in_order(self) -> None:
        with StubWeKnora() as stub:
            outcomes = _client(stub).search_all("query", max_results=6)

        self.assertEqual([outcome.source for outcome in outcomes], ["kb03", "kb02"])
        self.assertEqual(len(stub.requests), 2)


if __name__ == "__main__":
    unittest.main()
