from __future__ import annotations

from pathlib import Path
import re

from ..git.diff import commit_all, create_pr, git_diff, push_branch
from ..git.repositories import (
    checkout_dependency_remote_base,
    cleanup_worktree_dependencies,
    prepare_target_repo_from_remote,
    prepare_worktree_dependencies,
)
from ..git.worktree import create_remote_worktree, remove_worktree, run_git
from .bug_fix_flow import (
    _copy_failure_test_files,
    _ensure_fix_scope,
    _require_full_tests_pass,
    _require_gtest_status,
    _require_memory_audit_pass,
    _run_fix_format_check,
    _selected_tests_from_context,
)
from .memory_audit_flow import run_selected_memory_audit


def run_bug_fix_pr_job(ctx, job_id: str) -> None:
    emit = ctx._job_emit(job_id)
    job = ctx.db.get_job(job_id)
    metadata = dict(job.get("metadata") or {})
    source_path = Path(str(job.get("worktree_path") or ""))
    target_repo = ctx._job_target_repo(job)
    if not source_path.exists() or not metadata.get("fix_validated"):
        ctx.db.update_job(job_id, status="failed", error="Only a validated repair task can create a source PR.")
        return

    source_target = ctx._target_repo_path(source_path, target_repo)
    source_patch = git_diff(source_target)
    if not source_patch.strip():
        ctx.db.update_job(job_id, status="failed", error="The repair task has no module source patch to submit.")
        return

    verification = None
    prepared_paths: list[str] = []
    try:
        ctx.db.update_job(job_id, status="creating_pr", error=None)
        verification = create_remote_worktree(
            ctx.config,
            job_id,
            f"fix-pr-{job.get('module') or 'module'}",
            emit,
        )
        test_repo = str(metadata.get("test_target_repo") or ctx._test_target_repo())
        prepared_paths = prepare_worktree_dependencies(
            ctx.config,
            verification.path,
            str(job.get("module") or ""),
            test_repo,
            emit,
        )
        checkout_dependency_remote_base(
            ctx.config,
            verification.path,
            test_repo,
            ctx.config.test_base_branch,
            emit,
        )
        pr_branch = f"{job.get('branch') or verification.branch}-pr"
        target = prepare_target_repo_from_remote(
            ctx.config,
            verification.path,
            target_repo,
            pr_branch,
            ctx.config.module_base_branch,
            emit,
        )

        _copy_failure_test_files(verification.path, metadata, emit)
        test_repo_path = verification.path / test_repo
        test_baseline_diff = git_diff(test_repo_path)
        gtest_filter = str(metadata.get("gtest_filter") or "")
        selected_tests = _selected_tests_from_context(metadata)
        if not selected_tests:
            raise RuntimeError("The repair task does not contain any selected tests to validate.")
        artifact_dir = ctx._artifact_dir(job_id)

        ctx._run_configure_and_build(job_id, verification.path)
        before = ctx._run_tests(
            job_id,
            verification.path,
            gtest_filter,
            artifact_name="gtest_pr_reproduce_before_patch.txt",
        )
        for item in selected_tests:
            full_name = f"{item['test_suite']}.{item['test_name']}"
            _require_gtest_status(
                before,
                full_name,
                "FAILED",
                "The selected failure no longer reproduces on latest module develop; do not create a duplicate PR.",
            )

        patch_path = artifact_dir / "fix_source.patch"
        patch_path.write_text(source_patch, encoding="utf-8")
        run_git(["apply", "--whitespace=nowarn", str(patch_path)], target.path, emit)
        changed_files = _ensure_fix_scope(
            verification.path,
            target.rel_path,
            test_repo,
            test_baseline_diff,
            prepared_paths,
        )
        format_summary = _run_fix_format_check(
            verification.path,
            target.path,
            changed_files,
            artifact_dir,
            emit,
        )
        ctx._run_configure_and_build(job_id, verification.path)
        after = ctx._run_tests(
            job_id,
            verification.path,
            gtest_filter,
            artifact_name="gtest_pr_verify_after_patch.txt",
        )
        for item in selected_tests:
            full_name = f"{item['test_suite']}.{item['test_name']}"
            _require_gtest_status(after, full_name, "OK", "Every selected test must pass on latest develop.")

        full = ctx._run_tests(job_id, verification.path, "*", artifact_name="gtest_pr_full_after_patch.txt")
        full_count = _require_full_tests_pass(full)
        memory = run_selected_memory_audit(
            ctx,
            job_id,
            verification.path,
            gtest_filter,
            artifact_dir=artifact_dir,
        )
        _require_memory_audit_pass(memory)
        _ensure_fix_scope(verification.path, target.rel_path, test_repo, test_baseline_diff, prepared_paths)

        title = _repair_pr_title(job)
        failure_details = _repair_failure_details(ctx, metadata, selected_tests)
        commit_all(target.path, title, emit)
        push_branch(ctx.config, target.path, pr_branch, emit)
        pr_url = create_pr(
            ctx.config,
            target.path,
            title,
            _repair_pr_body(
                job,
                selected_tests,
                failure_details,
                format_summary=format_summary,
                full_test_count=full_count,
                memory_summary=memory,
            ),
            emit,
            base_branch=ctx.config.module_base_branch,
        )
        metadata.update(
            {
                "pr_url": pr_url,
                "fix_target_repo": target.rel_path,
                "target_repo": target.rel_path,
                "pr_branch": pr_branch,
                "pr_base_branch": ctx.config.module_base_branch,
                "pr_validation": {
                    "format": format_summary,
                    "selected_tests": len(selected_tests),
                    "module_full_tests": full_count,
                    "memory_audit": memory,
                },
            }
        )
        ctx.db.update_job(job_id, status="pr_created", metadata=metadata, error=None)
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        emit("error", str(exc))
    finally:
        if verification is not None:
            cleanup_worktree_dependencies(ctx.config, verification.path, prepared_paths, emit)
            try:
                remove_worktree(ctx.config, verification.path, emit)
                run_git(["branch", "-D", verification.branch], ctx.config.gme_repo_path, emit)
            except Exception as exc:
                emit("warn", f"Could not fully clean repair PR worktree: {exc}")


def _repair_pr_title(job: dict) -> str:
    module = _repair_source_module(job)
    api_name = str(job.get("api_name") or "接口行为")
    return f"bugfix({module}): 修复 {api_name}"


def _repair_source_module(job: dict) -> str:
    metadata = job.get("metadata") or {}
    target_repo = str(metadata.get("fix_target_repo") or metadata.get("target_repo") or "")
    normalized = target_repo.replace("\\", "/").strip("/")
    if normalized.startswith("module/") and len(normalized.split("/")) > 1:
        return normalized.split("/", 1)[1]
    return str(job.get("module") or "module")


def _repair_failure_details(ctx, metadata: dict, selected_tests: list[dict]) -> list[dict[str, str]]:
    fallback_ids = [str(item) for item in metadata.get("failure_ids") or [] if item]
    if not fallback_ids and metadata.get("failure_id"):
        fallback_ids = [str(metadata["failure_id"])]
    details = []
    for index, item in enumerate(selected_tests):
        failure_id = str(item.get("failure_id") or (fallback_ids[index] if index < len(fallback_ids) else ""))
        reason = str(item.get("reason") or "")
        if not reason and failure_id:
            try:
                reason = str(ctx.db.get_failure(failure_id).get("reason") or "")
            except KeyError:
                pass
        details.append(
            {
                "name": f"{item['test_suite']}.{item['test_name']}",
                "reason": _clean_failure_reason(reason),
            }
        )
    return details


def _clean_failure_reason(reason: str) -> str:
    lines = [line.rstrip() for line in str(reason or "").strip().splitlines()]
    if lines and re.match(r"^(?:[A-Za-z]:[\\/]|/).+?:\d+(?::\d+)?$", lines[0].strip()):
        lines.pop(0)
    cleaned = "\n".join(lines).strip()
    return cleaned or "修复前测试失败，但任务未记录详细断言输出。"


def _repair_pr_body(
    job: dict,
    selected_tests: list[dict],
    failure_details: list[dict[str, str]],
    *,
    format_summary: dict | None = None,
    full_test_count: int = 0,
    memory_summary: dict | None = None,
) -> str:
    detail_sections = [
        f"### `{detail['name']}`\n\n```text\n{detail['reason']}\n```"
        for detail in failure_details
    ]
    if not detail_sections:
        detail_sections = [f"- `{item['test_suite']}.{item['test_name']}`" for item in selected_tests]
    format_files = ", ".join(f"`{path}`" for path in (format_summary or {}).get("files") or []) or "修改文件"
    memory = memory_summary or {}
    selected_count = len(selected_tests)
    memory_passed = int(memory.get("passed") or 0)
    return f"""该 PR 由 GME 修复 Agent 自动生成。

## 问题说明

修复 GME `{_repair_source_module(job)}` 模块中 `{job.get('api_name') or '接口'}` 的行为差异。修复前，GME 实现未满足测试定义的 ACIS 对照结果或接口数值契约。

## 修复前失败测试

{chr(10).join(detail_sections)}

## 验证结果

- {selected_count} 个目标测试全部通过
- 模块全量测试通过 {full_test_count} 项
- 内存审计通过 {memory_passed}/{int(memory.get('total') or selected_count)} 项，未发现泄漏
- clang-format 检查通过：{format_files}
"""
