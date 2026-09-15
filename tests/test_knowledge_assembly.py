from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.knowledge.assembler import (  # noqa: E402
    SOURCE_PRIORITY,
    KnowledgeBudget,
    assemble_knowledge_context,
)
from gme_agent.knowledge.local_priors import Prior  # noqa: E402
from gme_agent.knowledge.weknora import Hit, SearchOutcome  # noqa: E402
from gme_agent.knowledge.render import TRUNCATION_MARK, render_knowledge_block  # noqa: E402


def _prior(name: str, *, reason: str = "gme_answer 与 ACIS 不一致", confirmed: bool = False, stable_runs: int = 1, match_kind: str = "interface") -> Prior:
    return Prior(
        failure_id=f"gmefail-{name}",
        test_suite="Laws_ClassTest",
        test_name=name,
        file="tests/gme/src/laws/law_main_law_test.cpp",
        reason=reason,
        reproduce_command=f"tests.exe --gtest_filter=Laws_ClassTest.{name}",
        interface_id="laws-main_law-derivative_law-evaluate",
        api_name="GME::derivative_law::evaluate",
        module="laws",
        stable_runs=stable_runs,
        injected_count=0,
        confirmed=confirmed,
        match_kind=match_kind,
    )


def _hit(source: str, chunk: str, *, content: str = "知识内容", score: float = 0.01, document: str = "doc.md") -> Hit:
    return Hit(
        source=source,
        knowledge_id=f"doc-{document}",
        chunk_id=chunk,
        document=document,
        chunk_index=1,
        score=score,
        content=content,
    )


class AssembleKnowledgeContextTests(unittest.TestCase):
    def test_empty_inputs_produce_an_empty_context(self) -> None:
        context = assemble_knowledge_context(
            priors=[],
            outcomes=[SearchOutcome(source="kb03")],
            budget=KnowledgeBudget(),
            query="laws",
        )

        self.assertTrue(context.is_empty())
        self.assertEqual(context.degraded_sources(), [])

    def test_degraded_source_alone_is_not_empty(self) -> None:
        context = assemble_knowledge_context(
            priors=[],
            outcomes=[SearchOutcome(source="kb02", error="HTTP 500: knowledge base not found")],
            budget=KnowledgeBudget(),
            query="laws",
        )

        self.assertFalse(context.is_empty())
        self.assertEqual(context.degraded_sources(), ["kb02: HTTP 500: knowledge base not found"])

    def test_hits_are_ranked_by_source_priority_then_score(self) -> None:
        outcomes = [
            SearchOutcome(source="kb00", hits=(_hit("kb00", "a", score=0.99),)),
            SearchOutcome(source="kb03", hits=(_hit("kb03", "b", score=0.01),)),
            SearchOutcome(
                source="kb01",
                hits=(_hit("kb01", "c", score=0.02), _hit("kb01", "d", score=0.05, document="other.md")),
            ),
        ]

        context = assemble_knowledge_context(
            priors=[],
            outcomes=outcomes,
            budget=KnowledgeBudget(),
            query="laws",
        )

        self.assertEqual([item.hit.chunk_id for item in context.hits], ["d", "c", "b", "a"])
        self.assertEqual(SOURCE_PRIORITY["kb01"], 1)

    def test_duplicate_chunks_from_one_source_are_dropped(self) -> None:
        outcomes = [
            SearchOutcome(source="kb03", hits=(_hit("kb03", "dup", score=0.5), _hit("kb03", "dup", score=0.4))),
        ]

        context = assemble_knowledge_context(
            priors=[],
            outcomes=outcomes,
            budget=KnowledgeBudget(),
            query="laws",
        )

        self.assertEqual(len(context.hits), 1)

    def test_prior_and_hit_limits_are_enforced(self) -> None:
        priors = [_prior(f"T{index}") for index in range(5)]
        outcomes = [SearchOutcome(source="kb03", hits=tuple(_hit("kb03", f"c{index}") for index in range(5)))]

        context = assemble_knowledge_context(
            priors=priors,
            outcomes=outcomes,
            budget=KnowledgeBudget(max_priors=2, max_kb_hits=3, max_chars=40000),
            query="laws",
        )

        self.assertEqual(len(context.priors), 2)
        self.assertEqual(len(context.hits), 3)

    def test_char_budget_truncates_the_lowest_priority_items(self) -> None:
        long_reason = "分歧细节" * 400
        outcomes = [
            SearchOutcome(source="kb03", hits=(_hit("kb03", "c1", content="命中内容" * 300),)),
        ]

        context = assemble_knowledge_context(
            priors=[_prior("T1", reason=long_reason)],
            outcomes=outcomes,
            budget=KnowledgeBudget(max_priors=8, max_kb_hits=6, max_chars=1200),
            query="laws",
        )

        self.assertTrue(context.truncated)
        self.assertEqual(len(context.priors), 1)
        self.assertTrue(context.priors[0].truncated)
        self.assertLessEqual(len(context.priors[0].content), 1200)
        self.assertEqual(context.hits, ())

    def test_items_that_do_not_fit_are_dropped_entirely(self) -> None:
        context = assemble_knowledge_context(
            priors=[_prior("T1"), _prior("T2")],
            outcomes=[],
            budget=KnowledgeBudget(max_chars=400),
            query="laws",
        )

        self.assertTrue(context.truncated)
        self.assertEqual(len(context.priors), 1)
        self.assertFalse(context.priors[0].truncated)
        self.assertEqual(context.priors[0].prior.test_name, "T1")

    def test_a_budget_too_small_for_one_item_yields_nothing(self) -> None:
        context = assemble_knowledge_context(
            priors=[_prior("T1")],
            outcomes=[],
            budget=KnowledgeBudget(max_chars=120),
            query="laws",
        )

        self.assertTrue(context.truncated)
        self.assertEqual(context.priors, ())
        self.assertTrue(context.is_empty())

    def test_prior_content_carries_reason_and_reproduce_command(self) -> None:
        context = assemble_knowledge_context(
            priors=[_prior("T1", reason="第一行\n第二行")],
            outcomes=[],
            budget=KnowledgeBudget(),
            query="laws",
        )

        content = context.priors[0].content
        self.assertIn("第一行 | 第二行", content)
        self.assertIn("--gtest_filter=Laws_ClassTest.T1", content)


class RenderKnowledgeBlockTests(unittest.TestCase):
    def test_empty_context_renders_nothing(self) -> None:
        context = assemble_knowledge_context(
            priors=[],
            outcomes=[SearchOutcome(source="kb03")],
            budget=KnowledgeBudget(),
            query="laws",
        )

        self.assertEqual(render_knowledge_block(context), "")

    def test_priors_and_hits_render_with_source_labels_and_usage_rules(self) -> None:
        context = assemble_knowledge_context(
            priors=[_prior("T1", confirmed=True, stable_runs=2), _prior("T2")],
            outcomes=[SearchOutcome(source="kb03", hits=(_hit("kb03", "c1", document="design.md"),))],
            budget=KnowledgeBudget(),
            query="laws derivative_law",
        )

        block = render_knowledge_block(context)

        self.assertIn("## 历史分歧与知识参照", block)
        self.assertIn("### 本地分歧先验（source=local）", block)
        self.assertIn("Laws_ClassTest.T1", block)
        self.assertIn("已确认分歧（升格）", block)
        self.assertIn("疑似分歧（未达稳定复现阈值", block)
        self.assertIn("`laws-main_law-derivative_law-evaluate`", block)
        self.assertIn("稳定复现 2 次", block)
        self.assertIn("匹配 interface", block)
        self.assertIn("### 知识库参照 kb03", block)
        self.assertIn("design.md", block)
        self.assertIn("chunk 1", block)
        self.assertIn("score 0.0100", block)
        self.assertIn("使用规则", block)
        self.assertNotIn("### 知识库参照 kb01", block)
        self.assertNotIn("### 检索状态", block)

    def test_degraded_source_is_stated_and_truncation_is_marked(self) -> None:
        context = assemble_knowledge_context(
            priors=[_prior("T1", reason="分歧细节" * 200)],
            outcomes=[SearchOutcome(source="kb02", error="HTTP 500: knowledge base not found")],
            budget=KnowledgeBudget(max_chars=900),
            query="laws",
        )

        block = render_knowledge_block(context)

        self.assertIn("### 检索状态", block)
        self.assertIn("kb02: HTTP 500: knowledge base not found", block)
        self.assertIn(TRUNCATION_MARK, block)

    def test_zero_hits_without_degradation_states_that_nothing_matched(self) -> None:
        context = assemble_knowledge_context(
            priors=[],
            outcomes=[SearchOutcome(source="kb03"), SearchOutcome(source="kb01")],
            budget=KnowledgeBudget(),
            query="laws",
        )

        self.assertEqual(render_knowledge_block(context), "")


if __name__ == "__main__":
    unittest.main()
