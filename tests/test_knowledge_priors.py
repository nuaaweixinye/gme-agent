from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.knowledge.local_priors import Prior, collect_priors, count_stable_runs  # noqa: E402
from gme_agent.storage.db import AgentDb  # noqa: E402


DERIVATIVE_INTERFACE = "laws-main_law-derivative_law-evaluate"
NMAAX_INTERFACE = "laws-kernel-api-nmax-of-law"
DIVERGENCE_REASON = (
    "D:\\worktrees\\tests\\gme\\src\\laws\\law_main_law_test.cpp:3017\n"
    "The difference between gme_answer and 1.0 is 1, which exceeds 1.0e-6, where\n"
    "gme_answer evaluates to 0"
)


class PriorFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.db = AgentDb(":memory:")
        self.addCleanup(self.db.close)
        self.db.create_job(
            job_id="job-1",
            job_type="test_generation",
            title="laws",
            module="laws",
            api_name="GME::derivative_law::evaluate",
            metadata={},
        )

    def add_failure(
        self,
        failure_id: str,
        *,
        test_name: str,
        interface_id: str,
        api_name: str,
        module: str = "laws",
        runs: int = 1,
        confirmed: bool = False,
        reason: str = DIVERGENCE_REASON,
    ) -> dict:
        failure = self.db.upsert_failure(
            failure_id=failure_id,
            job_id="job-1",
            test_suite="Laws_ClassTest",
            test_name=test_name,
            file="tests/gme/src/laws/law_main_law_test.cpp",
            line=3017,
            reason=reason,
            reproduce_command="tests.exe --gtest_filter=Laws_ClassTest." + test_name,
            skip_id=failure_id,
            metadata={
                "module": module,
                "interface_id": interface_id,
                "api_name": api_name,
                "gtest_filter": f"Laws_ClassTest.{test_name}",
            },
        )
        for index in range(runs):
            self.db.add_failure_observation(
                run_id=f"run-{failure_id}-{index}",
                failure_id=str(failure["id"]),
                job_id="job-1",
                outcome="failed",
                test_suite="Laws_ClassTest",
                test_name=test_name,
                file="tests/gme/src/laws/law_main_law_test.cpp",
                line=3017,
                reason=reason,
                gtest_filter=f"Laws_ClassTest.{test_name}",
            )
        if confirmed:
            metadata = dict(failure["metadata"])
            metadata["knowledge"] = {"confirmed_at": "2026-09-15T10:00:00+0800", "stability": runs, "injected_count": 3}
            failure = self.db.update_failure(str(failure["id"]), metadata=metadata)
        return failure


class CollectPriorsTests(PriorFixture):
    def test_interface_matches_rank_before_module_only_matches(self) -> None:
        self.add_failure("f-1", test_name="A", interface_id=DERIVATIVE_INTERFACE, api_name="GME::derivative_law::evaluate", runs=3)
        self.add_failure("f-2", test_name="B", interface_id=NMAAX_INTERFACE, api_name="api_nmax_of_law", runs=1)

        priors = collect_priors(
            self.db,
            module="laws",
            interface_ids=[DERIVATIVE_INTERFACE],
            api_names=["GME::derivative_law::evaluate"],
        )

        self.assertEqual([prior.test_name for prior in priors], ["A", "B"])
        self.assertEqual([prior.match_kind for prior in priors], ["interface", "module"])
        self.assertEqual(priors[0].stable_runs, 3)
        self.assertFalse(priors[0].confirmed)
        self.assertEqual(priors[0].key, "Laws_ClassTest.A")
        self.assertEqual(priors[0].attribution, DERIVATIVE_INTERFACE)
        self.assertIn("gme_answer", priors[0].reason)

    def test_api_name_matches_rank_between_interface_and_module(self) -> None:
        self.add_failure("f-1", test_name="ByInterface", interface_id=DERIVATIVE_INTERFACE, api_name="api_one", runs=1)
        self.add_failure("f-2", test_name="ByApi", interface_id="other-interface", api_name="api_two", runs=9)
        self.add_failure("f-3", test_name="ByModule", interface_id="third-interface", api_name="api_three", runs=9)

        priors = collect_priors(
            self.db,
            module="laws",
            interface_ids=[DERIVATIVE_INTERFACE],
            api_names=["api_two"],
        )

        self.assertEqual([prior.test_name for prior in priors], ["ByInterface", "ByApi", "ByModule"])
        self.assertEqual([prior.match_kind for prior in priors], ["interface", "api", "module"])

    def test_module_match_alone_still_injects_a_job_without_structured_selection(self) -> None:
        # A free-text task carries api_name "api_x" and no interface ids; the
        # module is the only signal left, and losing it would empty local injection.
        self.add_failure("f-1", test_name="A", interface_id=DERIVATIVE_INTERFACE, api_name="GME::derivative_law::evaluate")

        priors = collect_priors(self.db, module="laws", api_names=["api_x"])

        self.assertEqual([prior.test_name for prior in priors], ["A"])
        self.assertEqual(priors[0].match_kind, "module")

    def test_confirmed_priors_rank_before_observed_ones(self) -> None:
        self.add_failure("f-1", test_name="Unconfirmed", interface_id=DERIVATIVE_INTERFACE, api_name="api", runs=4)
        self.add_failure("f-2", test_name="Confirmed", interface_id=DERIVATIVE_INTERFACE, api_name="api", runs=2, confirmed=True)

        priors = collect_priors(self.db, module="laws", interface_ids=[DERIVATIVE_INTERFACE])

        self.assertEqual([prior.test_name for prior in priors], ["Confirmed", "Unconfirmed"])
        self.assertTrue(priors[0].confirmed)
        self.assertEqual(priors[0].injected_count, 3)

    def test_falls_back_to_module_when_no_selection_is_given(self) -> None:
        self.add_failure("f-1", test_name="A", interface_id=DERIVATIVE_INTERFACE, api_name="api")
        self.add_failure("f-2", test_name="B", interface_id="base.entity.copy", api_name="ENTITY::copy", module="base")

        priors = collect_priors(self.db, module="laws")

        self.assertEqual([prior.test_name for prior in priors], ["A"])

    def test_failures_without_attribution_are_never_injected(self) -> None:
        self.add_failure("f-1", test_name="NoAttribution", interface_id="", api_name="")

        self.assertEqual(collect_priors(self.db, module="laws"), [])

    def test_limit_is_respected(self) -> None:
        for index in range(4):
            self.add_failure(
                f"f-{index}",
                test_name=f"T{index}",
                interface_id=DERIVATIVE_INTERFACE,
                api_name="api",
                runs=index + 1,
            )

        priors = collect_priors(self.db, module="laws", interface_ids=[DERIVATIVE_INTERFACE], limit=2)

        self.assertEqual([prior.stable_runs for prior in priors], [4, 3])

    def test_count_stable_runs_counts_distinct_failed_run_ids(self) -> None:
        failure = self.add_failure("f-1", test_name="A", interface_id=DERIVATIVE_INTERFACE, api_name="api", runs=2)
        self.db.add_failure_observation(
            run_id="run-f-1-0",
            failure_id="f-1",
            job_id="job-1",
            outcome="passed",
            test_suite="Laws_ClassTest",
            test_name="A",
        )

        self.assertEqual(count_stable_runs(self.db, "f-1"), 2)
        self.assertEqual(count_stable_runs(self.db, "missing"), 0)
        self.assertEqual(failure["id"], "f-1")


if __name__ == "__main__":
    unittest.main()
