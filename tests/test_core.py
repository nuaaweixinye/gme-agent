from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path
from typing import Any
import sys
import subprocess
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.harness.runner import HarnessResult, HarnessRunner
from gme_agent.flows.test_generation_flow import run_test_extension_job, run_test_generation_job
from gme_agent.git.diff import commit_paths, create_pr, ensure_only_target_repo_changed, git_diff
from gme_agent.git.repositories import (
    _sync_local_cache_repo,
    _sync_task_dependency_repo,
    module_scoped_submodule_paths,
    prepare_target_repo_from_remote,
    prepare_worktree_dependencies,
    repair_candidate_repos,
    submodule_base_branch,
)
from gme_agent.git.worktree import create_remote_worktree, normalize_repo_path, remove_worktree, run_git_with_retry
from gme_agent.execution.runner import (
    merge_failures,
    parse_gtest_failures,
    parse_gtest_xml,
    resolve_command_executable,
)
from gme_agent.flows.memory_audit_flow import _audit_single_test, _parse_gtest_list, _summarize_results, run_selected_memory_audit
from gme_agent.flows.skip_pr_flow import (
    SelectedTestBlock,
    _checkout_fresh_pr_branch,
    _classify_selected_tests,
    _extract_selected_test_blocks,
    _failure_suite_filter,
    _format_generated_tests,
    _insert_selected_test_blocks,
    _prune_manifest_tests_to_failures,
    _prune_manifest_tests_to_selection,
    _prune_generated_test_text,
    _read_utf8_source,
    _require_selected_tests_reported,
    _restore_generated_tests,
    _restore_task_target_changes,
    _selected_manifest_tests,
    _selected_pr_body,
    _selected_pr_branch_name,
    _skip_pr_branch_name,
    _skip_pr_body,
    _skip_pr_title,
    _snapshot_generated_tests,
    _stash_task_target_changes,
    _validate_fresh_pr_files,
    _validate_selected_test_results,
)
from gme_agent.flows.generated_test_edit_flow import delete_generated_tests
from gme_agent.flows.build_test_flow import record_failures
from gme_agent.flows.bug_fix_flow import (
    _copy_failure_test_file,
    _detect_fix_target,
    _gtest_status,
    _run_fix_format_check,
    _require_full_tests_pass,
    _require_memory_audit_pass,
    validate_fix_failure,
    validate_fix_failures,
)
from gme_agent.flows.bug_fix_pr_flow import _clean_failure_reason, _repair_pr_body, _repair_pr_title
from gme_agent.generated_tests import (
    ensure_generated_tests_use_selected_files,
    ensure_generated_tests_use_existing_files,
    load_generated_tests_manifest,
    require_generated_tests_manifest,
)
from gme_agent.interface_coverage import (
    REQUIRED_COVERAGE_CATEGORIES,
    require_interface_coverage_artifacts,
)
from gme_agent.services.orchestrator import JobAlreadyActiveError, Orchestrator
from gme_agent.api.server import _match_job_action, create_server
from gme_agent.prompts import (
    bug_fix_prompt,
    continue_test_generation_prompt,
    skip_known_failure_prompt,
    test_generation_prompt,
)
from gme_agent.settings.config import AgentConfig, load_config, save_config
from gme_agent.settings.options import load_config_options
from gme_agent.settings.validation import validate_config
from gme_agent.storage.db import AgentDb


class CoreTests(unittest.TestCase):
    @staticmethod
    def _coverage_checklist(*gap_ids: str) -> list[dict[str, Any]]:
        return [
            {
                "category": category,
                "findings": [f"reviewed {category}"],
                "existing_test_refs": [],
                "candidate_gap_ids": list(gap_ids) if category == "success_partitions" else [],
                "blocked_scenarios": [],
            }
            for category in REQUIRED_COVERAGE_CATEGORIES
        ]

    @staticmethod
    def _closure_audit() -> dict[str, Any]:
        return {
            "performed": True,
            "reviewed_categories": list(REQUIRED_COVERAGE_CATEGORIES),
            "remaining_unplanned_scenarios": [],
            "evidence": ["reviewed contracts, implementation branches, and tests"],
        }

    @staticmethod
    def _candidate_gap(gap_id: str, scenario: str) -> dict[str, Any]:
        return {
            "gap_id": gap_id,
            "category": "partition",
            "scenario": scenario,
            "preconditions": [],
            "oracle": "compare observable behavior",
            "uncovered_evidence": "no existing test covers this scenario",
        }

    def _init_repo(self, path: Path, file_name: str, content: str = "content\n") -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        subprocess.run(["git", "config", "user.email", "agent@example.invalid"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "GME Agent"], cwd=path, check=True)
        (path / file_name).parent.mkdir(parents=True, exist_ok=True)
        (path / file_name).write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        subprocess.run(["git", "branch", "-M", "main"], cwd=path, check=True)

    def _clone_repo(self, remote: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", str(remote), str(target)], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        subprocess.run(["git", "config", "user.email", "agent@example.invalid"], cwd=target, check=True)
        subprocess.run(["git", "config", "user.name", "GME Agent"], cwd=target, check=True)

    def test_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = AgentConfig(gme_repo_path="D:/GME", model="deepseek-v4-flash", reasoning_effort="high")
            save_config(path, cfg)
            loaded = load_config(path)
            self.assertEqual(Path(loaded.gme_repo_path), Path("D:/GME"))
            self.assertEqual(loaded.model, "deepseek-v4-flash")
            self.assertEqual(loaded.reasoning_effort, "high")
            self.assertTrue(loaded.agent_enabled)
            self.assertFalse(loaded.auto_apply_skips)
            self.assertTrue(loaded.use_builtin_skills)
            self.assertEqual(loaded.test_generation_skill, "gme-test-generation")
            self.assertEqual(loaded.bug_fix_skill, "gme-bug-fix")
            self.assertEqual(loaded.test_base_branch, "main")
            self.assertEqual(loaded.module_base_branch, "develop")

    def test_resolved_config_anchors_relative_roots(self) -> None:
        cfg = AgentConfig(
            worktree_root="./worktrees",
            artifact_root="./artifacts",
            database_path="./gme_agent.db",
            dsh_home="./dot-dsh",
        )
        resolved = cfg.resolved()
        for key in ("worktree_root", "artifact_root", "database_path", "dsh_home"):
            self.assertTrue(Path(getattr(resolved, key)).is_absolute(), key)

    def test_prepare_target_repo_from_remote_uses_latest_base_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote"
            local = root / "local"
            self._init_repo(remote, "version.txt", "old\n")
            self._clone_repo(remote, local)

            (remote / "version.txt").write_text("latest\n", encoding="utf-8")
            subprocess.run(["git", "add", "version.txt"], cwd=remote, check=True)
            subprocess.run(
                ["git", "commit", "-m", "advance"],
                cwd=remote,
                check=True,
                stdout=subprocess.PIPE,
            )

            target = prepare_target_repo_from_remote(
                AgentConfig(base_branch="main"),
                local,
                ".",
                "task-branch",
                "main",
                lambda *_args: None,
            )

            self.assertEqual(target.base_branch, "main")
            self.assertEqual((local / "version.txt").read_text(encoding="utf-8"), "latest\n")

    def test_git_retry_retries_transient_failure(self) -> None:
        with mock.patch(
            "gme_agent.git.worktree.run_git",
            side_effect=[RuntimeError("connection closed"), "fetched"],
        ) as run, mock.patch("gme_agent.git.worktree.time.sleep") as sleep:
            output = run_git_with_retry(["fetch", "origin"], ROOT, attempts=3, delay_seconds=0.1)

        self.assertEqual(output, "fetched")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_local_cache_sync_uses_one_fetch_without_pull(self) -> None:
        repo = Path("D:/cache/repo")
        emit = mock.Mock()
        with mock.patch("gme_agent.git.repositories.origin_remote", return_value="origin"), mock.patch(
            "gme_agent.git.repositories.remote_ref_exists", return_value=True
        ), mock.patch("gme_agent.git.repositories.run_git_with_retry") as retry, mock.patch(
            "gme_agent.git.repositories.run_git"
        ) as run:
            _sync_local_cache_repo(repo, "main", emit)

        retry.assert_called_once_with(["fetch", "origin"], repo, emit)
        run.assert_called_once_with(["checkout", "-B", "main", "origin/main"], repo, emit)

    def test_task_dependency_skips_fetch_when_gitlink_exists(self) -> None:
        repo = Path("D:/worktree/tests/haizhou")
        emit = mock.Mock()
        with mock.patch("gme_agent.git.repositories.origin_remote", return_value="origin"), mock.patch(
            "gme_agent.git.repositories.run_git"
        ) as run, mock.patch("gme_agent.git.repositories.run_git_with_retry") as retry:
            _sync_task_dependency_repo(repo, "main", "abc123", emit)

        run.assert_called_once_with(["checkout", "--detach", "abc123"], repo, emit)
        retry.assert_not_called()

    def test_dependency_preparation_rolls_back_partial_worktrees(self) -> None:
        cfg = AgentConfig(gme_repo_path="D:/GME")
        worktree = Path("D:/worktree")
        emit = mock.Mock()
        with mock.patch(
            "gme_agent.git.repositories.module_scoped_submodule_paths",
            return_value=["tests/gme", "tests/haizhou"],
        ), mock.patch(
            "gme_agent.git.repositories._prepare_git_dependency",
            side_effect=[None, RuntimeError("network failed")],
        ), mock.patch("gme_agent.git.repositories.cleanup_worktree_dependencies") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "network failed"):
                prepare_worktree_dependencies(cfg, worktree, "laws", "tests/gme", emit)

        cleanup.assert_called_once_with(cfg, worktree, ["tests/gme", "tests/haizhou"], emit)

    def test_create_remote_worktree_uses_latest_gme_remote_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote"
            local = root / "local"
            worktrees = root / "worktrees"
            self._init_repo(remote, "version.txt", "old\n")
            self._clone_repo(remote, local)

            (remote / "version.txt").write_text("latest\n", encoding="utf-8")
            subprocess.run(["git", "add", "version.txt"], cwd=remote, check=True)
            subprocess.run(["git", "commit", "-m", "advance"], cwd=remote, check=True, stdout=subprocess.PIPE)

            cfg = AgentConfig(
                gme_repo_path=str(local),
                worktree_root=str(worktrees),
                base_branch="main",
                github_remote="origin",
            )
            created = create_remote_worktree(cfg, "job-12345678", "pr-verify-laws", lambda *_args: None)
            try:
                self.assertEqual((created.path / "version.txt").read_text(encoding="utf-8"), "latest\n")
            finally:
                remove_worktree(cfg, created.path, lambda *_args: None)
                subprocess.run(["git", "branch", "-D", created.branch], cwd=local, check=True, stdout=subprocess.PIPE)

    def test_api_exposes_harness_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            save_config(
                config_path,
                AgentConfig(
                    gme_repo_path=str(root / "gme"),
                    worktree_root=str(root / "worktrees"),
                    artifact_root=str(root / "artifacts"),
                    database_path=str(root / "agent.db"),
                ),
            )
            token = "b" * 64
            server = create_server(config_path, "127.0.0.1", 0, token)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def get(path: str) -> dict:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with opener.open(request, timeout=5) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                config = get("/api/config")
                for key in ("provider", "model", "reasoning_effort", "dsh_home", "dsh_profile", "dsh_bin", "agent_enabled"):
                    self.assertIn(key, config)
                for key in ("codex_enabled", "sandbox", "approval_policy"):
                    self.assertNotIn(key, config)

                options = get("/api/options")
                self.assertIn("reasoning_effort_options", options)
                self.assertNotIn("sandbox_options", options)
                self.assertNotIn("approval_policy_options", options)

                models = get("/api/models")
                self.assertEqual(models["reasoning_efforts"], ["low", "medium", "high", "xhigh", "max", "ultra"])
                self.assertEqual(models["models"][0]["id"], config["model"])
                self.assertEqual(models["error"], "")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                server.agent_state.db.close()  # type: ignore[attr-defined]

    def test_api_requires_runtime_token_and_restricts_browser_origins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            save_config(
                config_path,
                AgentConfig(
                    gme_repo_path=str(root / "gme"),
                    worktree_root=str(root / "worktrees"),
                    artifact_root=str(root / "artifacts"),
                    database_path=str(root / "agent.db"),
                ),
            )
            token = "a" * 64
            server = create_server(config_path, "127.0.0.1", 0, token)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}/api/health"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def status(headers: dict[str, str] | None = None, *, method: str = "GET") -> tuple[int, dict, Any]:
                request = urllib.request.Request(base_url, headers=headers or {}, method=method)
                try:
                    response = opener.open(request, timeout=5)
                    return response.status, json.loads(response.read().decode("utf-8")), response.headers
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read().decode("utf-8")), exc.headers

            try:
                self.assertEqual(status()[0], 401)
                self.assertEqual(status({"Authorization": "Bearer wrong-token"})[0], 401)
                self.assertEqual(
                    status(
                        {
                            "Authorization": f"Bearer {token}",
                            "Origin": "https://malicious.example",
                        }
                    )[0],
                    403,
                )

                code, data, headers = status(
                    {
                        "Authorization": f"Bearer {token}",
                        "Origin": "http://127.0.0.1:5173",
                    }
                )
                self.assertEqual(code, 200)
                self.assertTrue(data["authenticated"])
                self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:5173")
                self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")

                code, _, headers = status({"Origin": "null"}, method="OPTIONS")
                self.assertEqual(code, 200)
                self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers", ""))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                server.agent_state.db.close()  # type: ignore[attr-defined]

    def test_db_job_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDb(Path(tmp) / "agent.db")
            try:
                job = db.create_job(job_id="job-1", job_type="test_generation", title="Generate tests")
                db.add_event(job["id"], "info", "hello")
                self.assertEqual(db.get_job("job-1")["status"], "queued")
                self.assertEqual(db.list_events("job-1")[0]["message"], "hello")
            finally:
                db.close()

    def test_db_delete_job_removes_related_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDb(Path(tmp) / "agent.db")
            try:
                job = db.create_job(job_id="job-1", job_type="test_generation", title="Generate tests")
                db.add_event(job["id"], "info", "hello")
                db.create_failure(failure_id="failure-1", job_id=job["id"], test_suite="Suite", test_name="Case")
                db.upsert_test_case_result(
                    job_id=job["id"],
                    test_suite="Suite",
                    test_name="Case",
                    status="passed",
                    run_id="run-1",
                )

                deleted = db.delete_job(job["id"])

                self.assertEqual(deleted["jobs"], 1)
                self.assertEqual(deleted["events"], 1)
                self.assertEqual(deleted["failures"], 1)
                self.assertEqual(deleted["test_case_results"], 1)
                with self.assertRaises(KeyError):
                    db.get_job(job["id"])
                self.assertEqual(db.list_events(job["id"]), [])
                self.assertEqual(db.list_failures(), [])
            finally:
                db.close()

    def test_delete_stale_running_job_discovers_unrecorded_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree_root = root / "worktrees"
            worktree = worktree_root / "testgen-base-20260708-181048-e477f65e"
            worktree.mkdir(parents=True)
            cfg = AgentConfig(
                gme_repo_path=str(root / "gme"),
                worktree_root=str(worktree_root),
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
            )
            db = AgentDb(cfg.database_path)
            try:
                job_id = "e477f65e-01ff-4f7e-99ae-bd2b0f12f082"
                db.create_job(job_id=job_id, job_type="test_generation", title="Generate base tests", module="base")
                db.update_job(job_id, status="creating_worktree")
                orchestrator = Orchestrator(cfg, db)

                with mock.patch("gme_agent.services.job_service.remove_worktree") as remove:
                    result = orchestrator.delete_job(job_id)

                remove.assert_called_once()
                self.assertEqual(Path(remove.call_args.args[1]).resolve(), worktree.resolve())
                self.assertTrue(result["deleted_worktree"])
                self.assertEqual(result["deleted_rows"]["jobs"], 1)
                with self.assertRaises(KeyError):
                    db.get_job(job_id)
            finally:
                db.close()

    def test_delete_job_removes_readonly_git_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = AgentConfig(
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
            )
            db = AgentDb(cfg.database_path)
            try:
                job_id = "job-readonly"
                db.create_job(job_id=job_id, job_type="test_generation", title="Generate tests")
                artifact_dir = root / "artifacts" / job_id
                pack_dir = artifact_dir / "kernel-source-review" / ".git" / "objects" / "pack"
                pack_dir.mkdir(parents=True)
                for suffix in ("idx", "pack"):
                    path = pack_dir / f"pack-test.{suffix}"
                    path.write_bytes(b"artifact")
                    path.chmod(stat.S_IREAD)
                sibling = root / "artifacts" / "other-job" / "keep.txt"
                sibling.parent.mkdir()
                sibling.write_bytes(b"keep")

                result = Orchestrator(cfg, db).delete_job(job_id, cleanup_worktree=False)

                self.assertTrue(result["deleted_artifacts"])
                self.assertFalse(artifact_dir.exists())
                self.assertEqual(sibling.read_bytes(), b"keep")
                with self.assertRaises(KeyError):
                    db.get_job(job_id)
            finally:
                db.close()

    def test_delete_job_preserves_record_on_artifact_permission_error(self) -> None:
        for readonly in (False, True):
            with self.subTest(readonly=readonly), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = AgentConfig(
                    artifact_root=str(root / "artifacts"),
                    database_path=str(root / "agent.db"),
                )
                db = AgentDb(cfg.database_path)
                path = root / "artifacts" / "job-locked" / "locked.idx"
                try:
                    job_id = "job-locked"
                    db.create_job(job_id=job_id, job_type="test_generation", title="Generate tests")
                    path.parent.mkdir(parents=True)
                    path.write_bytes(b"keep")
                    if readonly:
                        path.chmod(stat.S_IREAD)
                    error = PermissionError("File remains inaccessible")

                    def fail_removal(target, *, onerror):
                        self.assertEqual(Path(target), path.parent.resolve())
                        onerror(os.unlink, str(path), (PermissionError, error, None))

                    with mock.patch("gme_agent.services.job_service.shutil.rmtree", side_effect=fail_removal):
                        with mock.patch("gme_agent.services.job_service.os.unlink", side_effect=error) as unlink:
                            with self.assertRaisesRegex(PermissionError, "remains inaccessible"):
                                Orchestrator(cfg, db).delete_job(job_id, cleanup_worktree=False)
                        self.assertEqual(unlink.call_count, 1 if readonly else 0)
                    self.assertEqual(db.get_job(job_id)["id"], job_id)
                    self.assertEqual(path.read_bytes(), b"keep")
                finally:
                    if path.exists():
                        path.chmod(stat.S_IREAD | stat.S_IWRITE)
                    db.close()

    def test_delete_active_running_job_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(database_path=str(Path(tmp) / "agent.db"))
            db = AgentDb(cfg.database_path)
            try:
                job_id = "job-active"
                db.create_job(job_id=job_id, job_type="test_generation", title="Generate tests")
                db.update_job(job_id, status="creating_worktree")
                orchestrator = Orchestrator(cfg, db)
                with orchestrator._active_jobs_lock:
                    orchestrator._active_jobs.add(job_id)

                with self.assertRaisesRegex(JobAlreadyActiveError, "already running another action"):
                    orchestrator.delete_job(job_id)
            finally:
                db.close()

    def test_delete_bug_fix_job_reopens_linked_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(database_path=str(Path(tmp) / "agent.db"))
            db = AgentDb(cfg.database_path)
            try:
                source_job = db.create_job(job_id="source-job", job_type="test_generation", title="Generate tests")
                failure = db.create_failure(
                    failure_id="failure-1",
                    job_id=source_job["id"],
                    test_suite="Suite",
                    test_name="Case",
                )
                fix_job = db.create_job(
                    job_id="fix-job",
                    job_type="bug_fix",
                    title="Fix Case",
                    metadata={"failure_id": failure["id"]},
                )
                db.update_failure(
                    failure["id"],
                    status="fix_failed",
                    metadata={"fix_job_id": fix_job["id"], "keep": "value"},
                )
                orchestrator = Orchestrator(cfg, db)

                result = orchestrator.delete_job(
                    fix_job["id"],
                    cleanup_worktree=False,
                    delete_artifacts=False,
                )

                with self.assertRaises(KeyError):
                    db.get_job(fix_job["id"])
                reopened = db.get_failure(failure["id"])
                self.assertEqual(reopened["status"], "open")
                self.assertNotIn("fix_job_id", reopened["metadata"])
                self.assertEqual(reopened["metadata"]["keep"], "value")
                self.assertEqual(result["reopened_failure_id"], failure["id"])
            finally:
                db.close()

    def test_job_actions_are_mutually_exclusive_until_background_thread_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(database_path=str(Path(tmp) / "agent.db"))
            db = AgentDb(cfg.database_path)
            started = threading.Event()
            release = threading.Event()
            try:
                job_id = "job-exclusive"
                db.create_job(job_id=job_id, job_type="test_generation", title="Generate tests")
                orchestrator = Orchestrator(cfg, db)

                def blocking_build(_job_id: str) -> None:
                    started.set()
                    release.wait(timeout=5)

                with mock.patch.object(orchestrator, "_run_build_job", side_effect=blocking_build):
                    response = orchestrator.build_job(job_id)
                    self.assertTrue(started.wait(timeout=2))
                    self.assertTrue(response["active"])
                    self.assertTrue(orchestrator.is_job_active(job_id))

                    with self.assertRaises(JobAlreadyActiveError):
                        orchestrator.run_tests_for_job(job_id)
                    with self.assertRaises(JobAlreadyActiveError):
                        orchestrator.cleanup_job_worktree(job_id)
                    with self.assertRaises(JobAlreadyActiveError):
                        orchestrator.delete_generated_tests_for_job(job_id, [{"suite": "Suite", "name": "Case"}])
                    with self.assertRaises(JobAlreadyActiveError):
                        orchestrator.delete_job(job_id)

                    release.set()
                    deadline = time.time() + 2
                    while orchestrator.is_job_active(job_id) and time.time() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(orchestrator.is_job_active(job_id))
            finally:
                release.set()
                db.close()

    def test_record_failures_keeps_stable_id_status_and_observation_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = AgentConfig(
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
                test_target_repo="tests/gme",
            )
            db = AgentDb(cfg.database_path)
            try:
                job = db.create_job(
                    job_id="job-1",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    metadata={"target_repo": "tests/gme"},
                )
                orchestrator = Orchestrator(cfg, db)
                artifact_dir = orchestrator._artifact_dir(job["id"])
                output = """
D:/repo/tests/gme/src/laws/law_base_test.cpp:42: Failure
Expected equality of these values:
[  FAILED  ] Laws_BaseTest.GeneratedCase (1 ms)
"""

                first = record_failures(orchestrator, job["id"], output, "Laws_BaseTest.GeneratedCase", artifact_dir=artifact_dir)
                db.update_failure(first[0]["id"], status="fix_ready", metadata={"fix_job_id": "fix-1"})
                second = record_failures(orchestrator, job["id"], output, "Laws_BaseTest.GeneratedCase", artifact_dir=artifact_dir)

                self.assertEqual(second[0]["id"], first[0]["id"])
                self.assertEqual(second[0]["status"], "fix_ready")
                self.assertEqual(second[0]["metadata"]["fix_job_id"], "fix-1")
                observations = db.list_failure_observations(first[0]["id"])
                self.assertEqual(len(observations), 2)
                self.assertEqual({item["outcome"] for item in observations}, {"failed"})
                self.assertEqual(len({item["run_id"] for item in observations}), 2)
            finally:
                db.close()

    def test_record_failures_resolves_only_open_tests_reported_as_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = AgentConfig(
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
                test_target_repo="tests/gme",
            )
            db = AgentDb(cfg.database_path)
            try:
                job = db.create_job(
                    job_id="job-1",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    metadata={"target_repo": "tests/gme"},
                )
                orchestrator = Orchestrator(cfg, db)
                artifact_dir = orchestrator._artifact_dir(job["id"])
                failed_output = """
D:/repo/tests/gme/src/laws/law_base_test.cpp:42: Failure
[  FAILED  ] Suite.Case (1 ms)
"""
                failure = record_failures(orchestrator, job["id"], failed_output, "Suite.Case", artifact_dir=artifact_dir)[0]

                record_failures(orchestrator, job["id"], "[       OK ] Suite.Case (1 ms)", "Suite.Case", artifact_dir=artifact_dir)
                self.assertEqual(db.get_failure(failure["id"])["status"], "resolved")
                self.assertEqual(
                    {item["outcome"] for item in db.list_failure_observations(failure["id"])},
                    {"failed", "passed"},
                )

                reopened = record_failures(orchestrator, job["id"], failed_output, "Suite.Case", artifact_dir=artifact_dir)[0]
                self.assertEqual(reopened["id"], failure["id"])
                self.assertEqual(reopened["status"], "open")
                record_failures(orchestrator, job["id"], "test executable was not found", "Suite.Case", artifact_dir=artifact_dir)
                self.assertEqual(db.get_failure(failure["id"])["status"], "open")
            finally:
                db.close()

    def test_partial_test_run_updates_only_reported_test_case_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = AgentConfig(
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
                test_target_repo="tests/gme",
            )
            db = AgentDb(cfg.database_path)
            try:
                job = db.create_job(
                    job_id="job-1",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    metadata={"target_repo": "tests/gme"},
                )
                orchestrator = Orchestrator(cfg, db)
                artifact_dir = orchestrator._artifact_dir(job["id"])

                record_failures(
                    orchestrator,
                    job["id"],
                    "[       OK ] Suite.CaseA (1 ms)\n[       OK ] Suite.CaseB (1 ms)",
                    "Suite.*",
                    artifact_dir=artifact_dir,
                )
                initial = {
                    (item["test_suite"], item["test_name"]): item["status"]
                    for item in db.list_test_case_results(job["id"])
                }
                self.assertEqual(initial, {("Suite", "CaseA"): "passed", ("Suite", "CaseB"): "passed"})

                record_failures(
                    orchestrator,
                    job["id"],
                    "D:/repo/case.cpp:42: Failure\n[  FAILED  ] Suite.CaseA (1 ms)",
                    "Suite.CaseA",
                    artifact_dir=artifact_dir,
                )
                partial = {
                    (item["test_suite"], item["test_name"]): item["status"]
                    for item in db.list_test_case_results(job["id"])
                }
                self.assertEqual(partial, {("Suite", "CaseA"): "failed", ("Suite", "CaseB"): "passed"})
            finally:
                db.close()

    def test_db_migration_merges_duplicate_failure_identity_and_rewrites_job_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                create table jobs (
                    id text primary key, type text not null, status text not null, title text not null,
                    module text, api_name text, branch text, worktree_path text, harness_session_id text,
                    created_at text not null, updated_at text not null,
                    metadata_json text not null default '{}', error text
                );
                create table events (
                    id integer primary key autoincrement, job_id text not null, ts text not null,
                    level text not null, message text not null
                );
                create table failures (
                    id text primary key, job_id text not null, status text not null,
                    test_suite text, test_name text, file text, line integer, reason text,
                    reproduce_command text, skip_id text, created_at text not null,
                    updated_at text not null, metadata_json text not null default '{}'
                );
                """
            )
            conn.execute(
                "insert into jobs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "job-1", "test_generation", "needs_review", "Generate tests", "laws", "", "", "", "",
                    "2026-07-08T10:00:00+0800", "2026-07-11T10:00:00+0800",
                    json.dumps({"skip_failure_ids": ["failure-open"]}), None,
                ),
            )
            conn.execute(
                "insert into failures values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "failure-fix", "job-1", "fix_ready", "Suite", "Case", "old.cpp", 10, "old",
                    "old command", "failure-fix", "2026-07-08T10:00:00+0800", "2026-07-08T11:00:00+0800",
                    json.dumps({"fix_job_id": "fix-1"}),
                ),
            )
            conn.execute(
                "insert into failures values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "failure-open", "job-1", "open", "Suite", "Case", "new.cpp", 20, "latest",
                    "new command", "failure-open", "2026-07-11T10:00:00+0800", "2026-07-11T10:00:00+0800",
                    "{}",
                ),
            )
            conn.commit()
            conn.close()

            db = AgentDb(path)
            try:
                failures = db.list_failures()
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["id"], "failure-fix")
                self.assertEqual(failures[0]["status"], "fix_ready")
                self.assertEqual(failures[0]["reason"], "latest")
                self.assertEqual(failures[0]["metadata"]["fix_job_id"], "fix-1")
                self.assertEqual(db.get_job("job-1")["metadata"]["skip_failure_ids"], ["failure-fix"])
            finally:
                db.close()

    def test_db_delete_open_failures_for_job_preserves_closed_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDb(Path(tmp) / "agent.db")
            try:
                job = db.create_job(job_id="job-1", job_type="test_generation", title="Generate tests")
                db.create_failure(failure_id="open-1", job_id=job["id"], test_suite="Suite", test_name="Open")
                db.create_failure(failure_id="fixed-1", job_id=job["id"], test_suite="Suite", test_name="Fixed")
                db.update_failure("fixed-1", status="fixed")

                deleted = db.delete_open_failures_for_job(job["id"])

                self.assertEqual(deleted, 1)
                self.assertEqual([failure["id"] for failure in db.list_failures()], ["fixed-1"])
            finally:
                db.close()

    def test_job_action_route_does_not_match_nested_delete_paths(self) -> None:
        self.assertEqual(_match_job_action("/api/jobs/job-1/delete", "delete"), "job-1")
        self.assertEqual(_match_job_action("/api/jobs/job-1/generated-tests/delete", "delete"), "")
        self.assertEqual(_match_job_action("/api/jobs/job-1/generated-tests/remove", "generated-tests", "remove"), "job-1")
        self.assertEqual(_match_job_action("/api/jobs/job-1/selected-tests-pr", "selected-tests-pr"), "job-1")
        self.assertEqual(_match_job_action("/api/jobs/job-1/memory-audit", "memory-audit"), "job-1")

    def test_memory_audit_parses_gtest_list(self) -> None:
        output = """SuiteOne.
  Passes
  Fails  # TypeParam = int
SuiteTwo/0.  # TypeParam = double
  Case/0  # GetParam() = 1
"""

        self.assertEqual(
            _parse_gtest_list(output),
            ["SuiteOne.Passes", "SuiteOne.Fails", "SuiteTwo/0.Case/0"],
        )

    def test_memory_audit_passes_module_options_as_separate_arguments(self) -> None:
        cases = [
            ("test_generation", "laws", {}, ["-DDEVELOP_LAWS=ON"]),
            ("test_generation", "kernel", {}, ["-DDEVELOP_KERNEL=ON"]),
            ("bug_fix", "laws", {"fix_target_repo": "module/kernel"}, ["-DDEVELOP_LAWS=ON", "-DDEVELOP_KERNEL=ON"]),
            ("bug_fix", "kernel", {"fix_target_repo": "module/kernel"}, ["-DDEVELOP_KERNEL=ON"]),
        ]
        for job_type, module, metadata, expected_options in cases:
            with self.subTest(job_type=job_type, module=module), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                worktree = root / "worktree with spaces"
                worktree.mkdir()
                cfg = AgentConfig(database_path=str(root / "agent.db"))
                db = AgentDb(cfg.database_path)
                try:
                    db.create_job(job_id="job-1", job_type=job_type, title="Audit", module=module, metadata=metadata)
                    orchestrator = Orchestrator(cfg, db)

                    def run_command(command, cwd, emit):
                        if command[:2] == ["cmake", "--build"]:
                            executable = Path(command[2]) / "Release" / "tests.exe"
                            executable.parent.mkdir(parents=True)
                            executable.touch()
                        if "--gtest_list_tests" in command:
                            return 0, "Suite.\n  Case\n"
                        return 0, ""

                    audit_result = {
                        "test": "Suite.Case", "functional_status": "passed", "memory_status": "passed",
                        "leaks": 0, "bad_delete": 0, "return_code": 0, "detail": "",
                        "command": "tests.exe --gtest_filter=Suite.Case", "output": "",
                    }
                    with mock.patch("gme_agent.flows.memory_audit_flow._run_command", side_effect=run_command) as run, mock.patch(
                        "gme_agent.flows.memory_audit_flow._audit_single_test", return_value=audit_result
                    ):
                        summary = run_selected_memory_audit(
                            orchestrator, "job-1", worktree, "Suite.Case", artifact_dir=root / "job-1"
                        )

                    configure_command = run.call_args_list[0].args[0]
                    self.assertEqual([arg for arg in configure_command if arg.startswith("-DDEVELOP_")], expected_options)
                    self.assertIn(f"-DTEST_{module.upper()}=ON", configure_command)
                    self.assertIn("-DTEST_MEMORY_AUDIT=ON", configure_command)
                    self.assertEqual(configure_command[configure_command.index("-S") + 1], str(worktree))
                    self.assertEqual(configure_command[configure_command.index("-G") + 1], "Visual Studio 17 2022")
                    self.assertEqual(summary["passed"], 1)
                finally:
                    db.close()

    def test_memory_audit_reads_log_when_gtest_assertion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            process_dir = Path(tmp) / "single-test"

            def run_test(*args, **kwargs):
                Path(kwargs["cwd"], "gme_mmgr.1.log").write_text(
                    "Leaks: 0\nBad delete pointers: 0\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    args[0],
                    1,
                    stdout="[  FAILED  ] Suite.Case (1 ms)\n",
                    stderr="",
                )

            with mock.patch("gme_agent.flows.memory_audit_flow.subprocess.run", side_effect=run_test):
                result = _audit_single_test(
                    Path("tests.exe"),
                    "Suite.Case",
                    process_dir,
                    lambda _level, _message: None,
                )

            self.assertEqual(result["functional_status"], "failed")
            self.assertEqual(result["memory_status"], "passed")
            self.assertEqual(result["leaks"], 0)
            self.assertEqual(result["bad_delete"], 0)

    def test_memory_audit_reports_leaks_independently(self) -> None:
        summary = _summarize_results(
            "Suite.*",
            [
                {
                    "functional_status": "passed",
                    "memory_status": "leaking",
                },
                {
                    "functional_status": "failed",
                    "memory_status": "passed",
                },
                {
                    "functional_status": "skipped",
                    "memory_status": "not_applicable",
                },
                {
                    "functional_status": "passed",
                    "memory_status": "error",
                },
            ],
        )

        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["leaking"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["functional_failed"], 1)

    def test_delete_generated_tests_updates_files_manifest_metadata_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            target = worktree / "tests" / "gme"
            test_rel = "src/laws/generated_test.cpp"
            self._init_repo(
                target,
                test_rel,
                """#include "gtest/gtest.h"

TEST_F(Suite, ManualCase) {
    EXPECT_TRUE(true);
}

/**
 * generated case A
 */
TEST_F(Suite, GeneratedA) {
    EXPECT_TRUE(true);
}

TEST_F(Suite, GeneratedB) {
    EXPECT_TRUE(false);
}
""",
            )
            notes = worktree / ".gme-agent"
            notes.mkdir(parents=True)
            (notes / "generated_tests.json").write_text(
                """
{
  "tests": [
    {"file": "src/laws/generated_test.cpp", "suite": "Suite", "name": "GeneratedA", "api": "api_a"},
    {"file": "src/laws/generated_test.cpp", "suite": "Suite", "name": "GeneratedB", "api": "api_b"}
  ]
}
""",
                encoding="utf-8",
            )
            cfg = AgentConfig(
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
                test_target_repo="tests/gme",
            )
            db = AgentDb(cfg.database_path)
            try:
                job = db.create_job(
                    job_id="job-1",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    metadata={"target_repo": "tests/gme"},
                )
                db.update_job(
                    job["id"],
                    worktree_path=str(worktree),
                    metadata={
                        "target_repo": "tests/gme",
                        "generated_tests": load_generated_tests_manifest(worktree, "tests/gme")["tests"],
                    },
                )
                db.create_failure(failure_id="failure-a", job_id=job["id"], test_suite="Suite", test_name="GeneratedA")
                db.create_failure(failure_id="failure-b", job_id=job["id"], test_suite="Suite", test_name="GeneratedB")
                db.upsert_test_case_result(
                    job_id=job["id"], test_suite="Suite", test_name="GeneratedA", status="passed", run_id="run-1"
                )
                db.upsert_test_case_result(
                    job_id=job["id"], test_suite="Suite", test_name="GeneratedB", status="failed", run_id="run-1"
                )
                orchestrator = Orchestrator(cfg, db)

                updated = delete_generated_tests(orchestrator, job["id"], [{"suite": "Suite", "name": "GeneratedA"}])

                content = (target / test_rel).read_text(encoding="utf-8")
                self.assertIn("TEST_F(Suite, ManualCase)", content)
                self.assertNotIn("GeneratedA", content)
                self.assertNotIn("generated case A", content)
                self.assertIn("TEST_F(Suite, GeneratedB)", content)
                manifest = load_generated_tests_manifest(worktree, "tests/gme")
                self.assertEqual([(test["suite"], test["name"]) for test in manifest["tests"]], [("Suite", "GeneratedB")])
                self.assertEqual(updated["metadata"]["generated_gtest_filter"], "Suite.GeneratedB")
                self.assertEqual([failure["id"] for failure in db.list_failures()], ["failure-b"])
                self.assertEqual(
                    [(item["test_suite"], item["test_name"], item["status"]) for item in db.list_test_case_results(job["id"])],
                    [("Suite", "GeneratedB", "failed")],
                )
                self.assertTrue((Path(cfg.artifact_root) / job["id"] / "diff.patch").exists())
            finally:
                db.close()

    def test_parse_gtest_failures(self) -> None:
        output = """
        D:/GME/tests/gme/src/laws/foo_test.cpp:42: Failure
        Expected equality of these values:
        [  FAILED  ] LawsFooTest.CompareCaseA (15 ms)
        [  FAILED  ] 1 test, listed below:
        [  FAILED  ] LawsFooTest.CompareCaseA
        """
        failures = parse_gtest_failures(output)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["test_suite"], "LawsFooTest")
        self.assertEqual(failures[0]["test_name"], "CompareCaseA")
        self.assertEqual(failures[0]["line"], 42)

    def test_parse_gtest_xml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gtest.xml"
            path.write_text(
                """<?xml version="1.0"?>
<testsuites>
  <testsuite name="Suite" tests="1" failures="1">
    <testcase classname="Suite" name="TestA" file="foo.cpp" line="12">
      <failure message="mismatch">details</failure>
    </testcase>
  </testsuite>
</testsuites>
""",
                encoding="utf-8",
            )
            failures = parse_gtest_xml(path)
            self.assertEqual(failures[0]["test_suite"], "Suite")
            self.assertEqual(failures[0]["test_name"], "TestA")
            self.assertEqual(failures[0]["reason"], "mismatch")

    def test_parse_gtest_xml_tolerates_non_utf8_localized_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gtest.xml"
            prefix = b'''<?xml version="1.0" encoding="UTF-8"?>\n<testsuites><testsuite><testcase classname="Suite" name="TestA"><failure message="'''
            suffix = b'''">details</failure></testcase></testsuite></testsuites>'''
            path.write_bytes(prefix + "单点区间".encode("gbk") + suffix)

            failures = parse_gtest_xml(path)

            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["test_suite"], "Suite")
            self.assertEqual(failures[0]["test_name"], "TestA")

    def test_resolve_command_executable_uses_visual_studio_cmake_when_missing_from_path(self) -> None:
        with (
            mock.patch("gme_agent.execution.runner.shutil.which", return_value=None),
            mock.patch(
                "gme_agent.execution.runner._find_visual_studio_cmake",
                return_value="D:\\VS\\CMake\\bin\\cmake.exe",
            ),
        ):
            command = resolve_command_executable("cmake --build build --parallel")

        self.assertEqual(command, '"D:\\VS\\CMake\\bin\\cmake.exe" --build build --parallel')

    def test_merge_failures_deduplicates(self) -> None:
        first = [{"test_suite": "A", "test_name": "B", "file": "x.cpp", "line": 1}]
        second = [{"test_suite": "A", "test_name": "B", "file": "x.cpp", "line": 1}]
        self.assertEqual(len(merge_failures(first, second)), 1)

    def test_merge_failures_counts_failed_tests_not_assert_locations(self) -> None:
        xml = [{"test_suite": "A", "test_name": "B", "file": "x.cpp", "line": 10, "reason": "details"}]
        stdout = [{"test_suite": "A", "test_name": "B", "file": "", "line": 0, "reason": "GTest reported failure."}]

        failures = merge_failures(xml, stdout)

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["file"], "x.cpp")
        self.assertEqual(failures[0]["line"], 10)

    def test_skip_prompt_uses_direct_gtest_skip(self) -> None:
        prompt = skip_known_failure_prompt(
            "test output",
            [{"id": "gmefail-1", "test_suite": "Suite", "test_name": "Case", "file": "x.cpp", "line": 12, "reason": "mismatch"}],
            "tests/gme",
            ["tests/gme/src/base/base_geometry_test.cpp"],
        )

        self.assertIn("GTEST_SKIP()", prompt)
        self.assertIn('GTEST_SKIP() << "<简短差异原因>";', prompt)
        self.assertIn("<简短差异原因>", prompt)
        self.assertIn("失败 ID 只用于将失败记录对应到正确的测试", prompt)
        self.assertIn("禁止写入 `[gme-agent-known-failure:...]`", prompt)
        self.assertIn("`repro`、复现命令", prompt)
        self.assertIn("只描述该测试观察到的 GME 与 ACIS", prompt)
        self.assertNotIn("保留失败原因和复现命令", prompt)
        self.assertIn("只能修改 `tests/gme`", prompt)
        self.assertIn("需要标记的失败测试", prompt)
        self.assertNotIn("GME_AGENT_KNOWN_FAILURE", prompt)
        self.assertNotIn("gme_agent_known_failure.hxx", prompt)
        self.assertNotIn("Failures to mark", prompt)

    def test_bug_fix_prompt_forbids_test_and_include_changes(self) -> None:
        prompt = bug_fix_prompt(
            {"id": "gmefail-1", "test_suite": "Suite", "test_name": "Case", "reason": "mismatch"},
            "module/laws",
            test_repo="tests/gme",
            test_file="tests/gme/src/laws/foo_test.cpp",
            gtest_filter="Suite.Case",
            before_output="[  FAILED  ] Suite.Case",
        )

        self.assertIn("生产代码只能修改 `module/laws`", prompt)
        self.assertIn("不要修改 `include/`", prompt)
        self.assertIn("不要修改 `tests/gme`", prompt)
        self.assertIn("不要添加 `GTEST_SKIP`", prompt)
        self.assertIn("沿调用链找到实际计算或状态变化的位置", prompt)
        self.assertIn("明确第一个产生行为差异的位置", prompt)
        self.assertIn("准确目标测试、当前模块全量测试和目标测试 Release 内存审计", prompt)
        self.assertNotIn("只运行给定的准确 GTest filter", prompt)
        self.assertNotIn("You are working in the GME repository", prompt)

    def test_bug_fix_prompt_requires_dynamic_single_repo_selection(self) -> None:
        prompt = bug_fix_prompt(
            {"id": "gmefail-1", "test_suite": "Suite", "test_name": "Case", "reason": "mismatch"},
            candidate_repos=["module/laws", "module/kernel"],
            test_repo="tests/gme",
        )

        self.assertIn("`module/laws`、`module/kernel`", prompt)
        self.assertIn("最终只能修改其中一个仓库", prompt)
        self.assertIn("测试模块不等于缺陷源码归属仓库", prompt)
        self.assertIn("公开 `api_*` 包装", prompt)

    def test_repair_pr_uses_bugfix_title_and_documents_failures(self) -> None:
        job = {"module": "laws", "api_name": "api_make_cubic"}
        selected = [{"test_suite": "Suite", "test_name": "CubicMismatch"}]
        body = _repair_pr_body(
            job,
            selected,
            [{"name": "Suite.CubicMismatch", "reason": "GME evaluates to 9, expected 5."}],
            format_summary={"files": ["src/law_util.cpp"]},
            full_test_count=1153,
            memory_summary={"passed": 1, "total": 1},
        )

        self.assertEqual(_repair_pr_title(job), "bugfix(laws): 修复 api_make_cubic")
        self.assertTrue(body.startswith("该 PR 由 GME 修复 Agent 自动生成。"))
        self.assertIn("## 问题说明", body)
        self.assertIn("## 修复前失败测试", body)
        self.assertIn("Suite.CubicMismatch", body)
        self.assertIn("GME evaluates to 9, expected 5.", body)
        self.assertIn("模块全量测试通过 1153 项", body)
        self.assertIn("内存审计通过 1/1 项", body)
        self.assertNotIn("最新 `develop` 上重新应用", body)

        kernel_job = {
            "module": "laws",
            "api_name": "api_str_to_law",
            "metadata": {"fix_target_repo": "module/kernel"},
        }
        self.assertEqual(_repair_pr_title(kernel_job), "bugfix(kernel): 修复 api_str_to_law")

    def test_repair_pr_failure_reason_removes_local_path(self) -> None:
        reason = "D:\\projects\\worktree\\test.cpp:42\nExpected equality of these values:\n  actual\n  expected"

        cleaned = _clean_failure_reason(reason)

        self.assertNotIn("D:\\projects", cleaned)
        self.assertTrue(cleaned.startswith("Expected equality"))

    def test_fix_full_test_gate_requires_successful_summary(self) -> None:
        output = "\n".join(
            [
                "[==========] 4 tests from 1 test suite ran.",
                "[  PASSED  ] 4 tests.",
            ]
        )
        self.assertEqual(_require_full_tests_pass(output), 4)

        with self.assertRaisesRegex(RuntimeError, "reported failures"):
            _require_full_tests_pass("[  PASSED  ] 3 tests.\n[  FAILED  ] 1 test, listed below:")
        with self.assertRaisesRegex(RuntimeError, "successful GTest summary"):
            _require_full_tests_pass("process terminated before the summary")

    def test_fix_memory_gate_requires_every_selected_test_to_pass(self) -> None:
        _require_memory_audit_pass(
            {
                "total": 1,
                "passed": 1,
                "leaking": 0,
                "errors": 0,
                "not_applicable": 0,
                "functional_failed": 0,
            }
        )

        with self.assertRaisesRegex(RuntimeError, "did not pass cleanly"):
            _require_memory_audit_pass(
                {
                    "total": 1,
                    "passed": 0,
                    "leaking": 1,
                    "errors": 0,
                    "not_applicable": 0,
                    "functional_failed": 0,
                }
            )

    def test_fix_format_gate_checks_only_changed_cpp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            module = worktree / "module" / "laws"
            artifact_dir = worktree / "artifacts"
            module.mkdir(parents=True)
            artifact_dir.mkdir()
            (worktree / ".clang-format").write_text("BasedOnStyle: Google\n", encoding="utf-8")
            (module / "fix.cpp").write_text("int fixed();\n", encoding="utf-8")
            (module / "notes.txt").write_text("notes\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            version = subprocess.CompletedProcess([], 0, stdout="clang-format version 17.0.2\n", stderr="")

            with mock.patch("gme_agent.flows.bug_fix_flow.shutil.which", return_value="clang-format"), mock.patch(
                "gme_agent.flows.bug_fix_flow.subprocess.run",
                side_effect=[completed, version],
            ) as run:
                summary = _run_fix_format_check(
                    worktree,
                    module,
                    ["fix.cpp", "notes.txt"],
                    artifact_dir,
                    lambda _level, _message: None,
                )

            self.assertEqual(summary["files"], ["fix.cpp"])
            self.assertIn("--dry-run", run.call_args_list[0].args[0])
            self.assertIn("--Werror", run.call_args_list[0].args[0])
            self.assertIn("fix.cpp", run.call_args_list[0].args[0])
            self.assertNotIn("notes.txt", run.call_args_list[0].args[0])
            self.assertIn("17.0.2", (artifact_dir / "format_check_output.txt").read_text(encoding="utf-8"))

    def test_validate_fix_failure_accepts_open_unskipped_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            target = worktree / "tests" / "gme"
            rel_path = "src/laws/foo_test.cpp"
            (target / "src" / "laws").mkdir(parents=True)
            (target / rel_path).write_text(
                """#include "gtest/gtest.h"

TEST_F(Suite, Case) {
    EXPECT_TRUE(false);
}
""",
                encoding="utf-8",
            )
            notes = worktree / ".gme-agent"
            notes.mkdir(parents=True)
            (notes / "generated_tests.json").write_text(
                """
{
  "tests": [
    {"file": "src/laws/foo_test.cpp", "suite": "Suite", "name": "Case"}
  ]
}
""",
                encoding="utf-8",
            )
            cfg = AgentConfig(
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
                test_target_repo="tests/gme",
            )
            db = AgentDb(cfg.database_path)
            try:
                job = db.create_job(
                    job_id="job-1",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    metadata={"target_repo": "tests/gme"},
                )
                db.update_job(job["id"], worktree_path=str(worktree))
                failure = db.create_failure(failure_id="failure-1", job_id=job["id"], test_suite="Suite", test_name="Case")
                orchestrator = Orchestrator(cfg, db)

                context = validate_fix_failure(orchestrator, failure)

                self.assertEqual(context["generated_test_file"], rel_path)
                self.assertEqual(context["gtest_filter"], "Suite.Case")

                (target / rel_path).write_text(
                    """#include "gtest/gtest.h"

TEST_F(Suite, Case) {
    GTEST_SKIP() << "already skipped";
    EXPECT_TRUE(false);
}
""",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "GTEST_SKIP"):
                    validate_fix_failure(orchestrator, failure)
            finally:
                db.close()

    def test_validate_fix_failure_accepts_submitted_resolved_unskipped_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            target = worktree / "tests" / "gme"
            rel_path = "src/laws/foo_test.cpp"
            (target / "src" / "laws").mkdir(parents=True)
            (target / rel_path).write_text(
                """TEST_F(Suite, Case) {
    EXPECT_TRUE(false);
}
""",
                encoding="utf-8",
            )
            notes = worktree / ".gme-agent"
            notes.mkdir(parents=True)
            (notes / "generated_tests.json").write_text(
                json.dumps({"tests": [{"file": rel_path, "suite": "Suite", "name": "Case"}]}),
                encoding="utf-8",
            )
            cfg = AgentConfig(
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
                test_target_repo="tests/gme",
            )
            db = AgentDb(cfg.database_path)
            try:
                job = db.create_job(
                    job_id="job-1",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    metadata={
                        "target_repo": "tests/gme",
                        "skip_failure_ids": ["failure-1"],
                        "submitted_test_names": ["Suite.Case"],
                    },
                )
                db.update_job(job["id"], worktree_path=str(worktree))
                failure = db.create_failure(
                    failure_id="failure-1",
                    job_id=job["id"],
                    test_suite="Suite",
                    test_name="Case",
                )
                failure = db.update_failure(failure["id"], status="resolved")
                orchestrator = Orchestrator(cfg, db)

                context = validate_fix_failure(orchestrator, failure)

                self.assertTrue(context["submitted_known_failure"])
            finally:
                db.close()

    def test_validate_fix_failures_groups_tests_for_the_same_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            target = worktree / "tests" / "gme"
            rel_path = "src/laws/foo_test.cpp"
            (target / "src" / "laws").mkdir(parents=True)
            (target / rel_path).write_text(
                """TEST_F(Suite, First) {
    EXPECT_TRUE(false);
}

TEST_F(Suite, Second) {
    EXPECT_TRUE(false);
}
""",
                encoding="utf-8",
            )
            notes = worktree / ".gme-agent"
            notes.mkdir(parents=True)
            (notes / "generated_tests.json").write_text(
                json.dumps(
                    {
                        "tests": [
                            {"file": rel_path, "suite": "Suite", "name": "First", "api": "api_make_cubic"},
                            {"file": rel_path, "suite": "Suite", "name": "Second", "api": "api_make_cubic"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            cfg = AgentConfig(
                artifact_root=str(root / "artifacts"),
                database_path=str(root / "agent.db"),
                test_target_repo="tests/gme",
            )
            db = AgentDb(cfg.database_path)
            try:
                job = db.create_job(
                    job_id="job-group",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    metadata={"target_repo": "tests/gme"},
                )
                db.update_job(job["id"], worktree_path=str(worktree))
                failures = [
                    db.create_failure(
                        failure_id="failure-first",
                        job_id=job["id"],
                        test_suite="Suite",
                        test_name="First",
                    ),
                    db.create_failure(
                        failure_id="failure-second",
                        job_id=job["id"],
                        test_suite="Suite",
                        test_name="Second",
                    ),
                ]

                context = validate_fix_failures(Orchestrator(cfg, db), failures)

                self.assertEqual(context["api_name"], "api_make_cubic")
                self.assertEqual(context["failure_ids"], ["failure-first", "failure-second"])
                self.assertEqual(context["source_job_ids"], ["job-group"])
                self.assertEqual(context["gtest_filter"], "Suite.First:Suite.Second")
                self.assertEqual(len(context["selected_tests"]), 2)
            finally:
                db.close()

    def test_copy_failure_test_installs_only_selected_block_into_current_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_worktree = root / "source"
            repair_worktree = root / "repair"
            rel_file = "src/laws/foo_test.cpp"
            source_file = source_worktree / "tests" / "gme" / rel_file
            target_file = repair_worktree / "tests" / "gme" / rel_file
            source_file.parent.mkdir(parents=True)
            target_file.parent.mkdir(parents=True)
            source_file.write_text(
                """#include "old/missing_header.hxx"

TEST_F(Suite, Before) {
    EXPECT_TRUE(true);
}

TEST_F(Suite, SelectedFailure) {
    EXPECT_TRUE(false);
}

TEST_F(Suite, OldOnly) {
    EXPECT_TRUE(true);
}
""",
                encoding="utf-8",
            )
            target_file.write_text(
                """#include "current/header.hxx"

TEST_F(Suite, Before) {
    EXPECT_TRUE(true);
}

TEST_F(Suite, CurrentOnly) {
    EXPECT_TRUE(true);
}
""",
                encoding="utf-8",
            )
            events: list[str] = []

            copied = _copy_failure_test_file(
                repair_worktree,
                {
                    "test_target_repo": "tests/gme",
                    "generated_test_file": rel_file,
                    "source_worktree_path": str(source_worktree),
                    "test_suite": "Suite",
                    "test_name": "SelectedFailure",
                },
                lambda _level, message: events.append(message),
            )

            updated = target_file.read_text(encoding="utf-8")
            self.assertEqual(copied, f"tests/gme/{rel_file}")
            self.assertIn('#include "current/header.hxx"', updated)
            self.assertNotIn("old/missing_header.hxx", updated)
            self.assertIn("TEST_F(Suite, SelectedFailure)", updated)
            self.assertIn("TEST_F(Suite, CurrentOnly)", updated)
            self.assertNotIn("TEST_F(Suite, OldOnly)", updated)
            self.assertTrue(any("Installed generated failure test" in message for message in events))

    def test_copy_failure_test_replaces_existing_skipped_block_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_worktree = root / "source"
            repair_worktree = root / "repair"
            rel_file = "src/laws/foo_test.cpp"
            source_file = source_worktree / "tests" / "gme" / rel_file
            target_file = repair_worktree / "tests" / "gme" / rel_file
            source_file.parent.mkdir(parents=True)
            target_file.parent.mkdir(parents=True)
            source_file.write_text(
                """TEST_F(Suite, SelectedFailure) {
    EXPECT_TRUE(false);
}
""",
                encoding="utf-8",
            )
            target_file.write_text(
                """TEST_F(Suite, SelectedFailure) {
    GTEST_SKIP() << "known difference";
}

TEST_F(Suite, CurrentOnly) {
    EXPECT_TRUE(true);
}
""",
                encoding="utf-8",
            )

            _copy_failure_test_file(
                repair_worktree,
                {
                    "test_target_repo": "tests/gme",
                    "generated_test_file": rel_file,
                    "source_worktree_path": str(source_worktree),
                    "test_suite": "Suite",
                    "test_name": "SelectedFailure",
                },
                lambda _level, _message: None,
            )

            updated = target_file.read_text(encoding="utf-8")
            self.assertNotIn("GTEST_SKIP", updated)
            self.assertIn("EXPECT_TRUE(false);", updated)
            self.assertIn("TEST_F(Suite, CurrentOnly)", updated)

    def test_gtest_status_reads_exact_selected_test(self) -> None:
        output = """
[       OK ] Suite.Pass (1 ms)
[  SKIPPED ] Suite.Skip (0 ms)
[  FAILED  ] Suite.Fail (1 ms)
"""
        self.assertEqual(_gtest_status(output, "Suite.Pass"), "OK")
        self.assertEqual(_gtest_status(output, "Suite.Skip"), "SKIPPED")
        self.assertEqual(_gtest_status(output, "Suite.Fail"), "FAILED")
        self.assertEqual(_gtest_status(output, "Suite.Missing"), "")

    def test_test_generation_prompt_uses_existing_files_manifest_and_no_helpers(self) -> None:
        prompt = test_generation_prompt("laws", "api_ndifferentiate_law", "tests/gme")

        self.assertIn(".gme-agent/generated_tests.json", prompt)
        self.assertIn(".gme-agent/existing_test_coverage.json", prompt)
        self.assertIn(".gme-agent/interface_contracts.json", prompt)
        self.assertIn(".gme-agent/interface_coverage_plan.json", prompt)
        self.assertIn("不要按固定总数生成测试", prompt)
        self.assertIn("消费 4–8 个 `planned` 缺口", prompt)
        self.assertIn("documented_contract", prompt)
        self.assertIn("boundary_tolerance", prompt)
        self.assertIn("closure_audit", prompt)
        self.assertIn("uncovered_evidence", prompt)
        self.assertIn("covered_difference_found", prompt)
        self.assertIn("GME/ACIS 断言差异首先是候选发现", prompt)
        self.assertIn("比较 helper 支持当前运行时类型", prompt)
        self.assertIn("指针身份", prompt)
        self.assertIn("interface_id", prompt)
        self.assertIn("gap_id", prompt)
        self.assertIn("scenario", prompt)
        self.assertIn("第一条语句必须是准确的 `RecordProperty", prompt)
        self.assertIn("timer_res_.csv", prompt)
        self.assertIn("Visual Studio 17 2022", prompt)
        self.assertIn("-DDEVELOP_LAWS=ON", prompt)
        self.assertIn("-DTEST_LAWS=ON", prompt)
        self.assertIn("cmake --build", prompt)
        self.assertIn("unresolved external/LNK2019", prompt)
        self.assertIn("仅构建通过不算完成", prompt)
        self.assertNotIn("每个选中接口准确生成", prompt)
        self.assertNotIn("You are working in the GME repository", prompt)

    def test_continue_generation_prompt_uses_existing_files_manifest_and_no_helpers(self) -> None:
        prompt = continue_test_generation_prompt("base", "extend coverage", "tests/gme")

        self.assertIn(".gme-agent/generated_tests.json", prompt)
        self.assertIn("重新分析本次选中接口", prompt)
        self.assertIn("旧测试也必须作为已有覆盖参与去重", prompt)
        self.assertIn("保留既有 generated_tests 清单", prompt)
        self.assertIn("budget_exhausted", prompt)
        self.assertIn("只要仍有可执行的 `planned` 缺口就不得停止", prompt)
        self.assertIn("Visual Studio 17 2022", prompt)
        self.assertIn("-DDEVELOP_BASE=ON", prompt)
        self.assertIn("-DTEST_BASE=ON", prompt)
        self.assertNotIn("You are continuing an existing", prompt)

    def test_kernel_generation_prompt_uses_kernel_build_options(self) -> None:
        prompt = test_generation_prompt("kernel", "ENTITY::copy", "tests/gme")

        self.assertIn("-DDEVELOP_KERNEL=ON", prompt)
        self.assertIn("-DTEST_KERNEL=ON", prompt)

    def test_generation_prompt_uses_task_specific_build_guidance(self) -> None:
        prompt = test_generation_prompt(
            "laws",
            "api_ndifferentiate_law",
            "tests/gme",
            "Build validation commands from the GME Test Agent settings:\n- Build:\n  `custom-build-command`",
        )

        self.assertIn("custom-build-command", prompt)
        self.assertNotIn("-DDEVELOP_LAWS=ON", prompt)

    def test_generation_prompt_uses_structured_interface_selection(self) -> None:
        selected = [
            {
                "id": "laws.api-make-cubic.abc",
                "unique_symbol": "outcome api_make_cubic(double, double, double, double, double, double, law *&)",
                "target_file": "tests/gme/src/laws/kernel_kernapi_test.cpp",
                "test_suite": "Laws_KernapiTest",
            },
            {
                "id": "laws.law-zero.def",
                "unique_symbol": "int law::zero(double) const",
                "target_file": "tests/gme/src/laws/law_base_test.cpp",
                "test_suite": "Laws_BaseTest",
            },
        ]

        prompt = test_generation_prompt(
            "laws",
            "selected interfaces",
            "tests/gme",
            selected_interfaces=selected,
        )

        self.assertIn("测试数量由可证明的未覆盖场景决定，不设每接口总配额", prompt)
        self.assertIn("tests/gme/src/laws/kernel_kernapi_test.cpp", prompt)
        self.assertIn("outcome api_make_cubic", prompt)
        self.assertIn("fixture `Laws_BaseTest`", prompt)
        self.assertIn("只修改结构化选择中列出的现有 `.cpp` 文件", prompt)
        self.assertNotIn("共生成 4 个新测试", prompt)

    def test_generated_tests_manifest_normalizes_files_and_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            notes = worktree / ".gme-agent"
            notes.mkdir()
            (notes / "generated_tests.json").write_text(
                """
{
  "tests": [
    {
      "file": "tests/gme/src/laws/kernel_kernapi_test.cpp",
      "suite": "Laws_KernapiTest",
      "name": "ApiMakeCubicAsymmetricEndpointSlopes",
      "api": "api_make_cubic",
      "interface_id": "laws.api-make-cubic.abc",
      "gap_id": "asymmetric-endpoint-slopes",
      "scenario": "asymmetric endpoint slopes"
    },
    {
      "file": "src/laws/law_main_law_test.cpp",
      "suite": "Laws_ClassTest",
      "name": "LawZeroConstantPredicate"
    }
  ]
}
""",
                encoding="utf-8",
            )

            manifest = load_generated_tests_manifest(worktree, "tests/gme")

            self.assertEqual(
                manifest["files"],
                ["src/laws/kernel_kernapi_test.cpp", "src/laws/law_main_law_test.cpp"],
            )
            self.assertEqual(
                manifest["gtest_filter"],
                "Laws_KernapiTest.ApiMakeCubicAsymmetricEndpointSlopes:Laws_ClassTest.LawZeroConstantPredicate",
            )
            self.assertEqual(manifest["tests"][0]["interface_id"], "laws.api-make-cubic.abc")
            self.assertEqual(manifest["tests"][0]["gap_id"], "asymmetric-endpoint-slopes")
            self.assertEqual(manifest["tests"][0]["scenario"], "asymmetric endpoint slopes")

    def test_generated_tests_manifest_accepts_bom_and_normalizes_to_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            notes = worktree / ".gme-agent"
            notes.mkdir()
            path = notes / "generated_tests.json"
            content = """{
  "tests": [
    {
      "file": "src/laws/law_base_test.cpp",
      "suite": "Laws_BaseTest",
      "name": "GeneratedChineseCommentCase",
      "api": "中文接口"
    }
  ]
}
""".encode("utf-8")
            path.write_bytes(b"\xef\xbb\xbf" + content)

            manifest = load_generated_tests_manifest(worktree, "tests/gme")

            self.assertEqual(manifest["tests"][0]["api"], "中文接口")
            self.assertEqual(path.read_bytes(), content)

    def test_generated_tests_manifest_does_not_rewrite_invalid_bom_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            notes = worktree / ".gme-agent"
            notes.mkdir()
            path = notes / "generated_tests.json"
            content = b"\xef\xbb\xbf{invalid json}"
            path.write_bytes(content)

            with self.assertRaises(json.JSONDecodeError):
                load_generated_tests_manifest(worktree, "tests/gme")

            self.assertEqual(path.read_bytes(), content)

    def test_generated_tests_manifest_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "generated_tests.json"):
                require_generated_tests_manifest(Path(tmp), "tests/gme")

    def test_generated_tests_manifest_can_be_empty_for_saturated_interface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / ".gme-agent"
            notes.mkdir()
            (notes / "generated_tests.json").write_text('{"tests": []}', encoding="utf-8")

            manifest = require_generated_tests_manifest(Path(tmp), "tests/gme", allow_empty=True)

            self.assertEqual(manifest["tests"], [])
            with self.assertRaisesRegex(RuntimeError, "No generated test manifest entries"):
                require_generated_tests_manifest(Path(tmp), "tests/gme")

    def test_generated_tests_manifest_requires_existing_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            repo = worktree / "tests" / "gme"
            self._init_repo(repo, "src/laws/existing_test.cpp", "TEST(Existing, Case) {}\n")
            new_file = repo / "src" / "laws" / "new_agent_test.cpp"
            new_file.write_text("TEST(New, Case) {}\n", encoding="utf-8")

            ensure_generated_tests_use_existing_files(worktree, "tests/gme", ["src/laws/existing_test.cpp"])
            with self.assertRaisesRegex(RuntimeError, "existing test files"):
                ensure_generated_tests_use_existing_files(worktree, "tests/gme", ["src/laws/new_agent_test.cpp"])

    def test_generated_tests_manifest_stays_in_selected_files(self) -> None:
        manifest = {
            "tests": [
                {
                    "file": "src/laws/kernel_kernapi_test.cpp",
                    "suite": "Laws_KernapiTest",
                    "name": "SelectedCase",
                }
            ]
        }

        ensure_generated_tests_use_selected_files(
            manifest,
            "tests/gme",
            ["tests/gme/src/laws/kernel_kernapi_test.cpp"],
        )
        with self.assertRaisesRegex(RuntimeError, "selected target files"):
            ensure_generated_tests_use_selected_files(
                manifest,
                "tests/gme",
                ["tests/gme/src/laws/law_base_test.cpp"],
            )

    def test_interface_coverage_artifacts_validate_manifest_gap_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            notes = worktree / ".gme-agent"
            notes.mkdir()
            selected = [
                {
                    "id": "laws.api-make-cubic.abc",
                    "unique_symbol": "outcome api_make_cubic(double)",
                    "target_file": "tests/gme/src/laws/kernel_kernapi_test.cpp",
                }
            ]
            identity = {
                "interface_id": selected[0]["id"],
                "unique_symbol": selected[0]["unique_symbol"],
                "target_file": selected[0]["target_file"],
            }
            (notes / "existing_test_coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "module": "laws",
                        "interfaces": [{**identity, "existing_tests": [], "covered_scenarios": []}],
                    }
                ),
                encoding="utf-8",
            )
            (notes / "interface_contracts.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "module": "laws",
                        "interfaces": [
                            {
                                **identity,
                                "coverage_checklist": self._coverage_checklist("zero-boundary"),
                                "candidate_gaps": [self._candidate_gap("zero-boundary", "zero boundary input")],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (notes / "interface_coverage_plan.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "module": "laws",
                        "interfaces": [
                            {
                                **identity,
                                "status": "complete",
                                "closure_audit": self._closure_audit(),
                                "gaps": [
                                    {
                                        "gap_id": "zero-boundary",
                                        "scenario": "zero boundary input",
                                        "status": "covered_passed",
                                        "test": {"suite": "Laws_KernapiTest", "name": "ZeroBoundary"},
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = {
                "tests": [
                    {
                        "file": "src/laws/kernel_kernapi_test.cpp",
                        "suite": "Laws_KernapiTest",
                        "name": "ZeroBoundary",
                        "interface_id": selected[0]["id"],
                        "gap_id": "zero-boundary",
                        "scenario": "zero boundary input",
                    }
                ]
            }

            summary = require_interface_coverage_artifacts(
                worktree,
                "tests/gme",
                selected,
                manifest,
            )

            self.assertEqual(summary["interfaces"][0]["status"], "complete")
            self.assertEqual(summary["gap_status_counts"], {"covered_passed": 1})

            manifest["tests"][0]["gap_id"] = "unknown-gap"
            with self.assertRaisesRegex(RuntimeError, "unknown coverage gap"):
                require_interface_coverage_artifacts(worktree, "tests/gme", selected, manifest)

            manifest["tests"][0]["gap_id"] = "zero-boundary"
            contracts = json.loads((notes / "interface_contracts.json").read_text(encoding="utf-8"))
            contracts["interfaces"][0]["candidate_gaps"][0]["scenario"] = "different scenario"
            (notes / "interface_contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "scenario differs from interface contracts"):
                require_interface_coverage_artifacts(worktree, "tests/gme", selected, manifest)

            contracts["interfaces"][0]["candidate_gaps"][0]["scenario"] = "zero boundary input"
            removed_check = contracts["interfaces"][0]["coverage_checklist"].pop()
            (notes / "interface_contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "did not review coverage categories"):
                require_interface_coverage_artifacts(worktree, "tests/gme", selected, manifest)

            contracts["interfaces"][0]["coverage_checklist"].append(removed_check)
            contracts["interfaces"][0]["candidate_gaps"].append(
                self._candidate_gap("omitted-gap", "scenario omitted from plan")
            )
            contracts["interfaces"][0]["coverage_checklist"][1]["candidate_gap_ids"].append("omitted-gap")
            (notes / "interface_contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "omits candidate gaps from interface contracts"):
                require_interface_coverage_artifacts(worktree, "tests/gme", selected, manifest)

    def test_interface_coverage_allows_empty_saturated_plan_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            notes = worktree / ".gme-agent"
            notes.mkdir()
            identity = {
                "interface_id": "base.vector.parallel",
                "unique_symbol": "int parallel(const SPAvector &, const SPAvector &)",
                "target_file": "tests/gme/src/base/vector_test.cpp",
            }
            selected = [
                {
                    "id": identity["interface_id"],
                    "unique_symbol": identity["unique_symbol"],
                    "target_file": identity["target_file"],
                }
            ]
            artifacts = {
                "existing_test_coverage.json": {**identity, "existing_tests": [], "covered_scenarios": []},
                "interface_contracts.json": {
                    **identity,
                    "coverage_checklist": self._coverage_checklist(),
                    "candidate_gaps": [],
                },
                "interface_coverage_plan.json": {
                    **identity,
                    "status": "saturated",
                    "closure_audit": self._closure_audit(),
                    "gaps": [],
                },
            }
            for name, interface in artifacts.items():
                (notes / name).write_text(
                    json.dumps({"schema_version": 1, "module": "base", "interfaces": [interface]}),
                    encoding="utf-8",
                )

            summary = require_interface_coverage_artifacts(
                worktree,
                "tests/gme",
                selected,
                {"tests": []},
            )

            self.assertEqual(summary["interfaces"][0]["gap_count"], 0)

    def test_interface_coverage_rejects_unfinished_plan_without_budget_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            notes = worktree / ".gme-agent"
            notes.mkdir()
            identity = {
                "interface_id": "base.vector.parallel",
                "unique_symbol": "int parallel(const SPAvector &, const SPAvector &)",
                "target_file": "tests/gme/src/base/vector_test.cpp",
            }
            selected = [{"id": identity["interface_id"], **{key: identity[key] for key in ("unique_symbol", "target_file")}}]
            (notes / "existing_test_coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "interfaces": [{**identity, "existing_tests": [], "covered_scenarios": []}],
                    }
                ),
                encoding="utf-8",
            )
            (notes / "interface_contracts.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "interfaces": [
                            {
                                **identity,
                                "coverage_checklist": self._coverage_checklist("negative"),
                                "candidate_gaps": [self._candidate_gap("negative", "negative tolerance")],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (notes / "interface_coverage_plan.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "interfaces": [
                            {
                                **identity,
                                "status": "complete",
                                "closure_audit": self._closure_audit(),
                                "gaps": [
                                    {"gap_id": "negative", "scenario": "negative tolerance", "status": "planned"}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "planned without budget_exhausted"):
                require_interface_coverage_artifacts(worktree, "tests/gme", selected, {"tests": []})

    def test_commit_paths_only_commits_selected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo, "selected.cpp", "old selected\n")
            (repo / "other.cpp").write_text("old other\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "add other"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (repo / "selected.cpp").write_text("new selected\n", encoding="utf-8")
            (repo / "other.cpp").write_text("new other\n", encoding="utf-8")

            commit_paths(repo, ["selected.cpp"], "commit selected", lambda _level, _message: None)

            changed = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
            ).stdout.splitlines()
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(changed, ["selected.cpp"])
            self.assertIn("other.cpp", status)

    def test_create_pr_creates_ready_pr(self) -> None:
        captured: dict[str, list[str]] = {}

        class Proc:
            stdout = "Warning: 1 uncommitted change\nhttps://example.invalid/pull/1\n"
            stderr = ""
            returncode = 0

        def fake_run(cmd: list[str], **_kwargs: object) -> Proc:
            captured["cmd"] = cmd
            return Proc()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("gme_agent.git.diff.shutil.which", return_value="gh"):
                with mock.patch("gme_agent.git.diff.subprocess.run", side_effect=fake_run):
                    url = create_pr(AgentConfig(base_branch="main"), tmp, "skip failures", "body", lambda _level, _message: None)

        self.assertEqual(url, "https://example.invalid/pull/1")
        self.assertNotIn("--draft", captured["cmd"])
        self.assertIn("--base", captured["cmd"])
        self.assertIn("main", captured["cmd"])

    def test_skip_pr_title_lists_all_selected_apis(self) -> None:
        selected_tests = [
            {"api": "api_make_cubic"},
            {"api": "api_make_quintic"},
            {"api": "api_make_cubic"},
            {"api": "api_ndifferentiate_law"},
            {"api": "api_integrate_law"},
        ]

        self.assertEqual(
            _skip_pr_title({"module": " laws "}, selected_tests),
            "test(laws): 补充 api_make_cubic、api_make_quintic、api_ndifferentiate_law、api_integrate_law 测试",
        )

    def test_skip_pr_title_requires_api_metadata(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing API metadata"):
            _skip_pr_title({"module": "laws"}, [{"api": ""}])

    def test_skip_pr_branch_name_is_unique_skip_branch(self) -> None:
        first = _skip_pr_branch_name({"id": "06437831-a6a2", "module": "laws"})
        second = _skip_pr_branch_name({"id": "06437831-a6a2", "module": "laws"})

        self.assertRegex(first, r"^gme-agent/skip-laws-\d{8}-\d{6}-06437831-[0-9a-f]{6}$")
        self.assertNotEqual(first, second)

    def test_selected_pr_branch_name_is_unique_test_branch(self) -> None:
        first = _selected_pr_branch_name({"id": "06437831-a6a2", "module": "laws"})
        second = _selected_pr_branch_name({"id": "06437831-a6a2", "module": "laws"})

        self.assertRegex(first, r"^gme-agent/tests-laws-\d{8}-\d{6}-06437831-[0-9a-f]{6}$")
        self.assertNotEqual(first, second)

    def test_selected_manifest_tests_preserves_request_order_and_rejects_unknown(self) -> None:
        manifest = [
            {"file": "src/laws/a.cpp", "suite": "Suite", "name": "First"},
            {"file": "src/laws/b.cpp", "suite": "Suite", "name": "Second"},
        ]

        selected = _selected_manifest_tests(
            manifest,
            [
                {"suite": "Suite", "name": "Second"},
                {"suite": "Suite", "name": "First"},
            ],
        )

        self.assertEqual([item["name"] for item in selected], ["Second", "First"])
        with self.assertRaisesRegex(RuntimeError, "generated_tests.json"):
            _selected_manifest_tests(manifest, [{"suite": "Suite", "name": "Missing"}])

    def test_selected_tests_are_classified_from_latest_output_and_failures(self) -> None:
        output = """
[       OK ] Suite.Passing (1 ms)
[  SKIPPED ] Suite.AlreadySkipped (0 ms)
[  FAILED  ] Suite.Failing (1 ms)
"""
        passing, skipped, failures = _classify_selected_tests(
            {("Suite", "Passing"), ("Suite", "AlreadySkipped"), ("Suite", "Failing")},
            [{"id": "gmefail-1", "test_suite": "Suite", "test_name": "Failing"}],
            output,
        )

        self.assertEqual(passing, {("Suite", "Passing")})
        self.assertEqual(skipped, {("Suite", "AlreadySkipped")})
        self.assertEqual([item["id"] for item in failures], ["gmefail-1"])

    def test_selected_tests_reject_unknown_status(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            _classify_selected_tests({("Suite", "Unknown")}, [], "[       OK ] Suite.Other (1 ms)")

    def test_selected_pr_body_lists_all_selected_tests(self) -> None:
        selected = [
            {"suite": "Suite", "name": "Passing"},
            {"suite": "Suite", "name": "Failing"},
        ]
        body = _selected_pr_body(
            selected,
            [{"test_suite": "Suite", "test_name": "Failing"}],
        )

        self.assertIn("本次新增测试用例：", body)
        self.assertIn("Suite.Passing", body)
        self.assertIn("Suite.Failing", body)
        self.assertIn("增加 skip", body)

    def test_skip_pr_body_uses_chinese_plain_text(self) -> None:
        body = _skip_pr_body(
            {"id": "job-1", "module": "base"},
            [
                {"test_suite": "BaseGeometryTest", "test_name": "GetPlaneFromPointArrayMatchesAcis"},
                {"test_suite": "BaseGeometryTest", "test_name": "MaxDistanceToParBoxMatchesAcis"},
            ],
            "BaseGeometryTest.GetPlaneFromPointArrayMatchesAcis:BaseGeometryTest.MaxDistanceToParBoxMatchesAcis",
        )

        self.assertEqual(
            body,
            "\n".join(
                [
                    "该 PR 由 GME Test Agent 自动生成，新增 GME vs ACIS 对比测试，并对当前已确认存在差异的失败用例增加 skip。",
                    "",
                    "本次新增测试用例：",
                    "BaseGeometryTest.GetPlaneFromPointArrayMatchesAcis",
                    "BaseGeometryTest.MaxDistanceToParBoxMatchesAcis",
                ]
            ),
        )

    def test_failure_suite_filter_uses_exact_failed_tests(self) -> None:
        self.assertEqual(
            _failure_suite_filter(
                [
                    {"test_suite": "BaseGeometryTest", "test_name": "GetPlaneFromPointArrayMatchesAcis"},
                    {"test_suite": "BaseGeometryTest", "test_name": "MaxDistanceToParBoxMatchesAcis"},
                    {"test_suite": "BaseGeometryTest", "test_name": "GetPlaneFromPointArrayMatchesAcis"},
                ]
            ),
            "BaseGeometryTest.GetPlaneFromPointArrayMatchesAcis:BaseGeometryTest.MaxDistanceToParBoxMatchesAcis",
        )

    def test_prune_generated_test_text_keeps_only_skipped_failures(self) -> None:
        text = """#include "gtest/include/gtest.h"

namespace {
void Helper() {}
}

class Suite : public ::testing::Test {};

TEST_F(Suite, PassingCase) {
    Helper();
    EXPECT_TRUE(true);
}

TEST_F(Suite, FailingCase) {
    GTEST_SKIP() << "[gme-agent-known-failure:gmefail-1] mismatch";
    Helper();
    EXPECT_TRUE(false);
}
"""

        pruned = _prune_generated_test_text(text, {("Suite", "FailingCase")}, "generated.cpp")

        self.assertIn("void Helper()", pruned)
        self.assertIn("TEST_F(Suite, FailingCase)", pruned)
        self.assertIn("GTEST_SKIP()", pruned)
        self.assertNotIn("TEST_F(Suite, PassingCase)", pruned)

    def test_prune_generated_test_text_requires_skip_marker(self) -> None:
        text = """#include "gtest/include/gtest.h"

TEST_F(Suite, FailingCase) {
    EXPECT_TRUE(false);
}
"""

        with self.assertRaisesRegex(RuntimeError, "does not contain GTEST_SKIP"):
            _prune_generated_test_text(text, {("Suite", "FailingCase")}, "generated.cpp")

    def test_prune_manifest_tests_removes_only_generated_passing_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            rel_path = "src/laws/kernel_kernapi_test.cpp"
            file_path = target / rel_path
            file_path.parent.mkdir(parents=True)
            file_path.write_text(
                """#include "gtest/include/gtest.h"

TEST_F(LawsKernapiTest, ExistingManualCase) {
    EXPECT_TRUE(true);
}

TEST_F(LawsKernapiTest, GeneratedPassingCase) {
    EXPECT_TRUE(true);
}

TEST_F(LawsKernapiTest, GeneratedFailingCase) {
    GTEST_SKIP() << "[gme-agent-known-failure:gmefail-1] ACIS/GME mismatch";
    EXPECT_TRUE(false);
}
""",
                encoding="utf-8",
            )

            _prune_manifest_tests_to_failures(
                target,
                [rel_path],
                [{"test_suite": "LawsKernapiTest", "test_name": "GeneratedFailingCase"}],
                [
                    {"file": rel_path, "suite": "LawsKernapiTest", "name": "GeneratedPassingCase"},
                    {"file": rel_path, "suite": "LawsKernapiTest", "name": "GeneratedFailingCase"},
                ],
                lambda _level, _message: None,
            )

            pruned = file_path.read_text(encoding="utf-8")
            self.assertIn("TEST_F(LawsKernapiTest, ExistingManualCase)", pruned)
            self.assertIn("TEST_F(LawsKernapiTest, GeneratedFailingCase)", pruned)
            self.assertIn("GTEST_SKIP()", pruned)
            self.assertNotIn("GeneratedPassingCase", pruned)

    def test_prune_manifest_tests_keeps_only_selected_generated_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            rel_path = "src/laws/law_base_test.cpp"
            file_path = target / rel_path
            file_path.parent.mkdir(parents=True)
            file_path.write_text(
                """TEST_F(LawsBaseTest, ExistingManualCase) {
    EXPECT_TRUE(true);
}

TEST_F(LawsBaseTest, GeneratedPassingCase) {
    EXPECT_TRUE(true);
}

TEST_F(LawsBaseTest, GeneratedFailingCase) {
    GTEST_SKIP() << "[gme-agent-known-failure:gmefail-1] mismatch";
}

TEST_F(LawsBaseTest, GeneratedUnselectedCase) {
    EXPECT_TRUE(true);
}
""",
                encoding="utf-8",
            )

            manifest = [
                {"file": rel_path, "suite": "LawsBaseTest", "name": "GeneratedPassingCase"},
                {"file": rel_path, "suite": "LawsBaseTest", "name": "GeneratedFailingCase"},
                {"file": rel_path, "suite": "LawsBaseTest", "name": "GeneratedUnselectedCase"},
            ]
            _prune_manifest_tests_to_selection(
                target,
                [rel_path],
                {("LawsBaseTest", "GeneratedPassingCase"), ("LawsBaseTest", "GeneratedFailingCase")},
                {("LawsBaseTest", "GeneratedFailingCase")},
                manifest,
                lambda _level, _message: None,
            )

            pruned = file_path.read_text(encoding="utf-8")
            self.assertIn("ExistingManualCase", pruned)
            self.assertIn("GeneratedPassingCase", pruned)
            self.assertIn("GeneratedFailingCase", pruned)
            self.assertNotIn("GeneratedUnselectedCase", pruned)

    def test_selected_pr_verification_requires_each_expected_status(self) -> None:
        output = """
[       OK ] Suite.Passing (1 ms)
[  SKIPPED ] Suite.Failing (0 ms)
"""
        selected = {("Suite", "Passing"), ("Suite", "Failing")}
        _require_selected_tests_reported(output, selected)
        _validate_selected_test_results(output, {("Suite", "Passing")}, {("Suite", "Failing")})

        with self.assertRaisesRegex(RuntimeError, "did not appear"):
            _require_selected_tests_reported(output, selected | {("Suite", "Missing")})
        with self.assertRaisesRegex(RuntimeError, "expected SKIPPED"):
            _validate_selected_test_results(output, set(), {("Suite", "Passing")})

    def test_generated_test_snapshot_restore_keeps_local_full_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            rel_path = "src/base/gme_agent_base_generated_test.cpp"
            file_path = target / rel_path
            file_path.parent.mkdir(parents=True)
            file_path.write_text("full generated tests with skips\n", encoding="utf-8")

            snapshots = _snapshot_generated_tests(target, [rel_path])
            file_path.write_text("pruned PR version\n", encoding="utf-8")
            _restore_generated_tests(target, snapshots, lambda _level, _message: None)

            self.assertEqual(file_path.read_text(encoding="utf-8"), "full generated tests with skips\n")

    def test_selected_blocks_are_inserted_into_latest_file_using_neighbor_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_target = root / "old"
            latest_target = root / "latest"
            rel_path = "src/laws/kernel_kernapi_test.cpp"
            old_file = old_target / rel_path
            latest_file = latest_target / rel_path
            old_file.parent.mkdir(parents=True)
            latest_file.parent.mkdir(parents=True)
            old_file.write_text(
                """TEST_F(LawsTest, ExistingBefore) {
    EXPECT_TRUE(true);
}

TEST_F(LawsTest, SelectedGenerated) {
    GTEST_SKIP() << "difference";
}

TEST_F(LawsTest, ExistingAfter) {
    EXPECT_TRUE(true);
}
""",
                encoding="utf-8",
            )
            latest_file.write_text(
                """TEST_F(LawsTest, ExistingBefore) {
    EXPECT_TRUE(true);
}

TEST_F(LawsTest, NewMainTest) {
    EXPECT_TRUE(true);
}

TEST_F(LawsTest, ExistingAfter) {
    EXPECT_TRUE(true);
}
""",
                encoding="utf-8",
            )

            selected = [{"file": rel_path, "suite": "LawsTest", "name": "SelectedGenerated"}]
            blocks = _extract_selected_test_blocks(old_target, selected)
            base_sources = _insert_selected_test_blocks(latest_target, blocks, lambda _level, _message: None)
            result = latest_file.read_text(encoding="utf-8")

            self.assertIn("TEST_F(LawsTest, SelectedGenerated)", result)
            self.assertIn("TEST_F(LawsTest, NewMainTest)", result)
            self.assertLess(result.index("ExistingBefore"), result.index("SelectedGenerated"))
            self.assertEqual(set(base_sources), {rel_path})

    def test_latest_file_insertion_preserves_utf8_bom_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            rel_path = "src/base/sample_test.cpp"
            file_path = target / rel_path
            file_path.parent.mkdir(parents=True)
            original = (
                "TEST_F(BaseTest, Existing) {\r\n"
                "    EXPECT_TRUE(true);\r\n"
                "}\r\n"
            )
            file_path.write_bytes(b"\xef\xbb\xbf" + original.encode("utf-8"))
            block = SelectedTestBlock(
                rel_path=rel_path,
                suite="BaseTest",
                name="Generated",
                text="TEST_F(BaseTest, Generated) {\n    EXPECT_TRUE(true);\n}",
                previous_test=("BaseTest", "Existing"),
                next_test=None,
            )

            _insert_selected_test_blocks(target, [block], lambda _level, _message: None)
            raw = file_path.read_bytes()
            source = _read_utf8_source(file_path)

            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(source.newline, "\r\n")
            self.assertNotIn("\n", source.text.replace("\r\n", ""))
            self.assertIn("TEST_F(BaseTest, Generated)", source.text)

    def test_latest_file_insertion_rejects_duplicate_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            rel_path = "src/laws/test.cpp"
            file_path = target / rel_path
            file_path.parent.mkdir(parents=True)
            file_path.write_text("TEST_F(Suite, Generated) {\n}\n", encoding="utf-8")
            block = SelectedTestBlock(
                rel_path=rel_path,
                suite="Suite",
                name="Generated",
                text="TEST_F(Suite, Generated) {\n}\n",
                previous_test=None,
                next_test=None,
            )

            with self.assertRaisesRegex(RuntimeError, "already exists"):
                _insert_selected_test_blocks(target, [block], lambda _level, _message: None)

    def test_fresh_pr_validation_rejects_changes_to_existing_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            rel_path = "src/laws/test.cpp"
            file_path = target / rel_path
            file_path.parent.mkdir(parents=True)
            file_path.write_text("TEST_F(Suite, Existing) {\n    EXPECT_TRUE(true);\n}\n", encoding="utf-8")
            base_sources = {rel_path: _read_utf8_source(file_path)}
            current = base_sources[rel_path]
            changed = current.text.replace("EXPECT_TRUE(true)", "EXPECT_TRUE(false)")
            file_path.write_bytes((b"\xef\xbb\xbf" if current.has_bom else b"") + changed.encode("utf-8"))

            with self.assertRaisesRegex(RuntimeError, "changed existing main tests"):
                _validate_fresh_pr_files(target, base_sources, [], "origin/main")

    def test_fresh_pr_branch_uses_latest_remote_main_and_restores_task_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote"
            task = root / "task"
            self._init_repo(remote, "src/laws/test.cpp", "initial\n")
            self._clone_repo(remote, task)
            subprocess.run(["git", "checkout", "-b", "task-branch"], cwd=task, check=True, stdout=subprocess.PIPE)
            (task / "src/laws/test.cpp").write_text("task generated tests\n", encoding="utf-8")

            (remote / "latest.txt").write_text("latest main\n", encoding="utf-8")
            subprocess.run(["git", "add", "latest.txt"], cwd=remote, check=True)
            subprocess.run(["git", "commit", "-m", "advance main"], cwd=remote, check=True, stdout=subprocess.PIPE)

            stash_commit = _stash_task_target_changes(task, "job-1", lambda _level, _message: None)
            _checkout_fresh_pr_branch(task, "pr-branch", "origin", "main", lambda _level, _message: None)

            self.assertRegex(stash_commit, r"^[0-9a-f]{40}$")
            self.assertTrue((task / "latest.txt").exists())
            self.assertEqual((task / "src/laws/test.cpp").read_text(encoding="utf-8"), "initial\n")

            subprocess.run(["git", "checkout", "task-branch"], cwd=task, check=True, stdout=subprocess.PIPE)
            _restore_task_target_changes(task, stash_commit, lambda _level, _message: None)
            self.assertEqual((task / "src/laws/test.cpp").read_text(encoding="utf-8"), "task generated tests\n")

    def test_format_generated_tests_uses_current_worktree_clang_format(self) -> None:
        captured: dict[str, object] = {}

        class Proc:
            stdout = ""
            returncode = 0

        def fake_run(cmd: list[str], **kwargs: object) -> Proc:
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            return Proc()

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            target = Path(tmp) / "tests-gme"
            worktree.mkdir()
            target.mkdir()
            (worktree / ".clang-format").write_text("BasedOnStyle: Google\n", encoding="utf-8")

            with mock.patch("gme_agent.flows.skip_pr_flow.shutil.which", return_value="clang-format"):
                with mock.patch("gme_agent.flows.skip_pr_flow.subprocess.run", side_effect=fake_run):
                    _format_generated_tests(worktree, target, ["src/laws/test.cpp"], lambda _level, _message: None)

        cmd = captured["cmd"]
        self.assertIsInstance(cmd, list)
        self.assertIn("-i", cmd)
        self.assertIn(f"--style=file:{worktree / '.clang-format'}", cmd)
        self.assertIn("src/laws/test.cpp", cmd)
        self.assertEqual(captured["cwd"], str(target))

    def test_command_mapping_reproduce_command_uses_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(
                database_path=str(Path(tmp) / "agent.db"),
                test_executable="{build_dir}/tests.exe",
                test_command="{test_executable} --gtest_filter={gtest_filter}",
            )
            db = AgentDb(cfg.database_path)
            try:
                orchestrator = Orchestrator(cfg, db)
                cmd = orchestrator._reproduce_command("Suite.Test")
                self.assertIn("--gtest_filter=Suite.Test", cmd)
                self.assertNotIn("FORCE_RUN_ALL", cmd)
            finally:
                db.close()

    def test_command_mapping_adds_selected_module_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(database_path=str(Path(tmp) / "agent.db"))
            db = AgentDb(cfg.database_path)
            try:
                db.create_job(job_id="job-laws", job_type="test_generation", title="Generate laws tests", module="laws")
                db.create_job(
                    job_id="job-kernel",
                    job_type="test_generation",
                    title="Generate kernel tests",
                    module="kernel",
                )
                orchestrator = Orchestrator(cfg, db)
                mapping = orchestrator._command_mapping(
                    Path("D:/worktree"),
                    Path("D:/worktree/build/vscode"),
                    "*",
                    artifact_dir=Path(tmp) / "job-laws",
                )
                self.assertEqual(mapping["test_module_name"], "laws")
                self.assertEqual(mapping["develop_module_option"], "-DDEVELOP_LAWS=ON")
                self.assertEqual(mapping["test_module_option"], "-DTEST_LAWS=ON")

                kernel_mapping = orchestrator._command_mapping(
                    Path("D:/worktree"),
                    Path("D:/worktree/build/vscode"),
                    "*",
                    artifact_dir=Path(tmp) / "job-kernel",
                )
                self.assertEqual(kernel_mapping["test_module_name"], "kernel")
                self.assertEqual(kernel_mapping["develop_module_option"], "-DDEVELOP_KERNEL=ON")
                self.assertEqual(kernel_mapping["test_module_option"], "-DTEST_KERNEL=ON")

                db.create_job(
                    job_id="job-laws-kernel-fix",
                    job_type="bug_fix",
                    title="Fix api_str_to_law",
                    module="laws",
                    metadata={"fix_target_repo": "module/kernel"},
                )
                repair_mapping = orchestrator._command_mapping(
                    Path("D:/worktree"),
                    Path("D:/worktree/build/vscode"),
                    "Suite.Case",
                    artifact_dir=Path(tmp) / "job-laws-kernel-fix",
                )
                self.assertEqual(repair_mapping["test_module_name"], "laws")
                self.assertEqual(repair_mapping["develop_module_option"], "-DDEVELOP_LAWS=ON -DDEVELOP_KERNEL=ON")
                self.assertEqual(repair_mapping["test_module_option"], "-DTEST_LAWS=ON")
            finally:
                db.close()

    def test_load_config_drops_legacy_force_run_all_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                """{
  "configure_command": "cmake -S {worktree} -B {build_dir} -DFORCE_RUN_ALL={force_run_all}"
}
""",
                encoding="utf-8",
            )

            loaded = load_config(path)

            self.assertNotIn("FORCE_RUN_ALL", loaded.configure_command)
            self.assertNotIn("force_run_all", loaded.configure_command)
            self.assertNotIn("GME_FULL_MODE", loaded.configure_command)
            self.assertNotIn("GME_HUDONG_MODE", loaded.configure_command)

    def test_load_config_overrides_legacy_full_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                """{
  "configure_command": "cmake -S {worktree} -B {build_dir} -DGME_FULL_MODE=ON -DGME_HUDONG_MODE=ON {develop_module_option}"
}
""",
                encoding="utf-8",
            )

            loaded = load_config(path)

            self.assertNotIn("-DGME_FULL_MODE=ON", loaded.configure_command)
            self.assertNotIn("-DGME_HUDONG_MODE=ON", loaded.configure_command)
            self.assertNotIn("GME_FULL_MODE", loaded.configure_command)
            self.assertNotIn("GME_HUDONG_MODE", loaded.configure_command)

    def test_module_scoped_submodule_paths_keep_only_required_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitmodules").write_text(
                """
[submodule "tests/gme"]
    path = tests/gme
    url = https://example.invalid/tests.git
[submodule "_deps/acis"]
    path = _deps/acis
    url = https://example.invalid/acis.git
[submodule "tests/hudong"]
    path = tests/hudong
    url = https://example.invalid/tests-hudong.git
[submodule "tests/yunji"]
    path = tests/yunji
    url = https://example.invalid/tests-yunji.git
[submodule "tests/haizhou"]
    path = tests/haizhou
    url = https://example.invalid/tests-haizhou.git
[submodule "data/public"]
    path = data/public
    url = https://example.invalid/data-public.git
[submodule "data/gme"]
    path = data/gme
    url = https://example.invalid/data-gme.git
[submodule "module/laws"]
    path = module/laws
    url = https://example.invalid/laws.git
[submodule "module/base"]
    path = module/base
    url = https://example.invalid/base.git
[submodule "module/kernel"]
    path = module/kernel
    url = https://example.invalid/kernel.git
""".lstrip(),
                encoding="utf-8",
            )

            paths = module_scoped_submodule_paths(AgentConfig(), root, "laws", "tests/gme")
            kernel_paths = module_scoped_submodule_paths(AgentConfig(), root, "kernel", "tests/gme")

            self.assertEqual(
                paths,
                ["tests/gme", "tests/hudong", "tests/yunji", "tests/haizhou", "module/laws", "module/kernel", "_deps/acis"],
            )
            self.assertEqual(
                kernel_paths,
                ["tests/gme", "tests/hudong", "tests/yunji", "tests/haizhou", "module/kernel", "_deps/acis"],
            )

            self.assertEqual(repair_candidate_repos(AgentConfig(), "laws"), ["module/laws", "module/kernel"])
            self.assertEqual(repair_candidate_repos(AgentConfig(), "kernel"), ["module/kernel"])

    def test_detect_fix_target_selects_only_changed_candidate_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            laws = root / "module" / "laws"
            kernel = root / "module" / "kernel"
            tests = root / "tests" / "gme"
            acis = root / "_deps" / "acis"
            for path in (laws, kernel, tests, acis):
                path.mkdir(parents=True)

            def status(path, _emit=None):
                resolved = Path(path)
                if resolved == kernel:
                    return " M src/kernapi/kernapi.cpp\n"
                if resolved == root:
                    return " M module/kernel\n M tests/gme\n"
                return ""

            with mock.patch("gme_agent.flows.bug_fix_flow.git_diff", return_value="baseline\n"), mock.patch(
                "gme_agent.flows.bug_fix_flow.git_status",
                side_effect=status,
            ):
                target, files = _detect_fix_target(
                    root,
                    ["module/laws", "module/kernel"],
                    "tests/gme",
                    "baseline\n",
                    ["module/laws", "module/kernel", "tests/gme", "_deps/acis"],
                )

            self.assertEqual(target, "module/kernel")
            self.assertEqual(files, ["src/kernapi/kernapi.cpp"])

    def test_validate_config_reports_unknown_placeholder(self) -> None:
        cfg = AgentConfig(configure_command="cmake -S {unknown}")
        result = validate_config(cfg)
        placeholder_checks = [c for c in result["checks"] if c["name"] == "Template placeholders: configure_command"]
        self.assertEqual(len(placeholder_checks), 1)
        self.assertFalse(placeholder_checks[0]["ok"])

    def test_validate_config_reports_harness_sdk(self) -> None:
        cfg = AgentConfig()
        result = validate_config(cfg)
        sdk_checks = [c for c in result["checks"] if c["name"] == "DeepSeek Harness Python SDK"]
        self.assertEqual(len(sdk_checks), 1)
        self.assertTrue(sdk_checks[0]["ok"])

    def test_git_diff_includes_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init"], cwd=tmp, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            Path(tmp, "new_test.cpp").write_text("TEST(Suite, Case) {}\n", encoding="utf-8")

            diff = git_diff(tmp)

            self.assertIn("new file mode", diff)
            self.assertIn("new_test.cpp", diff)
            self.assertIn("+TEST(Suite, Case) {}", diff)

    def test_git_diff_uses_histogram_for_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "gme_agent.git.diff.run_git",
                side_effect=["diff --git a/test.cpp b/test.cpp\n", ""],
            ) as run_git_mock:
                diff = git_diff(tmp)

            self.assertIn("diff --git", diff)
            self.assertEqual(
                run_git_mock.call_args_list[0].args[0],
                ["diff", "--histogram", "--", "."],
            )

    def test_git_diff_preserves_trailing_blank_context_line(self) -> None:
        tracked_diff = (
            "diff --git a/test.cpp b/test.cpp\n"
            "--- a/test.cpp\n"
            "+++ b/test.cpp\n"
            "@@ -1,2 +1,2 @@\n"
            "-old\n"
            "+new\n"
            " \n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("gme_agent.git.diff.run_git", side_effect=[tracked_diff, ""]):
                diff = git_diff(tmp)

        self.assertTrue(diff.endswith(" \n"))

    def test_target_repo_path_selection(self) -> None:
        cfg = AgentConfig(test_target_repo="tests\\gme", module_repo_root="module")
        db = AgentDb(":memory:")
        try:
            orchestrator = Orchestrator(cfg, db)
            self.assertEqual(orchestrator._test_target_repo(), "tests/gme")
            self.assertEqual(orchestrator._module_target_repo("laws"), "module/laws")
        finally:
            db.close()

    def test_submodule_base_branch_from_gitmodules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init"], cwd=tmp, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            Path(tmp, ".gitmodules").write_text(
                """
[submodule "tests/gme"]
    path = tests/gme
    url = https://example.invalid/tests-gme.git
    branch = develop
""".lstrip(),
                encoding="utf-8",
            )

            self.assertEqual(submodule_base_branch(tmp, "tests\\gme", "main"), "develop")
            self.assertEqual(submodule_base_branch(tmp, "module/laws", "main"), "main")

    def test_prepare_worktree_dependencies_uses_only_required_local_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            worktree = root / "worktree"
            kernel_worktree = root / "kernel-worktree"
            remote_tests = root / "remote-tests"
            remote_hudong = root / "remote-hudong"
            remote_yunji = root / "remote-yunji"
            remote_haizhou = root / "remote-haizhou"
            remote_laws = root / "remote-laws"
            remote_kernel = root / "remote-kernel"
            remote_acis = root / "remote-acis"
            worktree.mkdir()
            kernel_worktree.mkdir()

            self._init_repo(remote_tests, "src/laws/existing_test.cpp", "TEST(Existing, Case) {}\n")
            self._init_repo(remote_hudong, "README.md", "hudong tests\n")
            self._init_repo(remote_yunji, "README.md", "yunji tests\n")
            self._init_repo(remote_haizhou, "README.md", "haizhou tests\n")
            self._init_repo(remote_laws, "laws.cpp", "int laws_value = 1;\n")
            self._init_repo(remote_kernel, "kernel.cpp", "int kernel_value = 1;\n")
            self._init_repo(remote_acis, "acis.cpp", "int acis_value = 1;\n")
            self._clone_repo(remote_tests, source_root / "tests" / "gme")
            self._clone_repo(remote_hudong, source_root / "tests" / "hudong")
            self._clone_repo(remote_yunji, source_root / "tests" / "yunji")
            self._clone_repo(remote_haizhou, source_root / "tests" / "haizhou")
            self._clone_repo(remote_laws, source_root / "module" / "laws")
            self._clone_repo(remote_kernel, source_root / "module" / "kernel")
            self._clone_repo(remote_acis, source_root / "_deps" / "acis")

            gitmodules_content = f"""
[submodule "tests/gme"]
    path = tests/gme
    url = {remote_tests.as_posix()}
    branch = main
[submodule "_deps/acis"]
    path = _deps/acis
    url = {remote_acis.as_posix()}
    branch = main
[submodule "tests/hudong"]
    path = tests/hudong
    url = {remote_hudong.as_posix()}
    branch = main
[submodule "tests/yunji"]
    path = tests/yunji
    url = {remote_yunji.as_posix()}
    branch = main
[submodule "tests/haizhou"]
    path = tests/haizhou
    url = {remote_haizhou.as_posix()}
    branch = main
[submodule "data/gme"]
    path = data/gme
    url = https://example.invalid/data-gme.git
    branch = main
[submodule "data/public"]
    path = data/public
    url = https://example.invalid/data-public.git
    branch = main
[submodule "module/laws"]
    path = module/laws
    url = {remote_laws.as_posix()}
    branch = main
[submodule "module/kernel"]
    path = module/kernel
    url = {remote_kernel.as_posix()}
    branch = main
[submodule "module/base"]
    path = module/base
    url = https://example.invalid/base.git
    branch = main
""".lstrip()
            (worktree / ".gitmodules").write_text(gitmodules_content, encoding="utf-8")
            (kernel_worktree / ".gitmodules").write_text(gitmodules_content, encoding="utf-8")

            cfg = AgentConfig(gme_repo_path=str(source_root))
            prepared = prepare_worktree_dependencies(cfg, worktree, "laws", "tests/gme", lambda _level, _message: None)
            kernel_prepared = prepare_worktree_dependencies(
                cfg,
                kernel_worktree,
                "kernel",
                "tests/gme",
                lambda _level, _message: None,
            )

            self.assertEqual(
                prepared,
                ["tests/gme", "tests/hudong", "tests/yunji", "tests/haizhou", "module/laws", "module/kernel", "_deps/acis"],
            )
            self.assertEqual(
                kernel_prepared,
                ["tests/gme", "tests/hudong", "tests/yunji", "tests/haizhou", "module/kernel", "_deps/acis"],
            )
            self.assertTrue((worktree / "tests" / "gme" / ".git").exists())
            self.assertTrue((worktree / "tests" / "gme" / "src" / "laws" / "existing_test.cpp").exists())
            self.assertTrue((worktree / "tests" / "hudong" / ".git").exists())
            self.assertTrue((worktree / "tests" / "yunji" / ".git").exists())
            self.assertTrue((worktree / "tests" / "haizhou" / ".git").exists())
            self.assertTrue((worktree / "module" / "laws" / "laws.cpp").exists())
            self.assertTrue((worktree / "module" / "laws" / ".git").exists())
            self.assertTrue((worktree / "module" / "kernel" / "kernel.cpp").exists())
            self.assertTrue((worktree / "module" / "kernel" / ".git").exists())
            self.assertTrue((worktree / "_deps" / "acis" / ".git").exists())
            self.assertFalse((worktree / "data" / "gme").exists())
            self.assertFalse((worktree / "data" / "public").exists())
            self.assertFalse((worktree / "module" / "base").exists())
            self.assertTrue((kernel_worktree / "tests" / "gme" / ".git").exists())
            self.assertTrue((kernel_worktree / "module" / "kernel" / "kernel.cpp").exists())
            self.assertTrue((kernel_worktree / "module" / "kernel" / ".git").exists())
            self.assertTrue((kernel_worktree / "_deps" / "acis" / ".git").exists())
            self.assertFalse((kernel_worktree / "module" / "laws").exists())

    def test_prepare_worktree_dependencies_clones_missing_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_repo = root / "remote-test"
            source_root = root / "source"
            worktree = root / "worktree"
            source_root.mkdir()
            (source_root / "tests" / "gme").mkdir(parents=True)
            worktree.mkdir()

            self._init_repo(remote_repo, "README.md", "test repo\n")
            (worktree / ".gitmodules").write_text(
                f"""
[submodule "tests/gme"]
    path = tests/gme
    url = {remote_repo.as_posix()}
    branch = main
""".lstrip(),
                encoding="utf-8",
            )

            cfg = AgentConfig(gme_repo_path=str(source_root))
            prepared = prepare_worktree_dependencies(cfg, worktree, "laws", "tests/gme", lambda _level, _message: None)

            self.assertEqual(prepared, ["tests/gme"])
            self.assertTrue((worktree / "tests" / "gme" / ".git").exists())
            self.assertTrue((worktree / "tests" / "gme" / "README.md").exists())

    def test_only_target_repo_changes_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init"], cwd=tmp, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess.run(["git", "config", "user.email", "agent@example.invalid"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "GME Agent"], cwd=tmp, check=True)
            target = Path(tmp, "tests", "gme")
            target.mkdir(parents=True)
            (target / "existing.cpp").write_text("int old_value = 1;\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmp, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            (target / "existing.cpp").write_text("int old_value = 2;\n", encoding="utf-8")
            Path(tmp, "timer_res_.csv").write_text("", encoding="utf-8")
            ensure_only_target_repo_changed(tmp, normalize_repo_path("tests\\gme"))

            Path(tmp, "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ensure_only_target_repo_changed(tmp, "tests/gme")

    def test_load_config_options_from_git_and_gitmodules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init"], cwd=tmp, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess.run(["git", "config", "user.email", "agent@example.invalid"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "GME Agent"], cwd=tmp, check=True)
            Path(tmp, "README.md").write_text("test repo\n", encoding="utf-8")
            Path(tmp, ".gitmodules").write_text(
                """
[submodule "tests/gme"]
    path = tests/gme
    url = https://example.invalid/tests-gme.git
    branch = main
[submodule "module/laws"]
    path = module/laws
    url = https://example.invalid/laws.git
    branch = develop
""".lstrip(),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmp, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess.run(["git", "branch", "-M", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/gme.git"], cwd=tmp, check=True)

            options = load_config_options(AgentConfig(gme_repo_path=tmp))

            self.assertIn("main", options["branches"])
            self.assertIn("origin", options["remotes"])
            self.assertIn("tests/gme", options["test_repos"])
            self.assertIn("laws", options["modules"])
            self.assertIn("module/laws", options["module_repos"])
            self.assertIn("gme-test-generation", options["builtin_skills"])
            self.assertIn("gme-module-test-analyzer", options["builtin_skills"])
            self.assertIn("gme-acis-interface-analyzer", options["builtin_skills"])
            self.assertIn("gme-test-writer", options["builtin_skills"])
            self.assertIn("gme-bug-fix", options["builtin_skills"])

    def test_builtin_skill_dirs_exist(self) -> None:
        self.assertIsNotNone(HarnessRunner._builtin_skill_dir("gme-test-generation"))
        self.assertIsNotNone(HarnessRunner._builtin_skill_dir("gme-module-test-analyzer"))
        self.assertIsNotNone(HarnessRunner._builtin_skill_dir("gme-acis-interface-analyzer"))
        self.assertIsNotNone(HarnessRunner._builtin_skill_dir("gme-test-writer"))
        self.assertIsNotNone(HarnessRunner._builtin_skill_dir("gme-bug-fix"))

    def test_harness_run_input_explicitly_invokes_resolved_skills(self) -> None:
        runner = HarnessRunner(AgentConfig(), lambda _level, _message: None)
        self.assertEqual(
            runner._run_input("生成测试", ["gme-test-generation", "gme-test-writer"]),
            "$gme-test-generation\n$gme-test-writer\n\n生成测试",
        )

    def test_harness_run_input_without_skills_sends_plain_prompt(self) -> None:
        runner = HarnessRunner(AgentConfig(use_builtin_skills=False), lambda _level, _message: None)
        self.assertEqual(runner._run_input("生成测试", []), "生成测试")

    def test_harness_stages_skills_in_repository_discovery_path_and_cleans_them(self) -> None:
        events = []
        runner = HarnessRunner(AgentConfig(), lambda level, message: events.append((level, message)))

        with tempfile.TemporaryDirectory() as tmp:
            skill_path = Path(tmp) / ".agents" / "skills" / "gme-test-generation" / "SKILL.md"
            with runner._staged_skills(tmp, ["gme-test-generation", "missing-skill"]) as names:
                self.assertEqual(names, ["gme-test-generation"])
                self.assertTrue(skill_path.is_file())
                self.assertIn("name: gme-test-generation", skill_path.read_text(encoding="utf-8"))

            self.assertFalse(skill_path.exists())
            self.assertFalse((Path(tmp) / ".agents").exists())

        self.assertTrue(any(level == "warn" and "missing-skill" in message for level, message in events))

    def test_test_generation_loads_staged_skills(self) -> None:
        db = AgentDb(":memory:")
        try:
            orchestrator = Orchestrator(AgentConfig(), db)
            self.assertEqual(
                orchestrator._test_skill_names(),
                [
                    "gme-test-generation",
                    "gme-module-test-analyzer",
                    "gme-acis-interface-analyzer",
                    "gme-test-writer",
                ],
            )
        finally:
            db.close()

    def test_generation_flow_runs_harness_agent_and_persists_session(self) -> None:
        db = AgentDb(":memory:")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = AgentConfig(artifact_root=str(Path(tmp) / "artifacts"))
                orchestrator = Orchestrator(config, db)
                db.create_job(
                    job_id="job-1",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    api_name="api_x",
                    metadata={},
                )
                worktree = Path(tmp) / "worktree"
                worktree.mkdir()
                fake_worktree = types.SimpleNamespace(branch="job-branch", path=worktree)
                target = types.SimpleNamespace(
                    rel_path="tests/gme",
                    branch="target-branch",
                    path=worktree / "tests/gme",
                    base_branch="main",
                )
                calls: dict[str, Any] = {}

                def fake_run(runner_self, prompt, cwd, session_id=None, skill_names=None, on_session_started=None):
                    calls["cwd"] = Path(cwd)
                    calls["session_id"] = session_id
                    calls["skill_names"] = list(skill_names or [])
                    calls["status_during_run"] = db.get_job("job-1")["status"]
                    if on_session_started:
                        on_session_started(session_id or "gme-generated-session")
                    return HarnessResult(
                        final_response="agent finished",
                        session_id=session_id or "gme-generated-session",
                        finish_reason="completed",
                        raw="raw",
                    )

                with mock.patch(
                    "gme_agent.flows.test_generation_flow.create_worktree", return_value=fake_worktree
                ), mock.patch(
                    "gme_agent.flows.test_generation_flow.prepare_worktree_dependencies", return_value=[]
                ), mock.patch(
                    "gme_agent.flows.test_generation_flow.prepare_target_repo_from_remote", return_value=target
                ), mock.patch(
                    "gme_agent.flows.test_generation_flow.ensure_only_target_repo_changed"
                ), mock.patch(
                    "gme_agent.flows.test_generation_flow._generated_manifest_metadata", return_value={}
                ), mock.patch(
                    "gme_agent.flows.test_generation_flow.HarnessRunner.run", new=fake_run
                ), mock.patch.object(orchestrator, "_write_job_artifacts"):
                    run_test_generation_job(orchestrator, "job-1", "laws", "api_x", "tests/gme", None)

                artifact_dir = Path(config.artifact_root) / "job-1"
                job = db.get_job("job-1")
                self.assertEqual(job["status"], "needs_review")
                self.assertIn("harness_session_id", job)
                self.assertNotIn("codex_thread_id", job)
                self.assertEqual(job["harness_session_id"], "gme-generated-session")
                self.assertEqual(calls["status_during_run"], "running_agent")
                self.assertIsNone(calls["session_id"])
                self.assertEqual(calls["cwd"], worktree)
                self.assertEqual(calls["skill_names"], orchestrator._test_skill_names())
                self.assertEqual(
                    (artifact_dir / "agent_result.txt").read_text(encoding="utf-8"),
                    "agent finished",
                )
                self.assertTrue((artifact_dir / "test_generation_prompt.md").exists())
        finally:
            db.close()

    def test_extension_flow_resumes_stored_harness_session(self) -> None:
        db = AgentDb(":memory:")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = AgentConfig(artifact_root=str(Path(tmp) / "artifacts"))
                orchestrator = Orchestrator(config, db)
                worktree = Path(tmp) / "worktree"
                notes = worktree / ".gme-agent"
                notes.mkdir(parents=True)
                (notes / "generated_tests.json").write_text('{"tests": [], "files": []}', encoding="utf-8")
                db.create_job(
                    job_id="job-2",
                    job_type="test_generation",
                    title="Generate laws tests",
                    module="laws",
                    api_name="api_x",
                    metadata={"target_repo": "tests/gme", "prepared_paths": []},
                )
                db.update_job(
                    "job-2",
                    status="needs_review",
                    worktree_path=str(worktree),
                    harness_session_id="gme-stored-session",
                )
                calls: dict[str, Any] = {}

                def fake_run(runner_self, prompt, cwd, session_id=None, skill_names=None, on_session_started=None):
                    calls["session_id"] = session_id
                    if on_session_started:
                        on_session_started(session_id)
                    return HarnessResult(
                        final_response="extended",
                        session_id=session_id,
                        finish_reason="completed",
                        raw="raw",
                    )

                with mock.patch(
                    "gme_agent.flows.test_generation_flow.ensure_only_target_repo_changed"
                ), mock.patch(
                    "gme_agent.flows.test_generation_flow._generated_manifest_metadata", return_value={}
                ), mock.patch(
                    "gme_agent.flows.test_generation_flow.HarnessRunner.run", new=fake_run
                ), mock.patch.object(orchestrator, "_write_job_artifacts"):
                    run_test_extension_job(orchestrator, "job-2", "api_y", None)

                artifact_dir = Path(config.artifact_root) / "job-2"
                job = db.get_job("job-2")
                self.assertEqual(job["status"], "needs_review")
                self.assertEqual(job["harness_session_id"], "gme-stored-session")
                self.assertEqual(calls["session_id"], "gme-stored-session")
                self.assertEqual(
                    (artifact_dir / "agent_result.txt").read_text(encoding="utf-8"),
                    "extended",
                )
                self.assertTrue((artifact_dir / "agent_extend_result.txt").exists())
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
