"""Merge priors and retrieval hits into one budgeted, priority-ordered context.

Pure functions only: no I/O, no configuration, no clock. Ordering is fixed as
`local > kb01 > kb02 > kb03 > kb00`, and a single character budget is spent in
that order so the cheapest-to-lose source is what gets trimmed.

An item that cannot fit is dropped whole. Stopping at that point is exact
rather than merely cheap: a kept item spends down to the `LABEL_OVERHEAD_CHARS`
label overhead, so `remaining` never grows, and once the room left for content
falls under `MIN_ITEM_CHARS` every later item fails the same test. The first
item that cannot fit is therefore already the last one that could have been
kept, so later sources are not skipped while budget is still usable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .local_priors import Prior
from .weknora import Hit, SearchOutcome

SOURCE_PRIORITY = {"local": 0, "kb01": 1, "kb02": 2, "kb03": 3, "kb00": 4}
UNKNOWN_SOURCE_PRIORITY = 9
MIN_ITEM_CHARS = 200
LABEL_OVERHEAD_CHARS = 96


@dataclass(frozen=True, slots=True)
class KnowledgeBudget:
    max_priors: int = 8
    max_kb_hits: int = 6
    max_chars: int = 4000


@dataclass(frozen=True, slots=True)
class SelectedPrior:
    prior: Prior
    content: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SelectedHit:
    hit: Hit
    content: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    query: str
    budget: KnowledgeBudget
    priors: tuple[SelectedPrior, ...] = ()
    hits: tuple[SelectedHit, ...] = ()
    outcomes: tuple[SearchOutcome, ...] = ()
    truncated: bool = False

    def is_empty(self) -> bool:
        return not self.priors and not self.hits and not self.degraded_sources()

    def degraded_sources(self) -> list[str]:
        return [
            f"{outcome.source}: {outcome.error}"
            for outcome in self.outcomes
            if outcome.degraded
        ]


def assemble_knowledge_context(
    *,
    priors: Sequence[Prior] = (),
    outcomes: Sequence[SearchOutcome] = (),
    budget: KnowledgeBudget | None = None,
    query: str = "",
) -> KnowledgeContext:
    budgets = budget or KnowledgeBudget()
    resolved_outcomes = tuple(outcomes or ())
    remaining = max(int(budgets.max_chars), 0)
    truncated = False

    selected_priors: list[SelectedPrior] = []
    for prior in list(priors or ())[: max(int(budgets.max_priors), 0)]:
        content, cut = _fit(_prior_content(prior), remaining - LABEL_OVERHEAD_CHARS)
        if content is None:
            truncated = True
            break
        remaining -= len(content) + LABEL_OVERHEAD_CHARS
        selected_priors.append(SelectedPrior(prior=prior, content=content, truncated=cut))
        truncated = truncated or cut

    selected_hits: list[SelectedHit] = []
    for hit in _rank_hits(resolved_outcomes)[: max(int(budgets.max_kb_hits), 0)]:
        content, cut = _fit(hit.content, remaining - LABEL_OVERHEAD_CHARS)
        if content is None:
            truncated = True
            break
        remaining -= len(content) + LABEL_OVERHEAD_CHARS
        selected_hits.append(SelectedHit(hit=hit, content=content, truncated=cut))
        truncated = truncated or cut

    return KnowledgeContext(
        query=query,
        budget=budgets,
        priors=tuple(selected_priors),
        hits=tuple(selected_hits),
        outcomes=resolved_outcomes,
        truncated=truncated,
    )


def source_priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, UNKNOWN_SOURCE_PRIORITY)


def _fit(text: str, allowed: int) -> tuple[str | None, bool]:
    if allowed < MIN_ITEM_CHARS:
        return None, False
    if len(text) <= allowed:
        return text, False
    return text[:allowed], True


def _prior_content(prior: Prior) -> str:
    reason = " | ".join(line.strip() for line in (prior.reason or "").splitlines() if line.strip())
    parts = [f"断言差异：{reason}" if reason else "断言差异：（原始失败信息为空）"]
    if prior.reproduce_command:
        parts.append(f"复现命令：{prior.reproduce_command}")
    return "\n".join(parts)


def _rank_hits(outcomes: Iterable[SearchOutcome]) -> list[Hit]:
    hits = [hit for outcome in outcomes for hit in outcome.hits]
    hits.sort(key=lambda hit: (source_priority(hit.source), -hit.score, hit.document, hit.chunk_index))
    ranked: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        identity = (hit.source, hit.chunk_id or f"{hit.knowledge_id}#{hit.chunk_index}")
        if identity in seen:
            continue
        seen.add(identity)
        ranked.append(hit)
    return ranked
