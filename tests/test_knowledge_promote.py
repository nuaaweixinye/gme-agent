from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.knowledge.promote import (  # noqa: E402
    is_divergence_reason,
    promote_confirmed_failures,
    record_injection_counts,
)
from gme_agent.storage.db import AgentDb  # noqa: E402


INTERFACE = "laws-main_law-derivative_law-evaluate"
ACIS_DIVERGENCE = (
    "D:\\worktrees\\tests\\gme\\src\\laws\\kernel_kernapi_test.cpp:383\n"
    "Value of: same_acis_gme(gme_result, acis_result)\n"
    "  Actual: false\nExpected: true"
)
ANSWER_DIVERGENCE = (
    "D:\\worktrees\\tests\\gme\\src\\laws\\law_main_law_test.cpp:3017\n"
    "The difference between gme_answer and 1.0 is 1, which exceeds 1.0e-6, where\n"
    "gme_answer evaluates to 0"
)
LINK_ERROR = (
    "D:\\worktrees\\tests\\gme\\src\\laws\\law_main_law_test.cpp:1\n"
    "error LNK2019: unresolved external symbol \"public: double __cdecl law::zero(double)\""
)
TIMEOUT = "The test timed out after 30 seconds while evaluating gme_answer"
GME_FIRST_DIVERGENCE = (
    "D:\\worktrees\\tests\\gme\\src\\laws\\law_main_law_test.cpp:3133\n"
    "The difference between gme_first and 2.0 is 2, which exceeds 1.0e-4, where\n"
    "gme_first evaluates to 0"
)


class DivergenceReasonTests(unittest.TestCase):
    def test_real_divergence_reasons_are_recognised(self) -> None:
        self.assertTrue(is_divergence_reason(ACIS_DIVERGENCE))
        self.assertTrue(is_divergence_reason(ANSWER_DIVERGENCE))

    def test_every_helper_name_in_the_corpus_is_recognised(self) -> None:
        # The recorded corpus compares gme_answer, gme_result, gme_first and
        # acis_result; a fixed name list missed gme_first, so the family is matched
        # by identifier shape instead.
        self.assertTrue(is_divergence_reason(GME_FIRST_DIVERGENCE))

    def test_invalid_test_reasons_are_excluded(self) -> None:
        self.assertFalse(is_divergence_reason(LINK_ERROR))
        self.assertFalse(is_divergence_reason(TIMEOUT))
        self.assertFalse(is_divergence_reason(""))
        self.assertFalse(is_divergence_reason("Assertion failed: value == 3"))


class PromoteConfirmedFailuresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = AgentDb(":memory:")
        self.addCleanup(self.db.close)
        self.db.create_job(job_id="job-1", job_type="test_generation", title="laws", module="laws", api_name="api_x")
        self.events: list[tuple[str, str]] = []

    def _failure(self, name: str, *, reason: str = ANSWER_DIVERGENCE, interface_id: str = INTERFACE, api_name: str = "GME::derivative_law::evaluate", runs: int = 2) -> dict:
        failure = self.db.upsert_failure(
            failure_id=f"gmefail-{name}",
            job_id="job-1",
            test_suite="Laws_ClassTest",
            test_name=name,
            file="tests/gme/src/laws/law_main_law_test.cpp",
            line=3017,
            reason=reason,
            reproduce_command=f"tests.exe --gtest_filter=Laws_ClassTest.{name}",
            skip_id=f"gmefail-{name}",
            metadata={"module": "laws", "interface_id": interface_id, "api_name": api_name},
        )
        for index in range(runs):
            self.db.add_failure_observation(
                run_id=f"run-{index}",
                failure_id=str(failure["id"]),
                job_id="job-1",
                outcome="failed",
                test_suite="Laws_ClassTest",
                test_name=name,
            )
        return failure

    def _manifest(self, *names: str) -> dict:
        return {
            "tests": [
                {"file": "src/laws/law_main_law_test.cpp", "suite": "Laws_ClassTest", "name": name}
                for name in names
            ]
        }

    def _promote(self, failures: list[dict], manifest: dict, *, threshold: int = 2, events: bool = True) -> list[str]:
        return promote_confirmed_failures(
            self.db,
            failures=failures,
            manifest=manifest,
            min_stable_runs=threshold,
            on_event=(lambda level, message: self.events.append((level, message))) if events else None,
        )

    def test_stable_attributed_divergence_still_in_the_manifest_is_promoted(self) -> None:
        failure = self._failure("Stable", runs=2)

        promoted = self._promote([failure], self._manifest("Stable"))

        self.assertEqual(promoted, ["gmefail-Stable"])
        knowledge = self.db.get_failure("gmefail-Stable")["metadata"]["knowledge"]
        self.assertEqual(knowledge["stability"], 2)
        self.assertEqual(knowledge["injected_count"], 0)
        self.assertIn("confirmed_at", knowledge)
        self.assertEqual(self.db.get_failure("gmefail-Stable")["status"], "open")
        self.assertTrue(any(message.startswith("knowledge/promoted") for _level, message in self.events))

    def test_single_run_is_not_promoted(self) -> None:
        failure = self._failure("Once", runs=1)

        promoted = self._promote([failure], self._manifest("Once"))

        self.assertEqual(promoted, [])
        self.assertNotIn("knowledge", self.db.get_failure("gmefail-Once")["metadata"])

    def test_threshold_one_promotes_a_single_run(self) -> None:
        failure = self._failure("Once", runs=1)

        promoted = self._promote([failure], self._manifest("Once"), threshold=1)

        self.assertEqual(promoted, ["gmefail-Once"])
        self.assertEqual(self.db.get_failure("gmefail-Once")["metadata"]["knowledge"]["stability"], 1)

    def test_test_removed_from_the_manifest_is_not_promoted(self) -> None:
        failure = self._failure("Removed", runs=3)

        promoted = self._promote([failure], self._manifest("Other"))

        self.assertEqual(promoted, [])

    def test_failure_without_attribution_is_not_promoted(self) -> None:
        failure = self._failure("NoAttribution", interface_id="", api_name="", runs=3)

        promoted = self._promote([failure], self._manifest("NoAttribution"))

        self.assertEqual(promoted, [])

    def test_invalid_test_failure_is_not_promoted(self) -> None:
        failure = self._failure("LinkError", reason=LINK_ERROR, runs=3)

        promoted = self._promote([failure], self._manifest("LinkError"))

        self.assertEqual(promoted, [])

    def test_existing_injected_count_is_preserved(self) -> None:
        failure = self._failure("Counted", runs=2)
        metadata = dict(failure["metadata"])
        metadata["knowledge"] = {"injected_count": 4}
        self.db.update_failure("gmefail-Counted", metadata=metadata)

        self._promote([failure], self._manifest("Counted"))

        knowledge = self.db.get_failure("gmefail-Counted")["metadata"]["knowledge"]
        self.assertEqual(knowledge["injected_count"], 4)
        self.assertEqual(knowledge["stability"], 2)

    def test_a_corrupt_injected_count_does_not_raise(self) -> None:
        failure = self._failure("Corrupt", runs=2)
        metadata = dict(failure["metadata"])
        metadata["knowledge"] = {"injected_count": "many"}
        self.db.update_failure("gmefail-Corrupt", metadata=metadata)

        promoted = self._promote([failure], self._manifest("Corrupt"))

        self.assertEqual(promoted, ["gmefail-Corrupt"])
        knowledge = self.db.get_failure("gmefail-Corrupt")["metadata"]["knowledge"]
        self.assertEqual(knowledge["injected_count"], 0)
        self.assertEqual(knowledge["stability"], 2)

    def test_corrupt_metadata_never_raises(self) -> None:
        # A hand-edited metadata_json can be a JSON array; `dict()` on one raises, and a
        # raise inside the promotion loop would leave earlier promotions applied while the
        # caller loses the returned list. The corrupt blob is normalized, not fatal: the
        # criteria read the caller's dict (still valid here), so promotion proceeds.
        failure = self._failure("CorruptMetadata", runs=2)
        self.db.update_failure("gmefail-CorruptMetadata", metadata=["not", "a", "dict"])

        promoted = self._promote([failure], self._manifest("CorruptMetadata"))

        self.assertEqual(promoted, ["gmefail-CorruptMetadata"])
        stored = self.db.get_failure("gmefail-CorruptMetadata")["metadata"]["knowledge"]
        self.assertEqual(stored["stability"], 2)
        self.assertEqual(stored["injected_count"], 0)

        record_injection_counts(self.db, ["gmefail-CorruptMetadata"])

        stored = self.db.get_failure("gmefail-CorruptMetadata")["metadata"]["knowledge"]
        self.assertEqual(stored["injected_count"], 1)


if __name__ == "__main__":
    unittest.main()
