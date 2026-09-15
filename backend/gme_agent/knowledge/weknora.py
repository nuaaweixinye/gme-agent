"""Minimal WeKnora retrieval client for one knowledge base at a time.

Measured 2026-09-15 on the live deployment: asking one question with all four
knowledge base ids returned ten hits, all of them from KB00, which crowds out
KB01/KB02/KB03. Searching one base per request keeps every source labelled,
independently time-bounded, and independently degradable.

Failures never raise: the caller gets a `SearchOutcome` carrying the reason so
the prompt can state which source was unavailable. The API key is never part of
an error message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import socket
import urllib.error
import urllib.request

ERROR_SNIPPET_CHARS = 200
SEARCH_PATH = "/knowledge-search"


@dataclass(frozen=True, slots=True)
class Hit:
    source: str
    knowledge_id: str
    chunk_id: str
    document: str
    chunk_index: int
    score: float
    content: str


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    source: str
    hits: tuple[Hit, ...] = ()
    error: str = ""

    @property
    def degraded(self) -> bool:
        return bool(self.error)


class WeKnoraClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        sources: dict[str, str],
        timeout_ms: int = 3000,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.sources = dict(sources or {})
        self.timeout_ms = max(int(timeout_ms), 1)

    def search(self, source: str, query: str, *, max_results: int) -> SearchOutcome:
        knowledge_base_id = self.sources.get(source, "")
        if not self.api_key:
            return self._outcome(source, "API key is not set")
        if not self.base_url:
            return self._outcome(source, "WeKnora base URL is not configured")
        if not knowledge_base_id:
            return self._outcome(source, f"No knowledge base id configured for {source}")
        if not (query or "").strip():
            return self._outcome(source)
        try:
            envelope = self._post_search(knowledge_base_id, query)
        except urllib.error.HTTPError as exc:
            return self._outcome(source, f"HTTP {exc.code}: {_error_body(exc)}")
        except (socket.timeout, TimeoutError):
            return self._outcome(source, f"timed out after {self.timeout_ms} ms")
        except urllib.error.URLError as exc:
            return self._outcome(source, f"transport failure: {exc.reason}")
        except Exception as exc:  # malformed JSON, encoding, anything else
            return self._outcome(source, f"{type(exc).__name__}: {exc}")
        hits = _project_hits(envelope, source=source, max_results=max_results)
        return self._outcome(source, hits=tuple(hits))

    def search_all(self, query: str, *, max_results: int) -> list[SearchOutcome]:
        return [self.search(source, query, max_results=max_results) for source in self.sources]

    def _outcome(self, source: str, error: str = "", hits: tuple[Hit, ...] = ()) -> SearchOutcome:
        return SearchOutcome(source=source, hits=hits, error=self._redact(error))

    def _redact(self, text: str) -> str:
        """Keep the credential out of anything the caller may persist.

        A degraded outcome is rendered into the prompt and written to
        `knowledge_context.md`, so a server that echoes the request key in an
        error body must not be able to leak it there.
        """

        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "[REDACTED]")
        return text

    def _post_search(self, knowledge_base_id: str, query: str) -> dict[str, Any]:
        payload = json.dumps(
            {"query": query, "knowledge_base_ids": [knowledge_base_id]},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{SEARCH_PATH}", data=payload, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("X-API-Key", self.api_key)
        with urllib.request.urlopen(request, timeout=self.timeout_ms / 1000) as response:
            body = response.read().decode("utf-8", "replace")
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}


def _project_hits(envelope: dict[str, Any], *, source: str, max_results: int) -> list[Hit]:
    raw_hits = envelope.get("data")
    if not isinstance(raw_hits, list):
        return []
    limit = max(int(max_results), 0)
    hits: list[Hit] = []
    for raw in raw_hits:
        if len(hits) >= limit:
            break
        if not isinstance(raw, dict):
            continue
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        hits.append(
            Hit(
                source=source,
                knowledge_id=str(raw.get("knowledge_id") or ""),
                chunk_id=str(raw.get("id") or ""),
                document=_document_label(raw),
                chunk_index=raw["chunk_index"] if isinstance(raw.get("chunk_index"), int) else -1,
                score=float(raw["score"]) if isinstance(raw.get("score"), (int, float)) else 0.0,
                content=content,
            )
        )
    return hits


def _document_label(raw: dict[str, Any]) -> str:
    for field in ("knowledge_title", "knowledge_filename"):
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(raw.get("knowledge_id") or "(untitled)")


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        text = exc.read().decode("utf-8", "replace")
    except Exception:
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:ERROR_SNIPPET_CHARS]
