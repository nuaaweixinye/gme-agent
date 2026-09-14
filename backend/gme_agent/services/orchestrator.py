from __future__ import annotations

from pathlib import Path
from typing import Any
import threading
import uuid

from ..flows.bug_fix_flow import run_fix_job, validate_fix_failures
from ..flows.bug_fix_pr_flow import run_bug_fix_pr_job
from ..flows.build_test_flow import (
    record_failures,
    run_build_job,
    run_configure_and_build,
    run_tests,
    run_tests_job,
)
from ..flows.generated_test_edit_flow import delete_generated_tests
from ..flows.memory_audit_flow import run_memory_audit_job
from ..flows.pr_flow import run_cleanup_job, run_pr_job
from ..flows.skip_pr_flow import run_selected_tests_pr_job, run_skip_pr_job
from ..flows.test_generation_flow import run_test_extension_job, run_test_generation_job
from ..git.repositories import repair_candidate_repos
from ..git.worktree import normalize_repo_path
from ..settings.config import AgentConfig
from ..storage.db import AgentDb
from .artifact_service import artifact_dir_for_job, write_job_artifacts
from .failure_service import failure_filter, update_failure_status
from .interface_catalog_service import (
    MAX_CONCURRENT_GENERATION_JOBS,
    merge_selected_interfaces,
    resolve_test_generation_batches,
    resolve_test_generation_selection,
    selection_title,
)
from .job_service import delete_job_record


class JobAlreadyActiveError(RuntimeError):
    pass


MAX_CONCURRENT_RETRY_JOBS = 3


def _selection_metadata(selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "selected_interface_ids": selection["interface_ids"],
        "selected_interfaces": selection["interfaces"],
        "last_selected_interfaces": selection["interfaces"],
        "selected_target_files": selection["target_files"],
    }


class Orchestrator:
    def __init__(self, config: AgentConfig, db: AgentDb):
        self.config = config
        self.db = db
        self._active_jobs: set[str] = set()
        self._active_jobs_lock = threading.RLock()
        self._generation_slots = threading.BoundedSemaphore(MAX_CONCURRENT_GENERATION_JOBS)
        self._generation_prepare_lock = threading.Lock()
        self._retry_generation_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RETRY_JOBS)

    def set_config(self, config: AgentConfig) -> None:
        self.config = config

    def create_test_generation_job(
        self,
        module: str,
        api_name: str = "",
        *,
        interface_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        selection = None
        if interface_ids is not None:
            selection = resolve_test_generation_selection(
                module,
                interface_ids,
            )
            api_name = selection_title(selection)
        return self._create_test_generation_job(module, api_name, selection)

    def create_test_generation_jobs_batch(
        self,
        module: str,
        *,
        interface_ids: list[str],
        batch_size: int,
    ) -> dict[str, Any]:
        selections = resolve_test_generation_batches(
            module,
            interface_ids,
            batch_size=batch_size,
        )
        batch_group_id = str(uuid.uuid4())
        batch_count = len(selections)
        jobs = [
            self._create_test_generation_job(
                module,
                selection_title(selection),
                selection,
                batch_metadata={
                    "batch_group_id": batch_group_id,
                    "batch_index": index,
                    "batch_count": batch_count,
                    "batch_size": batch_size,
                    "batch_interface_count": len(selection["interface_ids"]),
                },
            )
            for index, selection in enumerate(selections, start=1)
        ]
        return {
            "batch_group_id": batch_group_id,
            "batch_count": batch_count,
            "batch_size": batch_size,
            "interface_count": len(interface_ids),
            "jobs": jobs,
        }

    def _create_test_generation_job(
        self,
        module: str,
        api_name: str,
        selection: dict[str, Any] | None,
        *,
        batch_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        title = f"Generate {module} tests"
        if batch_metadata and int(batch_metadata.get("batch_count") or 0) > 1:
            title += (
                f" (batch {batch_metadata['batch_index']}/"
                f"{batch_metadata['batch_count']})"
            )
        target_repo = self._test_target_repo()
        metadata = {"target_repo": target_repo, "pr_strategy": self.config.pr_strategy}
        if selection:
            metadata.update(_selection_metadata(selection))
        if batch_metadata:
            metadata.update(batch_metadata)
        job = self.db.create_job(
            job_id=job_id,
            job_type="test_generation",
            title=title,
            module=module,
            api_name=api_name,
            metadata=metadata,
        )
        self._start_thread(
            self._run_test_generation_job,
            job_id,
            module,
            api_name,
            target_repo,
            selection,
        )
        return self.job_with_runtime_state(job_id)

    def extend_test_generation_job(
        self,
        job_id: str,
        api_name: str = "",
        *,
        interface_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if job.get("type") != "test_generation":
            raise ValueError("Only test-generation jobs can be extended.")
        selection = None
        if interface_ids is not None:
            selection = resolve_test_generation_selection(
                str(job.get("module") or ""),
                interface_ids,
            )
            api_name = selection_title(selection)
            metadata = dict(job.get("metadata") or {})
            metadata.update(_selection_metadata(selection))
            metadata["selected_interfaces"] = merge_selected_interfaces(
                (job.get("metadata") or {}).get("selected_interfaces") or [],
                selection["interfaces"],
            )
            metadata["last_selected_interfaces"] = selection["interfaces"]
            metadata["selected_interface_ids"] = [
                item["id"] for item in metadata["selected_interfaces"]
            ]
            metadata["selected_target_files"] = list(
                dict.fromkeys(
                    [
                        *((job.get("metadata") or {}).get("selected_target_files") or []),
                        *selection["target_files"],
                    ]
                )
            )
        self._reserve_job(job_id)
        try:
            if selection is not None:
                job = self.db.update_job(job_id, api_name=api_name, metadata=metadata)
            self._start_reserved_thread(self._run_test_extension_job, job_id, api_name, selection)
        except Exception:
            self._release_job(job_id)
            raise
        return self.job_with_runtime_state(job_id)

    def retry_test_generation_job(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        selection, session_id, retry_context = self._test_generation_retry_details(job)
        metadata = dict(job.get("metadata") or {})
        metadata.update(
            {
                "retry_count": int(metadata.get("retry_count") or 0) + 1,
                "retry_source_error": str(job.get("error") or ""),
                "retry_recovered_session": bool(session_id),
            }
        )
        self.db.update_job(
            job_id,
            status="queued",
            error="",
            harness_session_id=session_id or None,
            metadata=metadata,
        )
        self.db.add_event(
            job_id,
            "info",
            "Queued failed test-generation task for continuation in its existing worktree.",
        )
        self._start_thread(
            self._run_test_retry_job,
            job_id,
            selection,
            retry_context,
        )
        return self.job_with_runtime_state(job_id)

    def retry_test_generation_jobs(self, job_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item or "").strip() for item in job_ids))
        if not ids or any(not item for item in ids):
            raise ValueError("Select at least one failed test-generation task")
        if len(ids) > 20:
            raise ValueError("Retry at most 20 test-generation tasks at once")
        jobs = [self.db.get_job(job_id) for job_id in ids]
        for job in jobs:
            self._test_generation_retry_details(job)
        return {
            "jobs": [self.retry_test_generation_job(str(job["id"])) for job in jobs],
            "retry_count": len(jobs),
            "max_concurrent_retries": MAX_CONCURRENT_RETRY_JOBS,
        }

    def create_fix_job(self, failure_id: str) -> dict[str, Any]:
        return self.create_fix_job_for_failures([failure_id])

    def create_fix_job_for_failures(self, failure_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item).strip() for item in failure_ids if str(item).strip()))
        failures = [self.db.get_failure(failure_id) for failure_id in ids]
        fix_context = validate_fix_failures(self, failures)
        source_job = self.db.get_job(failures[0]["job_id"]) if failures[0].get("job_id") else {}
        module = str(source_job.get("module") or failures[0].get("metadata", {}).get("module") or "")
        candidate_repos = repair_candidate_repos(self.config, module)
        job_id = str(uuid.uuid4())
        api_name = str(fix_context.get("api_name") or source_job.get("api_name") or "")
        job = self.db.create_job(
            job_id=job_id,
            job_type="bug_fix",
            title=f"Fix {api_name or fix_context['primary_failure_id']}",
            module=module,
            api_name=api_name,
            metadata={
                "failure_id": fix_context["primary_failure_id"],
                "test_module": module,
                "fix_candidate_repos": candidate_repos,
                "pr_strategy": self.config.pr_strategy,
                **fix_context,
            },
        )
        for failure in failures:
            failure_metadata = dict(failure.get("metadata") or {})
            failure_metadata["fix_job_id"] = job_id
            self.db.update_failure(str(failure["id"]), status="fixing", metadata=failure_metadata)
        self._start_thread(self._run_fix_job, job_id, failures)
        return self.job_with_runtime_state(job_id)

    def run_tests_for_job(self, job_id: str, gtest_filter: str = "*") -> dict[str, Any]:
        self.db.get_job(job_id)
        self._start_thread(self._run_tests_job, job_id, gtest_filter)
        return self.job_with_runtime_state(job_id)

    def build_job(self, job_id: str) -> dict[str, Any]:
        self.db.get_job(job_id)
        self._start_thread(self._run_build_job, job_id)
        return self.job_with_runtime_state(job_id)

    def run_memory_audit_for_job(self, job_id: str, gtest_filter: str = "*") -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if job.get("type") != "test_generation":
            raise RuntimeError("Only test-generation jobs can run generated-test memory audits.")
        self._start_thread(self._run_memory_audit_job, job_id, gtest_filter)
        return self.job_with_runtime_state(job_id)

    def create_pr_for_job(self, job_id: str) -> dict[str, Any]:
        self.db.get_job(job_id)
        self._start_thread(self._run_pr_job, job_id)
        return self.job_with_runtime_state(job_id)

    def create_skip_pr_for_job(self, job_id: str) -> dict[str, Any]:
        self.db.get_job(job_id)
        self._start_thread(self._run_skip_pr_job, job_id)
        return self.job_with_runtime_state(job_id)

    def create_selected_tests_pr_for_job(self, job_id: str, tests: list[dict[str, str]]) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if job.get("type") != "test_generation":
            raise RuntimeError("Only test-generation jobs can submit selected tests.")
        if not tests:
            raise RuntimeError("Select at least one generated test before creating a PR.")
        self._start_thread(self._run_selected_tests_pr_job, job_id, tests)
        return self.job_with_runtime_state(job_id)

    def cleanup_job_worktree(self, job_id: str) -> dict[str, Any]:
        self.db.get_job(job_id)
        self._start_thread(self._run_cleanup_job, job_id)
        return self.job_with_runtime_state(job_id)

    def delete_job(self, job_id: str, *, cleanup_worktree: bool = True, delete_artifacts: bool = True) -> dict[str, Any]:
        return self._run_exclusive_job_action(
            job_id,
            delete_job_record,
            self,
            job_id,
            cleanup_worktree=cleanup_worktree,
            delete_artifacts=delete_artifacts,
        )

    def delete_generated_tests_for_job(self, job_id: str, tests: list[dict[str, str]]) -> dict[str, Any]:
        return self._run_exclusive_job_action(job_id, delete_generated_tests, self, job_id, tests)

    def update_failure_status(self, failure_id: str, status: str) -> dict[str, Any]:
        return update_failure_status(self.db, failure_id, status)

    def _reserve_job(self, job_id: str) -> None:
        with self._active_jobs_lock:
            if job_id in self._active_jobs:
                raise JobAlreadyActiveError(f"Job {job_id} is already running another action.")
            self._active_jobs.add(job_id)

    def _release_job(self, job_id: str) -> None:
        with self._active_jobs_lock:
            self._active_jobs.discard(job_id)

    def _start_thread(self, target, *args) -> None:
        job_id = str(args[0]) if args else ""
        if job_id:
            self._reserve_job(job_id)
        try:
            self._start_reserved_thread(target, *args)
        except Exception:
            if job_id:
                self._release_job(job_id)
            raise

    def _start_reserved_thread(self, target, *args) -> None:
        job_id = str(args[0]) if args else ""

        def run_target() -> None:
            try:
                target(*args)
            finally:
                if job_id:
                    self._release_job(job_id)

        thread = threading.Thread(target=run_target, daemon=True)
        thread.start()

    def _run_exclusive_job_action(self, job_id: str, target, *args, **kwargs):
        self._reserve_job(job_id)
        try:
            return target(*args, **kwargs)
        finally:
            self._release_job(job_id)

    def is_job_active(self, job_id: str) -> bool:
        with self._active_jobs_lock:
            return job_id in self._active_jobs

    def job_with_runtime_state(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        return {**job, "active": self.is_job_active(job_id)}

    def list_jobs_with_runtime_state(self) -> list[dict[str, Any]]:
        return [
            {**job, "active": self.is_job_active(str(job.get("id") or ""))}
            for job in self.db.list_jobs()
        ]

    def _test_generation_retry_details(
        self,
        job: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        job_id = str(job.get("id") or "")
        if job.get("type") != "test_generation":
            raise ValueError(f"Task {job_id} is not a test-generation task")
        if job.get("status") != "failed":
            raise ValueError(f"Task {job_id} is not failed")
        if self.is_job_active(job_id):
            raise JobAlreadyActiveError(f"Job {job_id} is already running another action.")
        worktree_path = str(job.get("worktree_path") or "")
        if not worktree_path or not Path(worktree_path).is_dir():
            raise ValueError(f"Task {job_id} no longer has a reusable worktree")

        metadata = dict(job.get("metadata") or {})
        interfaces = list(metadata.get("selected_interfaces") or [])
        if not interfaces:
            raise ValueError(f"Task {job_id} has no structured interface selection to retry")
        selection = {
            "module": str(job.get("module") or ""),
            "interface_ids": [
                str(item.get("id") or "")
                for item in interfaces
            ],
            "interfaces": interfaces,
            "target_files": list(
                metadata.get("selected_target_files")
                or dict.fromkeys(str(item.get("target_file") or "") for item in interfaces)
            ),
        }
        session_id = str(job.get("harness_session_id") or "")
        return selection, session_id, self._test_generation_retry_context(
            str(job.get("error") or "")
        )

    @staticmethod
    def _test_generation_retry_context(error: str) -> str:
        instructions = [
            "Continue the interrupted, authorized GME vs ACIS C++ unit-test task in "
            "the existing worktree. Preserve valid code and artifacts already present; "
            "do not restart the task or discard completed work.",
            "Read the current coverage artifacts and generated test manifest first. "
            "Finish every remaining planned gap, rerun required functional and memory "
            "validation, and leave all artifacts internally consistent.",
            "Every gap marked covered_passed or covered_difference_found must have exactly "
            "one matching generated_tests.json entry, including a gap covered by strengthening "
            "an existing TEST_F rather than adding a new TEST_F.",
        ]
        normalized_error = error.lower()
        if "cybersecurity risk" in normalized_error:
            instructions.insert(
                0,
                "This is benign software quality assurance for mathematical geometry-law "
                "APIs. It does not involve security research, vulnerability discovery, "
                "credential access, exploitation, malware, or network targeting.",
            )
        elif "usage limit" in normalized_error or "at capacity" in normalized_error:
            instructions.append(
                "The prior run stopped because the agent service was temporarily unavailable; "
                "continue from the files on disk and verify all partial work before finishing."
            )
        elif "generated test mapping" in normalized_error:
            instructions.append(
                "The prior code and tests completed, but local artifact validation found a "
                "covered gap without a generated_tests.json mapping. Repair the manifest and "
                "related report without creating duplicate tests, then recheck the full closure."
            )
        return "\n".join(instructions)

    def _emit(self, job_id: str, level: str, message: str) -> None:
        self.db.add_event(job_id, level, message)

    def _job_emit(self, job_id: str):
        return lambda level, message: self._emit(job_id, level, message)

    def _run_test_generation_job(
        self,
        job_id: str,
        module: str,
        api_name: str,
        target_repo: str,
        selection: dict[str, Any] | None,
    ) -> None:
        with self._generation_slots:
            run_test_generation_job(self, job_id, module, api_name, target_repo, selection)

    def _run_test_extension_job(
        self,
        job_id: str,
        api_name: str,
        selection: dict[str, Any] | None,
    ) -> None:
        run_test_extension_job(self, job_id, api_name, selection)

    def _run_test_retry_job(
        self,
        job_id: str,
        selection: dict[str, Any],
        retry_context: str,
    ) -> None:
        with self._retry_generation_slots:
            job = self.db.get_job(job_id)
            run_test_extension_job(
                self,
                job_id,
                str(job.get("api_name") or ""),
                selection,
                retry_context=retry_context,
                validate_current_manifest=True,
            )

    def _run_fix_job(self, job_id: str, failures: list[dict[str, Any]]) -> None:
        run_fix_job(self, job_id, failures)

    def _run_tests_job(self, job_id: str, gtest_filter: str) -> None:
        run_tests_job(self, job_id, gtest_filter)

    def _run_build_job(self, job_id: str) -> None:
        run_build_job(self, job_id)

    def _run_memory_audit_job(self, job_id: str, gtest_filter: str) -> None:
        run_memory_audit_job(self, job_id, gtest_filter)

    def _run_cleanup_job(self, job_id: str) -> None:
        run_cleanup_job(self, job_id)

    def _run_pr_job(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if job.get("type") == "bug_fix":
            run_bug_fix_pr_job(self, job_id)
        else:
            run_pr_job(self, job_id)

    def _run_skip_pr_job(self, job_id: str) -> None:
        run_skip_pr_job(self, job_id)

    def _run_selected_tests_pr_job(self, job_id: str, tests: list[dict[str, str]]) -> None:
        run_selected_tests_pr_job(self, job_id, tests)

    def _run_configure_and_build(self, job_id: str, worktree: Path) -> str:
        return run_configure_and_build(self, job_id, worktree)

    def _run_tests(
        self,
        job_id: str,
        worktree: Path,
        gtest_filter: str,
        *,
        artifact_name: str = "gtest_output.txt",
    ) -> str:
        return run_tests(self, job_id, worktree, gtest_filter, artifact_name=artifact_name)

    def _record_failures(self, job_id: str, test_output: str, gtest_filter: str, *, artifact_dir: Path) -> list[dict[str, Any]]:
        return record_failures(self, job_id, test_output, gtest_filter, artifact_dir=artifact_dir)

    def _artifact_dir(self, job_id: str) -> Path:
        return artifact_dir_for_job(self.config, job_id)

    def _command_mapping(self, worktree: Path, build_dir: Path, gtest_filter: str, *, artifact_dir: Path | None = None) -> dict[str, str]:
        artifact_dir = artifact_dir or Path(self.config.artifact_root)
        gtest_xml_path = self._gtest_xml_path(artifact_dir)
        module_name = self._current_job_module_for_artifact(artifact_dir)
        develop_modules = self._current_job_develop_modules_for_artifact(artifact_dir)
        develop_module_option = " ".join(self._develop_module_option(name) for name in develop_modules if name)
        test_module_option = self._test_module_option(module_name)
        test_executable = self.config.test_executable.format(
            worktree=str(worktree),
            build_dir=str(build_dir),
            gtest_filter=gtest_filter,
            test_module_name=module_name,
            develop_module_option=develop_module_option,
            test_module_option=test_module_option,
            artifact_dir=str(artifact_dir),
            gtest_xml_path=str(gtest_xml_path),
        )
        return {
            "worktree": str(worktree),
            "build_dir": str(build_dir),
            "gtest_filter": gtest_filter,
            "test_executable": test_executable,
            "test_module_name": module_name,
            "develop_module_option": develop_module_option,
            "test_module_option": test_module_option,
            "artifact_dir": str(artifact_dir),
            "gtest_xml_path": str(gtest_xml_path),
        }

    def _reproduce_command(self, gtest_filter: str) -> str:
        build_dir = "{build_dir}"
        test_executable = self.config.test_executable.format(
            worktree="{worktree}",
            build_dir=build_dir,
            gtest_filter=gtest_filter,
            test_module_name="{test_module_name}",
            develop_module_option="{develop_module_option}",
            test_module_option="{test_module_option}",
            artifact_dir="{artifact_dir}",
            gtest_xml_path="{gtest_xml_path}",
        )
        return self.config.test_command.format(
            worktree="{worktree}",
            build_dir=build_dir,
            gtest_filter=gtest_filter,
            test_executable=test_executable,
            test_module_name="{test_module_name}",
            develop_module_option="{develop_module_option}",
            test_module_option="{test_module_option}",
            artifact_dir="{artifact_dir}",
            gtest_xml_path="{gtest_xml_path}",
        )

    def _agent_test_command(self, mapping: dict[str, str], build_dir: Path) -> str:
        """Render `test_command` for the sandboxed coding agent.

        The configured `gtest_xml_path` resolves into the job artifact directory,
        which lies outside the agent's workspace-write root and is not writable
        there. The agent's own verification run therefore writes its XML inside
        the build directory; GME Test Agent still produces the authoritative
        `gtest.xml` in the artifact directory with the configured command.
        """
        agent_mapping = {**mapping, "gtest_xml_path": str(Path(build_dir) / "gtest_agent.xml")}
        agent_mapping["test_executable"] = self.config.test_executable.format(**agent_mapping)
        return self.config.test_command.format(**agent_mapping)

    def _gtest_xml_path(self, artifact_dir: Path) -> Path:
        return Path(
            self.config.gtest_xml_path.format(
                artifact_dir=str(artifact_dir),
                worktree="{worktree}",
                build_dir="{build_dir}",
                test_executable="{test_executable}",
                gtest_filter="{gtest_filter}",
                test_module_name="{test_module_name}",
                develop_module_option="{develop_module_option}",
                test_module_option="{test_module_option}",
            )
        )

    def _current_job_module_for_artifact(self, artifact_dir: Path) -> str:
        job_id = artifact_dir.name
        try:
            job = self.db.get_job(job_id)
        except Exception:
            return ""
        return normalize_repo_path(str(job.get("module") or "")).replace("/", "_")

    def _current_job_develop_modules_for_artifact(self, artifact_dir: Path) -> list[str]:
        job_id = artifact_dir.name
        try:
            job = self.db.get_job(job_id)
        except Exception:
            return []
        modules = [normalize_repo_path(str(job.get("module") or "")).replace("/", "_")]
        if job.get("type") == "bug_fix":
            metadata = job.get("metadata") or {}
            target_repo = str(metadata.get("fix_target_repo") or metadata.get("target_repo") or "")
            module_root = normalize_repo_path(self.config.module_repo_root)
            prefix = f"{module_root}/"
            normalized_target = normalize_repo_path(target_repo)
            if normalized_target.startswith(prefix):
                modules.append(normalized_target[len(prefix) :].replace("/", "_"))
        return list(dict.fromkeys(module for module in modules if module))

    @staticmethod
    def _test_module_option(module_name: str) -> str:
        normalized = "".join(ch if ch.isalnum() else "_" for ch in module_name).strip("_")
        if not normalized:
            return ""
        return f"-DTEST_{normalized.upper()}=ON"

    @staticmethod
    def _develop_module_option(module_name: str) -> str:
        normalized = "".join(ch if ch.isalnum() else "_" for ch in module_name).strip("_")
        if not normalized:
            return ""
        return f"-DDEVELOP_{normalized.upper()}=ON"

    def _write_job_artifacts(self, job_id: str, worktree: Path, artifact_dir: Path) -> None:
        write_job_artifacts(self, job_id, worktree, artifact_dir)

    def _merge_metadata(self, job_id: str, extra: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(self.db.get_job(job_id).get("metadata") or {})
        metadata.update(extra)
        return metadata

    def _target_metadata(self, superproject_branch: str, target) -> dict[str, str]:
        return {
            "superproject_branch": superproject_branch,
            "target_repo": target.rel_path,
            "target_repo_path": str(target.path),
            "target_branch": target.branch,
            "target_base_branch": target.base_branch,
        }

    def _test_target_repo(self) -> str:
        return normalize_repo_path(self.config.test_target_repo)

    def _test_skill_names(self) -> list[str]:
        names = [
            self.config.test_generation_skill,
            "gme-module-test-analyzer",
            "gme-acis-interface-analyzer",
            "gme-test-writer",
        ]
        result = []
        for name in names:
            if name and name not in result:
                result.append(name)
        return result

    def _bug_fix_skill_names(self) -> list[str]:
        return [self.config.bug_fix_skill] if self.config.bug_fix_skill else []

    def _module_target_repo(self, module: str) -> str:
        module = normalize_repo_path(module)
        if module == ".":
            return "."
        return normalize_repo_path(f"{normalize_repo_path(self.config.module_repo_root)}/{module}")

    def _job_target_repo(self, job: dict[str, Any]) -> str:
        metadata = job.get("metadata") or {}
        if metadata.get("fix_target_repo"):
            return normalize_repo_path(metadata["fix_target_repo"])
        if metadata.get("target_repo"):
            return normalize_repo_path(metadata["target_repo"])
        if job.get("type") == "test_generation":
            return self._test_target_repo()
        module = str(job.get("module") or "")
        return self._module_target_repo(module) if module else "."

    @staticmethod
    def _target_repo_path(worktree: Path, target_repo: str) -> Path:
        target_repo = normalize_repo_path(target_repo)
        return worktree if target_repo == "." else worktree / target_repo

    @staticmethod
    def _failure_filter(failure: dict[str, Any]) -> str:
        return failure_filter(failure)

    def _pr_body(self, job: dict[str, Any]) -> str:
        metadata = job.get("metadata") or {}
        return "\n".join(
            [
                f"Automated GME Test Agent job: `{job['id']}`",
                "",
                f"Type: `{job['type']}`",
                f"Status before PR: `{job['status']}`",
                f"Module: `{job.get('module') or ''}`",
                f"API: `{job.get('api_name') or ''}`",
                f"Target repo: `{metadata.get('target_repo') or self._job_target_repo(job)}`",
                "",
                "Review the generated artifacts and logs in the local agent UI.",
            ]
        )
