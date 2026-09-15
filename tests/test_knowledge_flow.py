from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.flows.test_generation_flow import run_test_extension_job, run_test_generation_job  # noqa: E402
from gme_agent.harness.runner import HarnessResult  # noqa: E402
from gme_agent.services.orchestrator import Orchestrator  # noqa: E402
from gme_agent.settings.config import AgentConfig, KnowledgeConfig  # noqa: E402
from gme_agent.storage.db import AgentDb  # noqa: E402


INTERFACE = "laws-main_law-derivative_law-evaluate"
DIVERGENCE_REASON = "The difference between gme_answer and 1.0 is 1, which exceeds 1.0e-6"
GTEST_FAILURE_OUTPUT = (
    "[==========] Running 1 test from 1 test suite.\n"
    "[ RUN      ] Laws_ClassTest.Stable\n"
    "law_main_law_test.cpp:3017: Failure\n"
    f"{DIVERGENCE_REASON}\n"
    "[  FAILED  ] Laws_ClassTest.Stable (1 ms)\n"
)

GTEST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="1" failures="1">
  <testsuite name="Laws_ClassTest" tests="1" failures="1">
    <testcase classname="Laws_ClassTest" name="Stable" file="tests/gme/src/laws/law_main_law_test.cpp" line="3017" status="run">
      <failure message="The difference between gme_answer and 1.0 is 1, which exceeds 1.0e-6" type=""><![CDATA[law_main_law_test.cpp:3017: Failure
The difference between gme_answer and 1.0 is 1, which exceeds 1.0e-6]]></failure>
    </testcase>
  </testsuite>
</testsuites>
"""


def _knowledge_config(enabled: bool = True, *, base_url: str = "http://127.0.0.1:1/api/v1") -> KnowledgeConfig:
    return KnowledgeConfig.from_dict(
        {
            "enabled": enabled,
            # 空的 knowledge_bases 会回落到默认四库（kb00..kb03），这样测试断言的就是真实标签顺序。
            "weknora": {"base_url": base_url, "api_key_env": "GME_TEST_MISSING_KEY", "knowledge_bases": []},
            "budgets": {"max_priors": 8, "max_kb_hits": 6, "max_chars": 4000},
            "closed_loop": {"enabled": True, "min_stable_runs": 1},
        }
    )


class GenerationFlowInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = AgentDb(":memory:")
        self.addCleanup(self.db.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worktree = Path(self.tmp.name) / "worktree"
        (self.worktree / ".gme-agent").mkdir(parents=True)
        (self.worktree / ".gme-agent" / "generated_tests.json").write_text(
            json.dumps({"tests": [{"file": "src/laws/law_main_law_test.cpp", "suite": "Laws_ClassTest", "name": "Stable"}]}),
            encoding="utf-8",
        )

    def _seed_failure(self, *, runs: int = 1) -> None:
        self.db.upsert_failure(
            failure_id="gmefail-1",
            job_id="job-1",
            test_suite="Laws_ClassTest",
            test_name="Stable",
            file="tests/gme/src/laws/law_main_law_test.cpp",
            line=3017,
            reason=DIVERGENCE_REASON,
            reproduce_command="tests.exe --gtest_filter=Laws_ClassTest.Stable",
            skip_id="gmefail-1",
            metadata={"module": "laws", "interface_id": INTERFACE, "api_name": "GME::derivative_law::evaluate"},
        )
        for index in range(runs):
            self.db.add_failure_observation(
                run_id=f"run-{index}",
                failure_id="gmefail-1",
                job_id="job-1",
                outcome="failed",
                test_suite="Laws_ClassTest",
                test_name="Stable",
            )

    def _run_generation(self, *, knowledge: KnowledgeConfig, failures: list[dict] | None = None) -> dict[str, Any]:
        config = AgentConfig(
            artifact_root=str(Path(self.tmp.name) / "artifacts"),
            knowledge=knowledge,
            auto_run_build=False,
            auto_run_tests=failures is not None,
        )
        orchestrator = Orchestrator(config, self.db)
        self.db.create_job(job_id="job-1", job_type="test_generation", title="laws", module="laws", api_name="api_x")
        if failures is not None:
            # record_failures reads the authoritative GTest XML, and its failure
            # message is what the divergence classifier sees in production.
            artifact_dir = Path(config.artifact_root) / "job-1"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "gtest.xml").write_text(GTEST_XML, encoding="utf-8")
        worktree = types.SimpleNamespace(branch="job-branch", path=self.worktree)
        target = types.SimpleNamespace(
            rel_path="tests/gme",
            branch="target-branch",
            path=self.worktree / "tests/gme",
            base_branch="main",
        )
        captured: dict[str, Any] = {}

        def fake_run(runner_self, prompt, cwd, session_id=None, skill_names=None, on_session_started=None):
            captured["prompt"] = prompt
            if on_session_started:
                on_session_started("gme-session")
            return HarnessResult(final_response="done", session_id="gme-session", finish_reason="completed", raw="")

        patches = [
            mock.patch("gme_agent.flows.test_generation_flow.create_worktree", return_value=worktree),
            mock.patch("gme_agent.flows.test_generation_flow.prepare_worktree_dependencies", return_value=[]),
            mock.patch("gme_agent.flows.test_generation_flow.prepare_target_repo_from_remote", return_value=target),
            mock.patch("gme_agent.flows.test_generation_flow.ensure_only_target_repo_changed"),
            mock.patch(
                "gme_agent.flows.test_generation_flow._generated_manifest_metadata",
                return_value={"generated_gtest_filter": "Laws_ClassTest.Stable"},
            ),
            mock.patch("gme_agent.flows.test_generation_flow.HarnessRunner.run", new=fake_run),
            mock.patch.object(orchestrator, "_write_job_artifacts"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        if failures is not None:
            with mock.patch.object(orchestrator, "_run_tests", return_value=GTEST_FAILURE_OUTPUT):
                run_test_generation_job(orchestrator, "job-1", "laws", "api_x", "tests/gme", None)
        else:
            run_test_generation_job(orchestrator, "job-1", "laws", "api_x", "tests/gme", None)
        captured["artifact_dir"] = Path(config.artifact_root) / "job-1"
        captured["job"] = self.db.get_job("job-1")
        captured["events"] = self.db.list_events("job-1")
        return captured

    def test_disabled_knowledge_changes_nothing(self) -> None:
        self._seed_failure()
        result = self._run_generation(knowledge=_knowledge_config(enabled=False))

        self.assertFalse((result["artifact_dir"] / "knowledge_context.md").exists())
        self.assertNotIn("knowledge", result["job"]["metadata"])
        self.assertEqual([event for event in result["events"] if event["message"].startswith("knowledge/")], [])

    def test_enabled_knowledge_injects_priors_writes_artifact_and_records_metadata(self) -> None:
        self._seed_failure(runs=1)
        result = self._run_generation(knowledge=_knowledge_config())

        artifact = result["artifact_dir"] / "knowledge_context.md"
        self.assertTrue(artifact.exists())
        text = artifact.read_text(encoding="utf-8")
        self.assertIn("# 知识注入记录", text)
        self.assertIn("Laws_ClassTest.Stable", text)
        self.assertIn("kb02: API key is not set", text)
        self.assertIn("Laws_ClassTest.Stable", result["prompt"])
        self.assertIn("## 历史分歧与知识参照", result["prompt"])

        metadata = result["job"]["metadata"]["knowledge"]
        self.assertTrue(metadata["enabled"])
        self.assertEqual(metadata["prior_count"], 1)
        self.assertEqual([source["label"] for source in metadata["sources"]], ["kb00", "kb01", "kb02", "kb03"])
        self.assertEqual(metadata["hit_count"], 0)

        messages = [event["message"] for event in result["events"]]
        self.assertTrue(any(message.startswith("knowledge/assembled") for message in messages))

    def test_recorded_failure_is_promoted_when_it_meets_the_criteria(self) -> None:
        self._seed_failure(runs=0)
        result = self._run_generation(knowledge=_knowledge_config(), failures=[])

        knowledge = self.db.get_failure("gmefail-1")["metadata"]["knowledge"]
        self.assertEqual(knowledge["stability"], 1)
        self.assertIn("confirmed_at", knowledge)
        messages = [event["message"] for event in result["events"]]
        self.assertTrue(any(message.startswith("knowledge/promoted") for message in messages))

    def test_promotion_is_skipped_when_knowledge_is_disabled(self) -> None:
        self._seed_failure(runs=0)
        self._run_generation(knowledge=_knowledge_config(enabled=False), failures=[])

        self.assertNotIn("knowledge", self.db.get_failure("gmefail-1")["metadata"])

    def test_an_unreadable_manifest_and_a_broken_event_writer_still_do_not_fail_the_task(self) -> None:
        # Both halves of the "promotion never fails the task" promise in one run: the
        # manifest read raises, and the job-event writer raises while reporting it.
        self._seed_failure(runs=0)

        def job_emit(orchestrator_self, job_id: str):
            def emit(level: str, message: str) -> None:
                if message.startswith("knowledge/promotion"):
                    raise RuntimeError("database is closed")
                self.db.add_event(job_id, level, message)

            return emit

        with mock.patch.object(Orchestrator, "_job_emit", job_emit), mock.patch(
            "gme_agent.flows.test_generation_flow.load_generated_tests_manifest",
            side_effect=OSError("manifest is gone"),
        ):
            result = self._run_generation(knowledge=_knowledge_config(), failures=[])

        self.assertEqual(result["job"]["status"], "needs_review")
        self.assertEqual(result["job"]["error"] or "", "")
        # Injection still counted itself; promotion is what must not have happened.
        knowledge = self.db.get_failure("gmefail-1")["metadata"].get("knowledge") or {}
        self.assertEqual(knowledge.get("injected_count"), 1)
        self.assertNotIn("confirmed_at", knowledge)

    def test_broken_transport_never_fails_the_task(self) -> None:
        import os

        os.environ["GME_TEST_MISSING_KEY"] = "stub-key"
        self.addCleanup(os.environ.pop, "GME_TEST_MISSING_KEY", None)
        self._seed_failure()
        result = self._run_generation(knowledge=_knowledge_config(base_url="not-a-url"))

        self.assertEqual(result["job"]["status"], "needs_review")
        self.assertEqual(result["job"]["error"] or "", "")
        metadata = result["job"]["metadata"]["knowledge"]
        self.assertTrue(metadata["enabled"])
        self.assertEqual(metadata["hit_count"], 0)
        self.assertEqual(metadata["prior_count"], 1)
        self.assertTrue(all(source["error"] for source in metadata["sources"]))
        self.assertTrue((result["artifact_dir"] / "knowledge_context.md").exists())
        self.assertIn("### 检索状态", (result["artifact_dir"] / "knowledge_context.md").read_text(encoding="utf-8"))


class ExtensionFlowInjectionTests(unittest.TestCase):
    def test_extension_job_injects_the_previous_failure_as_a_prior(self) -> None:
        db = AgentDb(":memory:")
        self.addCleanup(db.close)
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            (worktree / ".gme-agent").mkdir(parents=True)
            (worktree / ".gme-agent" / "generated_tests.json").write_text('{"tests": [], "files": []}', encoding="utf-8")
            config = AgentConfig(
                artifact_root=str(Path(tmp) / "artifacts"),
                knowledge=_knowledge_config(),
                auto_run_build=False,
                auto_run_tests=False,
            )
            orchestrator = Orchestrator(config, db)
            db.create_job(job_id="job-2", job_type="test_generation", title="laws", module="laws", api_name="api_x",
                          metadata={"target_repo": "tests/gme", "prepared_paths": []})
            db.update_job("job-2", status="needs_review", worktree_path=str(worktree), harness_session_id="gme-session")
            db.upsert_failure(
                failure_id="gmefail-1",
                job_id="job-2",
                test_suite="Laws_ClassTest",
                test_name="Stable",
                file="tests/gme/src/laws/law_main_law_test.cpp",
                line=3017,
                reason=DIVERGENCE_REASON,
                reproduce_command="tests.exe --gtest_filter=Laws_ClassTest.Stable",
                metadata={"module": "laws", "interface_id": INTERFACE, "api_name": "GME::derivative_law::evaluate"},
            )
            db.add_failure_observation(
                run_id="run-1",
                failure_id="gmefail-1",
                job_id="job-2",
                outcome="failed",
                test_suite="Laws_ClassTest",
                test_name="Stable",
            )
            captured: dict[str, Any] = {}

            def fake_run(runner_self, prompt, cwd, session_id=None, skill_names=None, on_session_started=None):
                captured["prompt"] = prompt
                return HarnessResult(final_response="extended", session_id=session_id, finish_reason="completed", raw="")

            with mock.patch("gme_agent.flows.test_generation_flow.ensure_only_target_repo_changed"), \
                 mock.patch("gme_agent.flows.test_generation_flow._generated_manifest_metadata", return_value={}), \
                 mock.patch("gme_agent.flows.test_generation_flow.HarnessRunner.run", new=fake_run), \
                 mock.patch.object(orchestrator, "_write_job_artifacts"):
                run_test_extension_job(orchestrator, "job-2", "api_y", None)

            self.assertIn("本地分歧先验", captured["prompt"])
            self.assertTrue((Path(config.artifact_root) / "job-2" / "knowledge_context.md").exists())


if __name__ == "__main__":
    unittest.main()
