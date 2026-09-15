"""Assemble the knowledge block for one task and record what was injected.

This is the only place that touches configuration, the network, and the task
database together. Every failure path returns an empty result: a task must never
fail because knowledge retrieval did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence
import json
import os
import time

from .assembler import KnowledgeBudget, KnowledgeContext, assemble_knowledge_context
from .local_priors import collect_priors
from .promote import record_injection_counts
from .render import render_knowledge_block
from .weknora import SearchOutcome, WeKnoraClient

QUERY_SYMBOL_LIMIT = 3
QUERY_CHAR_LIMIT = 300
QUERY_SUFFIX = "GME ACIS"
EVENT_LINE_CHAR_LIMIT = 600

EventCallback = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class InjectionResult:
    block: str | None = None
    artifact_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def build_injection(
    config: Any,
    db: Any,
    *,
    module: str,
    api_names: Sequence[str] = (),
    interface_ids: Sequence[str] = (),
    symbols: Sequence[str] = (),
    on_event: EventCallback | None = None,
) -> InjectionResult:
    settings = config.knowledge
    if not settings.enabled:
        return InjectionResult(metadata={"enabled": False})
    emit = on_event or (lambda _level, _message: None)
    started = time.monotonic()
    try:
        budgets = settings.budgets
        budget = KnowledgeBudget(
            max_priors=budgets.max_priors,
            max_kb_hits=budgets.max_kb_hits,
            max_chars=budgets.max_chars,
        )
        query = knowledge_query(module, symbols=symbols, api_names=api_names)
        priors = collect_priors(
            db,
            module=module,
            interface_ids=interface_ids,
            api_names=api_names,
            limit=budget.max_priors,
        )
        outcomes = _search(config, query, max_results=budget.max_kb_hits)
        context = assemble_knowledge_context(priors=priors, outcomes=outcomes, budget=budget, query=query)
        block = render_knowledge_block(context)
        metadata = _metadata(context, elapsed_ms=int((time.monotonic() - started) * 1000), block_chars=len(block))
        record_injection_counts(db, [item.prior.failure_id for item in context.priors])
        _safe_emit(emit, "warn" if context.degraded_sources() else "info", _event_line(metadata))
        return InjectionResult(
            block=block or None,
            artifact_text=_artifact_text(block, metadata),
            metadata=metadata,
        )
    except Exception as exc:
        metadata = {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}
        _safe_emit(emit, "warn", f"knowledge/injection-failed {metadata['error']}")
        return InjectionResult(metadata=metadata)


def _safe_emit(emit: EventCallback, level: str, message: str) -> None:
    """Call the caller's emitter without letting it break the injection contract.

    The orchestrator promises that no knowledge-path failure fails the task, and
    `on_event` is the flow's job-event writer, which touches the database and can
    raise. An exception from it must neither escape `build_injection` nor cost the
    caller an already-rendered block — which is what happens if the handler's own
    emit is unguarded and raises again.
    """

    try:
        emit(level, message)
    except Exception:
        pass


def knowledge_query(module: str, *, symbols: Sequence[str] = (), api_names: Sequence[str] = ()) -> str:
    parts: list[str] = []
    module_name = " ".join(str(module or "").split())
    if module_name:
        parts.append(module_name)
    for value in [*symbols, *api_names]:
        text = " ".join(str(value or "").split())
        if not text or text in parts:
            continue
        parts.append(text)
        if len(parts) >= 1 + QUERY_SYMBOL_LIMIT:
            break
    # Reserve room for the suffix before spending the budget on symbols: a real
    # unique_symbol runs 60-120 characters, so three of them overflow the cap and a
    # naive slice would drop the anchor that tells the retrieval service what
    # corpus this question is about.
    budget = max(QUERY_CHAR_LIMIT - len(QUERY_SUFFIX) - 1, 0)
    body = " ".join(parts)[:budget].rstrip()
    return f"{body} {QUERY_SUFFIX}".strip()


def _search(config: Any, query: str, *, max_results: int) -> list[SearchOutcome]:
    weknora = config.knowledge.weknora
    api_key = os.environ.get(weknora.api_key_env, "") if weknora.api_key_env else ""
    client = WeKnoraClient(
        base_url=weknora.base_url,
        api_key=api_key,
        sources=weknora.source_ids(),
        timeout_ms=weknora.timeout_ms,
    )
    return client.search_all(query, max_results=max_results)


def _metadata(context: KnowledgeContext, *, elapsed_ms: int, block_chars: int) -> dict[str, Any]:
    return {
        "enabled": True,
        "query": context.query,
        "elapsed_ms": elapsed_ms,
        "block_chars": block_chars,
        "truncated": context.truncated,
        "prior_count": len(context.priors),
        "hit_count": len(context.hits),
        "priors": [
            {
                "failure_id": item.prior.failure_id,
                "test": item.prior.key,
                "interface_id": item.prior.interface_id,
                "api_name": item.prior.api_name,
                "stable_runs": item.prior.stable_runs,
                "confirmed": item.prior.confirmed,
                "match_kind": item.prior.match_kind,
                "truncated": item.truncated,
            }
            for item in context.priors
        ],
        "sources": [
            {"label": outcome.source, "hits": len(outcome.hits), "error": outcome.error}
            for outcome in context.outcomes
        ],
        "documents": [
            {
                "label": item.hit.source,
                "knowledge_id": item.hit.knowledge_id,
                "chunk_index": item.hit.chunk_index,
                "document": item.hit.document,
                "truncated": item.truncated,
            }
            for item in context.hits
        ],
    }


def _event_line(metadata: dict[str, Any]) -> str:
    sources = ",".join(f"{item['label']}:{item['hits']}" for item in metadata["sources"]) or "none"
    degraded = ",".join(item["label"] for item in metadata["sources"] if item["error"]) or "none"
    attributed = ",".join(
        sorted({item["interface_id"] or item["api_name"] for item in metadata["priors"] if item["interface_id"] or item["api_name"]})
    ) or "none"
    line = (
        f"knowledge/assembled priors={metadata['prior_count']} hits={metadata['hit_count']} "
        f"sources={sources} degraded={degraded} priors_for={attributed} "
        f"elapsed_ms={metadata['elapsed_ms']} chars={metadata['block_chars']} "
        f"truncated={'true' if metadata['truncated'] else 'false'}"
    )
    return line[:EVENT_LINE_CHAR_LIMIT]


def _artifact_text(block: str, metadata: dict[str, Any]) -> str:
    record = "## 组装记录\n\n```json\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n```\n"
    if not block:
        return "# 知识注入记录\n\n（本次没有可注入内容）\n\n" + record
    return "# 知识注入记录\n\n以下是注入生成提示词的原样内容：\n\n" + block + "\n" + record
