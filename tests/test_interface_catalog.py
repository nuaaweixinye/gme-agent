from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.interface_catalog import (
    generate_catalogs,
    list_interface_catalogs,
    load_interface_catalog,
)
from gme_agent.services.interface_catalog_service import (
    MAX_BATCH_INTERFACES,
    MAX_BATCH_JOBS,
    MAX_CONCURRENT_GENERATION_JOBS,
    list_selectable_interface_catalogs,
    resolve_test_generation_batches,
    resolve_test_generation_selection,
    selectable_interface_catalog,
)
from gme_agent.services.orchestrator import Orchestrator
from gme_agent.settings.config import AgentConfig
from gme_agent.storage.db import AgentDb


CATALOG_ROOT = ROOT / "backend" / "gme_agent" / "interface_catalog" / "catalogs"
# The module interface catalogs are generated from a GME checkout and are not
# distributed with this repository, so the specs that read one skip until they
# exist. Generate them with scripts/generate_interface_catalog.py.
GENERATED_CATALOGS_SKIP = (
    "interface catalogs are generated locally from a GME checkout "
    "(scripts/generate_interface_catalog.py) and are not distributed with this repository"
)
CATALOG_DEPENDENT_TESTS: dict[str, tuple[str, ...]] = {
    "test_runtime_catalog_exposes_supported_modules_grouped_by_cpp": ("kernel", "laws"),
    "test_runtime_catalog_accepts_kernel_selection": ("kernel",),
    "test_runtime_catalog_rejects_unsupported_or_invalid_selection": ("laws",),
    "test_runtime_catalog_splits_batch_selection_and_validates_global_limits": ("laws",),
    "test_orchestrator_persists_structured_selection_without_trusting_paths": ("base",),
    "test_orchestrator_creates_one_independent_job_per_batch": ("laws",),
    "test_orchestrator_retries_failed_generation_in_existing_worktree": ("laws",),
}


def catalogs_available(*modules: str) -> bool:
    """Whether every named generated catalog is present in this checkout."""
    return all((CATALOG_ROOT / f"{module}.json").is_file() for module in modules)


class InterfaceCatalogGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        needed = CATALOG_DEPENDENT_TESTS.get(self._testMethodName)
        if needed is not None and not catalogs_available(*needed):
            self.skipTest(GENERATED_CATALOGS_SKIP)

    def test_generator_creates_one_validated_catalog_per_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acis_root = root / "symbols" / "acis_symbol"
            self._write_catalog(
                acis_root / "BASE_acis_symbol.csv",
                [
                    {
                        "ACIS头文件名": "vector.hxx",
                        "模块名": "基础",
                        "类型": "函数",
                        "元素唯一标识": "int parallel(const SPAvector &, const SPAvector &, const double)",
                        "父元素": "",
                    }
                ],
            )
            self._write_catalog(
                acis_root / "LAW_acis_symbol.csv",
                [
                    {
                        "ACIS头文件名": "law_base.hxx",
                        "模块名": "解方程",
                        "类型": "成员函数",
                        "元素唯一标识": "int law::zero(double) const",
                        "父元素": "law",
                    }
                ],
            )
            self._write(
                root / "tests/gme/src/base/vector_test.cpp",
                '''TEST_F(Base_VectorTest, ParallelOne) {
    RecordProperty("UniqueSymbol", "int parallel(const SPAvector &, const SPAvector &, const double)");
}

TEST_F(Base_VectorTest, ParallelTwo) {
    RecordProperty("UniqueSymbol", "int parallel(const SPAvector &, const SPAvector &, const double)");
}

TEST_F(Base_VectorTest, GmeOnly) {
    RecordProperty("UniqueSymbol", "GME");
}
''',
            )
            self._write(
                root / "tests/gme/src/laws/law_base_test.cpp",
                '''TEST_F(Laws_BaseTest, Zero) {
    RecordProperty("UniqueSymbol", "int law::zero(double) const");
}

TEST_F(Laws_BaseTest, NotRegistered) {
    RecordProperty("UniqueSymbol", "int law::missing() const");
}

TEST_F(Laws_BaseTest, MissingProperty) {
    EXPECT_TRUE(true);
}

TEST_F(Laws_BaseTest, Zero) {
    RecordProperty("UniqueSymbol", "int law::zero(double) const");
}
''',
            )
            output = root / "output"

            generated = generate_catalogs(root, root / "symbols", ["base", "laws"], output)

            self.assertEqual([catalog["module"] for _, catalog in generated], ["base", "laws"])
            base = json.loads((output / "base.json").read_text(encoding="utf-8"))
            laws = json.loads((output / "laws.json").read_text(encoding="utf-8"))

            self.assertEqual(base["summary"]["interface_count"], 1)
            self.assertEqual(base["summary"]["registered_symbol_occurrences"], 2)
            self.assertEqual(base["summary"]["special_symbol_occurrences"], 1)
            self.assertEqual(base["interfaces"][0]["name"], "parallel")
            self.assertEqual(base["interfaces"][0]["existing_test_count"], 2)
            self.assertEqual(base["interfaces"][0]["source_catalog"], "BASE_acis_symbol.csv")
            self.assertEqual(base["interfaces"][0]["target_file"], "tests/gme/src/base/vector_test.cpp")
            self.assertEqual(base["interfaces"][0]["test_suite"], "Base_VectorTest")

            self.assertEqual(laws["summary"]["interface_count"], 1)
            self.assertEqual(laws["summary"]["tests_with_multiple_unique_symbols"], 0)
            self.assertEqual(laws["summary"]["unregistered_symbol_occurrences"], 1)
            self.assertEqual(laws["summary"]["tests_without_unique_symbol"], 1)
            self.assertEqual(
                laws["excluded"]["unregistered_symbols"][0]["value"],
                "int law::missing() const",
            )

    def test_generator_reports_dynamic_and_ignores_commented_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_catalog(
                root / "symbols/BASE_acis_symbol.csv",
                [
                    {
                        "ACIS头文件名": "base.hxx",
                        "模块名": "基础",
                        "类型": "函数",
                        "元素唯一标识": "int initialize_base()",
                        "父元素": "",
                    }
                ],
            )
            self._write(
                root / "tests/gme/src/base/base_test.cpp",
                '''// RecordProperty("UniqueSymbol", "int initialize_base()");
TEST_P(Base_Test, Dynamic) {
    RecordProperty("UniqueSymbol", unique_symbol_);
}
''',
            )

            generated = generate_catalogs(root, root / "symbols", ["base"], root / "out")
            catalog = generated[0][1]

            self.assertEqual(catalog["summary"]["interface_count"], 0)
            self.assertEqual(catalog["summary"]["record_property_occurrences"], 1)
            self.assertEqual(catalog["summary"]["dynamic_symbol_occurrences"], 1)

    def test_static_loader_lists_and_validates_catalogs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root / "laws.json",
                json.dumps({"schema_version": 1, "module": "laws", "interfaces": []}),
            )

            self.assertEqual(list_interface_catalogs(root), ["laws"])
            self.assertEqual(load_interface_catalog("laws", root)["interfaces"], [])
            with self.assertRaises(ValueError):
                load_interface_catalog("../laws", root)

    def test_runtime_catalog_exposes_supported_modules_grouped_by_cpp(self) -> None:
        index = list_selectable_interface_catalogs()

        self.assertEqual([item["module"] for item in index["modules"]], ["base", "laws", "kernel"])
        self.assertEqual(index["max_selected_interfaces"], 5)
        self.assertEqual(index["max_batch_jobs"], MAX_BATCH_JOBS)
        self.assertEqual(index["max_batch_interfaces"], MAX_BATCH_INTERFACES)
        self.assertEqual(index["max_concurrent_generation_jobs"], 10)
        laws = selectable_interface_catalog("laws")
        self.assertEqual(laws["max_selected_interfaces"], 5)
        self.assertEqual(laws["max_batch_jobs"], 20)
        self.assertEqual(laws["max_batch_interfaces"], 100)
        self.assertEqual(
            laws["max_concurrent_generation_jobs"],
            MAX_CONCURRENT_GENERATION_JOBS,
        )
        self.assertEqual(laws["summary"]["interface_count"], 587)
        self.assertEqual(laws["summary"]["file_count"], len(laws["files"]))
        self.assertTrue(all(file["path"].startswith("tests/gme/src/laws/") for file in laws["files"]))
        self.assertTrue(all(file["interfaces"] for file in laws["files"]))

        kernel = selectable_interface_catalog("kernel")
        self.assertEqual(kernel["summary"]["interface_count"], 1792)
        self.assertEqual(kernel["summary"]["file_count"], 169)
        self.assertEqual(kernel["summary"]["file_count"], len(kernel["files"]))
        self.assertTrue(all(file["path"].startswith("tests/gme/src/kernel/") for file in kernel["files"]))
        self.assertTrue(all(file["interfaces"] for file in kernel["files"]))

        selected = [laws["files"][0]["interfaces"][0], laws["files"][1]["interfaces"][0]]
        selection = resolve_test_generation_selection(
            "laws",
            [item["id"] for item in selected],
        )
        self.assertEqual(len(selection["target_files"]), 2)
        self.assertNotIn("tests_per_interface", selection)
        self.assertNotIn("extra_requirements", selection)

    def test_runtime_catalog_accepts_kernel_selection(self) -> None:
        kernel = selectable_interface_catalog("kernel")
        interface = kernel["files"][0]["interfaces"][0]

        selection = resolve_test_generation_selection("kernel", [interface["id"]])

        self.assertEqual(selection["module"], "kernel")
        self.assertEqual(selection["target_files"], [interface["target_file"]])
        self.assertTrue(selection["target_files"][0].startswith("tests/gme/src/kernel/"))

    def test_runtime_catalog_rejects_unsupported_or_invalid_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "supports only"):
            selectable_interface_catalog("unknown")
        with self.assertRaisesRegex(ValueError, "must be a list"):
            resolve_test_generation_selection("base", {"unexpected": "value"})
        with self.assertRaisesRegex(ValueError, "at least one"):
            resolve_test_generation_selection("base", [])
        with self.assertRaisesRegex(ValueError, "do not belong"):
            resolve_test_generation_selection("base", ["missing"])

        laws = selectable_interface_catalog("laws")
        interface_ids = [
            interface["id"]
            for file in laws["files"]
            for interface in file["interfaces"]
        ][:6]
        with self.assertRaisesRegex(ValueError, "at most 5 interfaces"):
            resolve_test_generation_selection("laws", interface_ids)

    def test_runtime_catalog_splits_batch_selection_and_validates_global_limits(self) -> None:
        laws = selectable_interface_catalog("laws")
        interface_ids = [
            interface["id"]
            for file in laws["files"]
            for interface in file["interfaces"]
        ]

        batches = resolve_test_generation_batches("laws", interface_ids[:11], batch_size=5)

        self.assertEqual([len(batch["interface_ids"]) for batch in batches], [5, 5, 1])
        self.assertEqual(
            [interface_id for batch in batches for interface_id in batch["interface_ids"]],
            interface_ids[:11],
        )
        full_request = resolve_test_generation_batches(
            "laws",
            interface_ids[:100],
            batch_size=5,
        )
        self.assertEqual(len(full_request), 20)
        self.assertTrue(all(len(batch["interface_ids"]) == 5 for batch in full_request))
        with self.assertRaisesRegex(ValueError, "at most 100 interfaces"):
            resolve_test_generation_batches("laws", interface_ids[:101], batch_size=5)
        with self.assertRaisesRegex(ValueError, "must be unique"):
            resolve_test_generation_batches(
                "laws",
                [*interface_ids[:5], interface_ids[0]],
                batch_size=5,
            )
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            resolve_test_generation_batches("laws", interface_ids[:1], batch_size=6)
        with self.assertRaisesRegex(ValueError, "at most 20 jobs"):
            resolve_test_generation_batches("laws", interface_ids[:21], batch_size=1)

    def test_orchestrator_persists_structured_selection_without_trusting_paths(self) -> None:
        catalog = selectable_interface_catalog("base")
        interface = catalog["files"][0]["interfaces"][0]
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDb(Path(tmp) / "agent.db")
            try:
                orchestrator = Orchestrator(AgentConfig(), db)
                with mock.patch.object(orchestrator, "_start_thread") as start_thread:
                    job = orchestrator.create_test_generation_job(
                        "base",
                        interface_ids=[interface["id"]],
                    )

                self.assertEqual(job["metadata"]["selected_interface_ids"], [interface["id"]])
                self.assertEqual(job["metadata"]["selected_target_files"], [interface["target_file"]])
                self.assertNotIn("requested_test_count", job["metadata"])
                self.assertNotIn("extra_requirements", job["metadata"])
                self.assertEqual(
                    job["metadata"]["selected_interfaces"][0]["unique_symbol"],
                    interface["unique_symbol"],
                )
                selection = start_thread.call_args.args[-1]
                self.assertEqual(selection["interfaces"][0]["target_file"], interface["target_file"])
            finally:
                db.close()

    def test_orchestrator_creates_one_independent_job_per_batch(self) -> None:
        catalog = selectable_interface_catalog("laws")
        interfaces = [
            interface
            for file in catalog["files"]
            for interface in file["interfaces"]
        ][:11]
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDb(Path(tmp) / "agent.db")
            try:
                orchestrator = Orchestrator(AgentConfig(), db)
                with mock.patch.object(orchestrator, "_start_thread") as start_thread:
                    result = orchestrator.create_test_generation_jobs_batch(
                        "laws",
                        interface_ids=[interface["id"] for interface in interfaces],
                        batch_size=5,
                    )

                self.assertEqual(result["batch_count"], 3)
                self.assertEqual(result["interface_count"], 11)
                self.assertEqual(len(result["jobs"]), 3)
                self.assertEqual(start_thread.call_count, 3)
                self.assertEqual(
                    [job["metadata"]["batch_index"] for job in result["jobs"]],
                    [1, 2, 3],
                )
                self.assertTrue(
                    all(
                        job["metadata"]["batch_group_id"] == result["batch_group_id"]
                        for job in result["jobs"]
                    )
                )
                self.assertEqual(
                    [len(job["metadata"]["selected_interface_ids"]) for job in result["jobs"]],
                    [5, 5, 1],
                )
            finally:
                db.close()

    def test_orchestrator_retries_failed_generation_in_existing_worktree(self) -> None:
        catalog = selectable_interface_catalog("laws")
        interface = catalog["files"][0]["interfaces"][0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            db = AgentDb(root / "agent.db")
            try:
                orchestrator = Orchestrator(AgentConfig(), db)
                db.create_job(
                    job_id="retry-job",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    api_name=interface["name"],
                    metadata={
                        "selected_interface_ids": [interface["id"]],
                        "selected_interfaces": [interface],
                        "selected_target_files": [interface["target_file"]],
                    },
                )
                db.update_job(
                    "retry-job",
                    status="failed",
                    error="Selected model is at capacity. Please try a different model.",
                    worktree_path=str(worktree),
                    harness_session_id="gme-01a-recoverable-session",
                )

                with mock.patch.object(orchestrator, "_start_thread") as start_thread:
                    job = orchestrator.retry_test_generation_job("retry-job")

                self.assertEqual(job["status"], "queued")
                self.assertEqual(job["error"], "")
                self.assertEqual(job["harness_session_id"], "gme-01a-recoverable-session")
                self.assertNotIn("codex_thread_id", job)
                self.assertEqual(job["metadata"]["retry_count"], 1)
                self.assertTrue(job["metadata"]["retry_recovered_session"])
                self.assertEqual(start_thread.call_args.args[0], orchestrator._run_test_retry_job)
                selection = start_thread.call_args.args[2]
                self.assertEqual(selection["interface_ids"], [interface["id"]])
                retry_context = start_thread.call_args.args[3]
                self.assertIn("temporarily unavailable", retry_context)
            finally:
                db.close()

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=["ACIS头文件名", "模块名", "类型", "元素唯一标识", "父元素"],
            )
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
