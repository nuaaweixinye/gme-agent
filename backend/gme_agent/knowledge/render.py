"""Render one assembled context as a prompt section.

Pure formatting: no I/O, no configuration, no clock. Three rules decide the shape
of the output:

- An empty section is never written: a source with no hits gets no heading, and a
  context with no priors, no hits and no degraded source renders as `""` — the
  caller reads that as "do not inject".
- Every rendered item carries its source label and a traceable id (prior:
  `failure_id`, interface and match kind; hit: `[kbNN]`, document,
  `knowledge_id`, chunk index, score).
- A trimmed item is marked with `TRUNCATION_MARK` so the model knows its text
  stops early.

`KnowledgeContext.truncated` means only "the character budget trimmed us". Items
dropped by `max_priors`/`max_kb_hits` are a designed cap and deliberately leave it
`False`, so the status block never claims the context is complete or incomplete:
it states what retrieval returned, which source degraded, and whether the budget
cost us text.
"""

from __future__ import annotations

from .assembler import KnowledgeContext, SelectedHit, SelectedPrior, source_priority

BLOCK_TITLE = "## 历史分歧与知识参照（GME Test Agent 自动注入）"
TRUNCATION_MARK = "（已截断）"

USAGE_RULES = (
    "使用规则：\n"
    "- `local` 是本地任务库记录的真实 GME/ACIS 分歧：优先生成能复现并进一步扩展它的用例，"
    "同时不要重复它已经用过的断言写法。\n"
    "- `kb01` 是 GME 当前事实，用于校准接口语义；`kb02` 是历史缺陷经验，只能当作假设；"
    "`kb03` 是 ACIS 与外部参照，用于确定期望侧并让测试有效；`kb00` 是知识规范，只用于统一术语与可信度口径。\n"
    "- 任何知识库内容都不得当作 GME 当前实现事实；与接口声明或实现代码冲突时以代码为准，"
    "并在 `.gme-agent/` 工件中记录该冲突。"
)


def render_knowledge_block(context: KnowledgeContext) -> str:
    """Render `context` as the injectable prompt section, or `""` when there is nothing to say."""

    if context.is_empty():
        return ""
    sections: list[str] = []
    if context.priors:
        sections.append(_priors_section(context.priors))
    for source in _hit_sources(context):
        sections.append(_hits_section(context, source))
    status = _status_section(context)
    if status:
        sections.append(status)
    if not sections:  # defensive: never emit a bare title with no content under it, whatever `is_empty()` says
        return ""
    return f"{BLOCK_TITLE}\n\n" + "\n\n".join(sections) + f"\n\n{USAGE_RULES}\n"


def _priors_section(priors: tuple[SelectedPrior, ...]) -> str:
    lines = ["### 本地分歧先验（source=local）"]
    for index, item in enumerate(priors, start=1):
        prior = item.prior
        status = "已确认分歧（升格）" if prior.confirmed else "疑似分歧（未达稳定复现阈值，只能作为假设）"
        lines.append(
            f"{index}. `{prior.key}` · 接口 `{prior.attribution}` · 模块 `{prior.module}`"
            f" · 匹配 {prior.match_kind} · 稳定复现 {prior.stable_runs} 次 · {status} · failure_id `{prior.failure_id}`"
        )
        lines.append(_indented(item))
    return "\n".join(lines)


def _hits_section(context: KnowledgeContext, source: str) -> str:
    lines = [f"### 知识库参照 {source}"]
    for index, item in enumerate([hit for hit in context.hits if hit.hit.source == source], start=1):
        hit = item.hit
        lines.append(
            f"{index}. [{hit.source}] {hit.document} · knowledge_id `{hit.knowledge_id}`"
            f" · chunk {hit.chunk_index} · score {hit.score:.4f}"
        )
        lines.append(_indented(item))
    return "\n".join(lines)


def _status_section(context: KnowledgeContext) -> str:
    lines: list[str] = []
    for entry in context.degraded_sources():
        lines.append(f"- {entry}")
    if not _retrieved_anything(context):
        lines.append("- 本次没有任何知识库命中，请完全依赖代码、接口声明与 `gme-acis-interface-analyzer` 的分析。")
    if context.truncated:
        lines.append(f"- 注入内容已按字数预算裁剪{TRUNCATION_MARK}，被省略的部分不在本提示词中。")
    if not lines:
        return ""
    return "### 检索状态\n" + "\n".join(lines)


def _indented(item: SelectedPrior | SelectedHit) -> str:
    text = item.content.strip()
    if item.truncated:
        text = f"{text}{TRUNCATION_MARK}"
    return "\n".join(f"   {line}" for line in text.splitlines())


def _hit_sources(context: KnowledgeContext) -> list[str]:
    sources = {item.hit.source for item in context.hits}
    return sorted(sources, key=lambda source: (source_priority(source), source))


def _retrieved_anything(context: KnowledgeContext) -> bool:
    """Whether retrieval returned any hit at all, before the budget had its say.

    Read from the raw outcomes rather than from `context.hits`, so the zero-hit
    sentence stays true when hits were dropped by `max_kb_hits` or by the character
    budget: a designed cap is not the same thing as a source that matched nothing.
    """

    return any(outcome.hits for outcome in context.outcomes)
