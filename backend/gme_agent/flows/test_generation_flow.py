from __future__ import annotations

from pathlib import Path

from ..harness.runner import HarnessRunner
from ..generated_tests import (
    ensure_generated_tests_use_existing_files,
    ensure_generated_tests_use_selected_files,
    generated_test_entry_keys,
    generated_test_filter,
    load_generated_tests_manifest,
    require_generated_tests_manifest,
)
from ..git.diff import ensure_only_target_repo_changed
from ..git.repositories import prepare_target_repo_from_remote, prepare_worktree_dependencies
from ..git.worktree import create_worktree
from ..interface_coverage import require_interface_coverage_artifacts
from ..knowledge.injection import build_injection
from ..knowledge.promote import promote_confirmed_failures
from ..prompts import continue_test_generation_prompt, skip_known_failure_prompt, test_generation_prompt


AGENT_NOTES_PATH = ".gme-agent"


def run_test_generation_job(
    ctx,
    job_id: str,
    module: str,
    api_name: str,
    target_repo: str,
    selection: dict | None = None,
) -> None:
    emit = ctx._job_emit(job_id)
    try:
        with ctx._generation_prepare_lock:
            ctx.db.update_job(job_id, status="creating_worktree")
            worktree = create_worktree(ctx.config, job_id, f"testgen-{module}", emit)
            ctx.db.update_job(
                job_id,
                branch=worktree.branch,
                worktree_path=str(worktree.path),
                metadata=ctx._merge_metadata(job_id, {"superproject_branch": worktree.branch}),
            )
            prepared_paths = prepare_worktree_dependencies(
                ctx.config,
                worktree.path,
                module,
                target_repo,
                emit,
            )
            target = prepare_target_repo_from_remote(
                ctx.config,
                worktree.path,
                target_repo,
                worktree.branch,
                ctx.config.test_base_branch,
                emit,
            )
            ctx.db.update_job(
                job_id,
                branch=target.branch,
                worktree_path=str(worktree.path),
                metadata=ctx._merge_metadata(
                    job_id,
                    {
                        **ctx._target_metadata(worktree.branch, target),
                        "prepared_paths": prepared_paths,
                    },
                ),
            )

        artifact_dir = ctx._artifact_dir(job_id)
        selected_interfaces = list((selection or {}).get("interfaces") or [])
        injection = build_injection(
            ctx.config,
            ctx.db,
            module=module,
            api_names=_knowledge_api_names(api_name, selected_interfaces),
            interface_ids=[str(item.get("id") or "") for item in selected_interfaces],
            symbols=[str(item.get("unique_symbol") or "") for item in selected_interfaces],
            on_event=emit,
        )
        if injection.block:
            (artifact_dir / "knowledge_context.md").write_text(injection.artifact_text, encoding="utf-8")
        if injection.metadata.get("enabled"):
            ctx.db.update_job(job_id, metadata=ctx._merge_metadata(job_id, {"knowledge": injection.metadata}))
        prompt = test_generation_prompt(
            module,
            api_name,
            target.rel_path,
            _build_validation_guidance(ctx, worktree.path, artifact_dir),
            selected_interfaces=selected_interfaces,
            knowledge_block=injection.block,
        )
        (artifact_dir / "test_generation_prompt.md").write_text(prompt, encoding="utf-8")
        emit("info", f"Wrote prompt artifact: {artifact_dir / 'test_generation_prompt.md'}")

        ctx.db.update_job(job_id, status="running_agent")
        runner = HarnessRunner(ctx.config, emit)
        result = None
        if ctx.config.agent_enabled:
            result = runner.run(
                prompt,
                worktree.path,
                skill_names=ctx._test_skill_names(),
                on_session_started=lambda session_id: ctx.db.update_job(
                    job_id,
                    harness_session_id=session_id,
                ),
            )
            (artifact_dir / "agent_result.txt").write_text(result.final_response, encoding="utf-8")
            if result.session_id:
                ctx.db.update_job(job_id, harness_session_id=result.session_id)
        else:
            emit("warn", "Agent execution is disabled; only prompt and worktree were generated.")
        allowed_paths = [*prepared_paths, AGENT_NOTES_PATH]
        ensure_only_target_repo_changed(worktree.path, target.rel_path, allowed_support_paths=allowed_paths)
        generated_metadata = _generated_manifest_metadata(
            worktree.path,
            target.rel_path,
            require=ctx.config.agent_enabled,
            selected_target_files=(selection or {}).get("target_files") or [],
            selected_interfaces=(selection or {}).get("interfaces") or [],
        )
        if generated_metadata:
            ctx.db.update_job(job_id, metadata=ctx._merge_metadata(job_id, generated_metadata))

        if ctx.config.auto_run_build:
            ctx._run_configure_and_build(job_id, worktree.path)

        failures = []
        test_output = ""
        if ctx.config.auto_run_tests:
            gtest_filter = generated_metadata.get("generated_gtest_filter") or ""
            if gtest_filter:
                test_output = ctx._run_tests(job_id, worktree.path, gtest_filter)
                failures = ctx._record_failures(job_id, test_output, gtest_filter, artifact_dir=artifact_dir)
            else:
                emit("info", "Generated test manifest is empty; skipping automatic test execution.")
            if failures:
                emit("warn", f"Recorded {len(failures)} failing tests.")
                _promote_recorded_failures(ctx, job_id, worktree.path, target.rel_path, failures)
                if ctx.config.auto_apply_skips:
                    skip_prompt = skip_known_failure_prompt(
                        test_output,
                        failures,
                        target.rel_path,
                    )
                    (artifact_dir / "skip_prompt.md").write_text(skip_prompt, encoding="utf-8")
                    ctx.db.update_job(job_id, status="applying_skips")
                    skip_result = runner.run(
                        skip_prompt,
                        worktree.path,
                        result.session_id if result else None,
                        skill_names=ctx._test_skill_names(),
                        on_session_started=lambda session_id: ctx.db.update_job(
                            job_id,
                            harness_session_id=session_id,
                        ),
                    )
                    (artifact_dir / "agent_skip_result.txt").write_text(skip_result.final_response, encoding="utf-8")
                    ensure_only_target_repo_changed(worktree.path, target.rel_path, allowed_support_paths=allowed_paths)
                    if ctx.config.auto_run_build:
                        ctx._run_configure_and_build(job_id, worktree.path)
                    if ctx.config.auto_rerun_after_skip:
                        rerun_output = ctx._run_tests(
                            job_id,
                            worktree.path,
                            "*",
                            artifact_name="gtest_output_after_skip.txt",
                        )
                        ctx._record_failures(
                            job_id,
                            rerun_output,
                            "*",
                            artifact_dir=artifact_dir,
                        )
                else:
                    (artifact_dir / "skip_prompt.md").write_text(
                        skip_known_failure_prompt(
                            test_output,
                            failures,
                            target.rel_path,
                        ),
                        encoding="utf-8",
                    )
                    emit("warn", "auto_apply_skips is disabled; review skip_prompt.md manually.")

        ctx._write_job_artifacts(job_id, worktree.path, artifact_dir)
        ctx.db.update_job(job_id, status="needs_review", metadata=ctx._merge_metadata(job_id, {"artifact_dir": str(artifact_dir), **generated_metadata}))
        emit("info", "Job finished and is ready for review.")
        if ctx.config.auto_create_pr:
            ctx._run_pr_job(job_id)
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        emit("error", str(exc))


def run_test_extension_job(
    ctx,
    job_id: str,
    api_name: str,
    selection: dict | None = None,
    *,
    retry_context: str = "",
    validate_current_manifest: bool = False,
) -> None:
    emit = ctx._job_emit(job_id)
    try:
        job = ctx.db.get_job(job_id)
        worktree_path = job.get("worktree_path")
        if not worktree_path:
            raise RuntimeError("Selected job has no worktree path. Create a test task first.")

        worktree = Path(worktree_path)
        if not worktree.exists():
            raise RuntimeError(f"Selected job worktree does not exist: {worktree}")

        module = str(job.get("module") or "")
        target_repo = ctx._job_target_repo(job)
        artifact_dir = ctx._artifact_dir(job_id)
        previous_manifest = load_generated_tests_manifest(worktree, target_repo)
        selected_interfaces = list((selection or {}).get("interfaces") or [])
        resolved_api_name = api_name or str(job.get("api_name") or "")
        injection = build_injection(
            ctx.config,
            ctx.db,
            module=module,
            api_names=_knowledge_api_names(resolved_api_name, selected_interfaces),
            interface_ids=[str(item.get("id") or "") for item in selected_interfaces],
            symbols=[str(item.get("unique_symbol") or "") for item in selected_interfaces],
            on_event=emit,
        )
        if injection.block:
            (artifact_dir / "knowledge_context.md").write_text(injection.artifact_text, encoding="utf-8")
        if injection.metadata.get("enabled"):
            ctx.db.update_job(job_id, metadata=ctx._merge_metadata(job_id, {"knowledge": injection.metadata}))
        prompt = continue_test_generation_prompt(
            module,
            resolved_api_name,
            target_repo,
            _build_validation_guidance(ctx, worktree, artifact_dir),
            selected_interfaces=selected_interfaces,
            knowledge_block=injection.block,
        )
        if retry_context:
            prompt = f"{retry_context.strip()}\n\n{prompt}"
        prompt_path = artifact_dir / "test_generation_extend_prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        emit("info", f"Wrote extension prompt artifact: {prompt_path}")

        ctx.db.update_job(
            job_id,
            status="running_agent",
            api_name=api_name or job.get("api_name") or "",
            error="",
            metadata=ctx._merge_metadata(job_id, {"last_extension_prompt": api_name}),
        )
        runner = HarnessRunner(ctx.config, emit)
        result = runner.run(
            prompt,
            worktree,
            job.get("harness_session_id"),
            skill_names=ctx._test_skill_names(),
            on_session_started=lambda session_id: ctx.db.update_job(
                job_id,
                harness_session_id=session_id,
            ),
        )
        (artifact_dir / "agent_result.txt").write_text(result.final_response, encoding="utf-8")
        (artifact_dir / "agent_extend_result.txt").write_text(result.final_response, encoding="utf-8")
        if result.session_id:
            ctx.db.update_job(job_id, harness_session_id=result.session_id)

        allowed_paths = list((job.get("metadata") or {}).get("prepared_paths") or [])
        if AGENT_NOTES_PATH not in allowed_paths:
            allowed_paths.append(AGENT_NOTES_PATH)
        ensure_only_target_repo_changed(worktree, target_repo, allowed_support_paths=allowed_paths)
        generated_metadata = _generated_manifest_metadata(
            worktree,
            target_repo,
            require=ctx.config.agent_enabled,
            selected_target_files=(selection or {}).get("target_files") or [],
            selected_interfaces=(selection or {}).get("interfaces") or [],
            previous_entries=(
                None
                if validate_current_manifest
                else generated_test_entry_keys(previous_manifest) if selection else None
            ),
        )
        if generated_metadata:
            ctx.db.update_job(job_id, metadata=ctx._merge_metadata(job_id, generated_metadata))

        if ctx.config.auto_run_build:
            ctx._run_configure_and_build(job_id, worktree)

        if ctx.config.auto_run_tests:
            gtest_filter = generated_metadata.get("generated_gtest_filter") or ""
            if gtest_filter:
                test_output = ctx._run_tests(job_id, worktree, gtest_filter)
                failures = ctx._record_failures(job_id, test_output, gtest_filter, artifact_dir=artifact_dir)
                _promote_recorded_failures(ctx, job_id, worktree, target_repo, failures)
            else:
                emit("info", "Generated test manifest is empty; skipping automatic test execution.")

        ctx._write_job_artifacts(job_id, worktree, artifact_dir)
        ctx.db.update_job(
            job_id,
            status="needs_review",
            error="",
            metadata=ctx._merge_metadata(
                job_id,
                {"artifact_dir": str(artifact_dir), **generated_metadata},
            ),
        )
        emit("info", "Test extension finished and is ready for review.")
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        emit("error", str(exc))


def _generated_manifest_metadata(
    worktree: Path,
    target_repo: str,
    *,
    require: bool,
    selected_target_files: list[str] | None = None,
    selected_interfaces: list[dict] | None = None,
    previous_entries: set[tuple[str, str, str]] | None = None,
) -> dict:
    if not require:
        return {}
    structured_interfaces = list(selected_interfaces or [])
    manifest = require_generated_tests_manifest(
        worktree,
        target_repo,
        allow_empty=bool(structured_interfaces),
    )
    ensure_generated_tests_use_existing_files(worktree, target_repo, manifest["files"])
    if selected_target_files:
        ensure_generated_tests_use_selected_files(
            manifest,
            target_repo,
            selected_target_files,
            previous_entries=previous_entries,
        )
    metadata = {
        "generated_tests": manifest["tests"],
        "generated_test_files": manifest["files"],
        "generated_gtest_filter": generated_test_filter(manifest),
    }
    if structured_interfaces:
        metadata["interface_coverage"] = require_interface_coverage_artifacts(
            worktree,
            target_repo,
            structured_interfaces,
            manifest,
            previous_entries=previous_entries,
        )
    return metadata


def _build_validation_guidance(ctx, worktree: Path, artifact_dir: Path) -> str:
    build_dir = worktree / "build" / "vscode"
    memory_audit_build_dir = worktree / "build" / "memory-audit"
    generated_filter_placeholder = "<exact-generated-filter-from-.gme-agent/generated_tests.json>"
    try:
        mapping = ctx._command_mapping(worktree, build_dir, generated_filter_placeholder, artifact_dir=artifact_dir)
        configure_command = ctx.config.configure_command.format(**mapping)
        build_command = ctx.config.build_command.format(**mapping)
        test_command = ctx._agent_test_command(mapping, build_dir)
    except Exception as exc:
        return f"""构建验证命令：
- GME Test Agent 无法预先渲染配置的构建命令：{exc}
- 请使用设置页中的命令模板及以下占位符：`worktree`、`build_dir`、`test_module_name`、`develop_module_option`、`test_module_option`、`gtest_filter`、`test_executable`、`artifact_dir`、`gtest_xml_path`。
- 构建目录：`{build_dir}`"""
    return f"""GME Test Agent 设置提供的构建验证命令：
- 构建目录：`{build_dir}`
- 配置：
  `{configure_command}`
- 构建：
  `{build_command}`
- 构建成功后，等 `.gme-agent/generated_tests.json` 提供准确 filter，必须运行本次所有生成测试；仅构建通过不算完成：
  `{test_command}`
- 上面这条测试命令的 XML 输出已指向构建目录内（你的工作区内、可写）。GME Test Agent 会在全部校验结束后用配置里的命令在 artifact 目录再跑一次，那份 `gtest.xml` 才是权威结果；**不要尝试写 artifact 目录**（在你的沙箱工作区之外，只读）。
- 功能测试后，必须在独立 Release 目录进行内存审计：
  `cmake -S "{worktree}" -B "{memory_audit_build_dir}" -G "Visual Studio 17 2022" -A x64 -DBUILD_ALL_MODULE=OFF -DBUILD_DEMO=OFF -DBUILD_BENCHTEST=OFF -DBUILD_TEST=ON -DBUILD_FORMAT=OFF -DTEST_MEMORY_AUDIT=ON {mapping["develop_module_option"]} {mapping["test_module_option"]}`
  `cmake --build "{memory_audit_build_dir}" --config Release --target tests -- /p:TrackFileAccess=false`
- 必须把准确 filter 展开为单条测试，在独立工作目录逐条运行 `"{memory_audit_build_dir / "Release" / "tests.exe"}" --gtest_filter=<exact-single-test>`，并且无论 GTest 退出码是否为零都继续读取对应工作目录中的 `gme_mmgr.1.log`。只有 `Leaks: 0` 且 `Bad delete pointers: 0` 才算内存审计通过。
- 这些命令是本任务的权威命令。若命令因生成测试代码失败，必须在最终回复前修复或删除对应生成测试。"""


def _knowledge_api_names(api_name: str, selected_interfaces: list[dict]) -> list[str]:
    names = [str(api_name or "")]
    names.extend(str(item.get("api") or "") for item in selected_interfaces)
    names.extend(str(item.get("unique_symbol") or "") for item in selected_interfaces)
    return [name for name in dict.fromkeys(names) if name.strip()]


def _promote_recorded_failures(ctx, job_id: str, worktree: Path, target_repo: str, failures: list[dict]) -> None:
    settings = ctx.config.knowledge
    if not (settings.enabled and settings.closed_loop.enabled and failures):
        return
    emit = ctx._job_emit(job_id)
    try:
        manifest = load_generated_tests_manifest(worktree, target_repo)
    except Exception as exc:
        _safe_warn(emit, f"knowledge/promotion-skipped manifest unreadable: {exc}")
        return
    try:
        promote_confirmed_failures(
            ctx.db,
            failures=failures,
            manifest=manifest,
            min_stable_runs=settings.closed_loop.min_stable_runs,
            on_event=emit,
        )
    except Exception as exc:
        _safe_warn(emit, f"knowledge/promotion-failed {type(exc).__name__}: {exc}")


def _safe_warn(emit, message: str) -> None:
    """Warn without letting a broken job-event writer fail the task.

    The wrapper exists so a promotion problem never fails the task; an emitter that
    raises inside the `except` branch would undo exactly that, and the flow's caller
    would mark a finished task failed.
    """

    try:
        emit("warn", message)
    except Exception:
        pass
