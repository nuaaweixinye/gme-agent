from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.settings.config import (  # noqa: E402
    AgentConfig,
    KnowledgeConfig,
    config_from_json,
    load_config,
    save_config,
)


KB_IDS = {
    "kb00": "<kb00-knowledge-base-id>",
    "kb01": "<kb01-knowledge-base-id>",
    "kb02": "<kb02-knowledge-base-id>",
    "kb03": "<kb03-knowledge-base-id>",
}


class KnowledgeConfigDefaultsTests(unittest.TestCase):
    def test_defaults_are_disabled_and_carry_placeholder_knowledge_bases(self) -> None:
        config = AgentConfig()

        self.assertFalse(config.knowledge.enabled)
        # Deployment-specific values (a real WeKnora URL, real knowledge-base ids)
        # live in config.local.json, which git ignores; the shipped defaults are
        # neutral placeholders that degrade with a clear message.
        self.assertEqual(config.knowledge.weknora.base_url, "")
        self.assertEqual(config.knowledge.weknora.api_key_env, "WEKNORA_API_KEY")
        self.assertEqual(config.knowledge.weknora.timeout_ms, 3000)
        self.assertEqual(config.knowledge.weknora.source_ids(), KB_IDS)
        self.assertEqual(config.knowledge.budgets.max_priors, 8)
        self.assertEqual(config.knowledge.budgets.max_kb_hits, 6)
        self.assertEqual(config.knowledge.budgets.max_chars, 4000)
        self.assertTrue(config.knowledge.closed_loop.enabled)
        self.assertEqual(config.knowledge.closed_loop.min_stable_runs, 2)


class KnowledgeConfigLoadingTests(unittest.TestCase):
    def test_load_config_without_a_knowledge_block_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model": "deepseek-v4-flash"}), encoding="utf-8")

            config = load_config(path)

            self.assertIsInstance(config.knowledge, KnowledgeConfig)
            self.assertFalse(config.knowledge.enabled)
            self.assertEqual(config.knowledge.weknora.source_ids(), KB_IDS)

    def test_round_trip_preserves_nested_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = AgentConfig(
                knowledge=KnowledgeConfig(
                    enabled=True,
                    weknora=KnowledgeConfig().weknora.from_dict(
                        {"base_url": "http://127.0.0.1:9/api/v1", "timeout_ms": 1500}
                    ),
                    budgets=KnowledgeConfig().budgets.from_dict({"max_kb_hits": 2}),
                    closed_loop=KnowledgeConfig().closed_loop.from_dict({"min_stable_runs": 1}),
                )
            )
            save_config(path, config)

            reloaded = load_config(path)

            self.assertTrue(reloaded.knowledge.enabled)
            self.assertEqual(reloaded.knowledge.weknora.base_url, "http://127.0.0.1:9/api/v1")
            self.assertEqual(reloaded.knowledge.weknora.timeout_ms, 1500)
            self.assertEqual(reloaded.knowledge.weknora.source_ids(), KB_IDS)
            self.assertEqual(reloaded.knowledge.budgets.max_kb_hits, 2)
            self.assertEqual(reloaded.knowledge.budgets.max_priors, 8)
            self.assertEqual(reloaded.knowledge.closed_loop.min_stable_runs, 1)
            self.assertTrue(reloaded.knowledge.closed_loop.enabled)

    def test_partial_update_keeps_sibling_defaults(self) -> None:
        current = AgentConfig()
        updated = config_from_json({"knowledge": {"enabled": True}}, current)

        self.assertTrue(updated.knowledge.enabled)
        self.assertEqual(updated.knowledge.weknora.source_ids(), KB_IDS)
        self.assertEqual(updated.knowledge.budgets.max_chars, 4000)

    def test_broken_knowledge_block_falls_back_to_defaults(self) -> None:
        # Wrong *types* are the real degradation case: a hand-edited config can
        # carry anything, and `resolved()` must not raise out of `load_config`.
        config = AgentConfig(
            knowledge={
                "enabled": [],
                "weknora": {"knowledge_bases": 5, "timeout_ms": "soon"},
                "budgets": None,
                "closed_loop": "yes",
            }
        )

        resolved = config.resolved()

        self.assertFalse(resolved.knowledge.enabled)
        self.assertEqual(resolved.knowledge.weknora.source_ids(), KB_IDS)
        self.assertEqual(resolved.knowledge.weknora.timeout_ms, 3000)
        self.assertEqual(resolved.knowledge.budgets.max_chars, 4000)
        self.assertTrue(resolved.knowledge.closed_loop.enabled)

    def test_canonical_string_booleans_are_accepted(self) -> None:
        for spelling in ("true", "True", "1", "yes", "on"):
            self.assertTrue(KnowledgeConfig.from_dict({"enabled": spelling}).enabled, spelling)
        for spelling in ("false", "False", "0", "no", "off"):
            self.assertFalse(KnowledgeConfig.from_dict({"enabled": spelling}).enabled, spelling)

    def test_non_finite_numbers_degrade_through_load_config(self) -> None:
        # json.loads accepts bare NaN/Infinity; int(nan) raises ValueError and
        # int(inf) raises OverflowError, so load_config must not crash on them.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                '{"knowledge": {"weknora": {"timeout_ms": NaN}, "budgets": {"max_chars": Infinity}}}',
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual(config.knowledge.weknora.timeout_ms, 3000)
            self.assertEqual(config.knowledge.budgets.max_chars, 4000)

    def test_config_is_json_serializable(self) -> None:
        payload = json.loads(json.dumps(__import__("dataclasses").asdict(AgentConfig())))

        self.assertIn("knowledge", payload)
        self.assertFalse(payload["knowledge"]["enabled"])
        self.assertEqual(len(payload["knowledge"]["weknora"]["knowledge_bases"]), 4)


if __name__ == "__main__":
    unittest.main()
