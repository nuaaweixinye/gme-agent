from __future__ import annotations

from pathlib import Path
from typing import Any
import re
import shutil
import subprocess

from ..harness.runner import HarnessRunner
from ..execution.clang_format import describe_version_mismatch, resolve_clang_format
from ..generated_tests import load_generated_tests_manifest
from ..git.diff import git_diff, git_status
from ..git.repositories import (
    checkout_dependency_remote_base,
    prepare_worktree_dependencies,
    repair_candidate_repos,
)
from ..git.worktree import normalize_repo_path, create_worktree
from ..prompts import bug_fix_prompt
from .memory_audit_flow import run_selected_memory_audit
from .skip_pr_flow import (
    Utf8Source,
    _extract_selected_test_blocks,
    _insert_selected_test_blocks,
    _normalize_newlines,
    _read_utf8_source,
    _test_blocks,
    _write_utf8_source,
)


def validate_fix_failure(ctx, failure: dict[str, Any]) -> dict[str, Any]:
    if not failure.get("job_id"):
        raise RuntimeError("The selected failure is not associated with a test-generation job.")

    source_job = ctx.db.get_job(str(failure["job_id"]))
    if source_job.get("type") != "test_generation":
        raise RuntimeError("Only failures from test-generation jobs can be used to create a fix job.")
    if not source_job.get("worktree_path"):
        raise RuntimeError("The source test-generation job has no worktree path.")

    submitted_failure = _is_submitted_failure(failure, source_job)
    if failure.get("status") not in {"open", "fix_failed"} and not (
        failure.get("status") == "resolved" and submitted_failure
    ):
        raise RuntimeError("Only open, retryable, or submitted known failures can be used to create a fix job.")

    module = str(source_job.get("module") or "").strip()
    if not module:
        raise RuntimeError("The source test-generation job has no module.")

    source_worktree = Path(str(source_job["worktree_path"]))
    if not source_worktree.exists():
        raise RuntimeError(f"Source test-generation worktree does not exist: {source_worktree}")

    source_target_repo = _source_test_target_repo(ctx, source_job)
    manifest = load_generated_tests_manifest(source_worktree, source_target_repo)
    selected = _selected_manifest_test(manifest, failure)
    if selected is None:
        raise RuntimeError("Only failures listed in .gme-agent/generated_tests.json can be used for repair.")

    source_test_file = source_worktree / source_target_repo / selected["file"]
    if not source_test_file.exists():
        raise RuntimeError(f"Generated failure test file does not exist: {source_test_file}")
    block = _selected_test_block(source_test_file, selected["suite"], selected["name"])
    if "GTEST_SKIP" in block:
        raise RuntimeError("The selected failure test contains GTEST_SKIP() and cannot reproduce the failure.")

    return {
        "source_job_id": source_job["id"],
        "source_worktree_path": str(source_worktree),
        "test_target_repo": source_target_repo,
        "generated_test_file": selected["file"],
        "test_suite": selected["suite"],
        "test_name": selected["name"],
        "gtest_filter": f"{selected['suite']}.{selected['name']}",
        "submitted_known_failure": submitted_failure,
        "api_name": str(selected.get("api") or "").strip(),
    }


def validate_fix_failures(ctx, failures: list[dict[str, Any]]) -> dict[str, Any]:
    if not failures:
        raise RuntimeError("Select at least one failure to create a repair task.")
    contexts = [validate_fix_failure(ctx, failure) for failure in failures]
    source_jobs = [ctx.db.get_job(str(failure["job_id"])) for failure in failures]
    modules = {str(job.get("module") or "").strip() for job in source_jobs}
    test_repos = {str(item["test_target_repo"]) for item in contexts}
    api_names = {str(item.get("api_name") or "").strip() for item in contexts if item.get("api_name")}
    if len(modules) != 1 or len(test_repos) != 1:
        raise RuntimeError("A grouped repair must use failures from the same module and test repository.")
    if len(api_names) > 1:
        raise RuntimeError("A grouped repair must use failures from the same API.")

    selected_tests = []
    seen = set()
    for failure, context in zip(failures, contexts):
        key = (context["test_suite"], context["test_name"])
        if key in seen:
            continue
        seen.add(key)
        selected_tests.append(
            {
                **context,
                "failure_id": str(failure["id"]),
                "reason": str(failure.get("reason") or ""),
            }
        )
    return {
        "failure_ids": [item["failure_id"] for item in selected_tests],
        "primary_failure_id": selected_tests[0]["failure_id"],
        "source_job_ids": list(dict.fromkeys(str(item["source_job_id"]) for item in contexts)),
        "api_name": next(iter(api_names), ""),
        "test_target_repo": next(iter(test_repos)),
        "selected_tests": selected_tests,
        "gtest_filter": ":".join(f"{item['test_suite']}.{item['test_name']}" for item in selected_tests),
    }


def run_fix_job(ctx, job_id: str, failures: list[dict[str, Any]] | dict[str, Any]) -> None:
    emit = ctx._job_emit(job_id)
    selected_failures = failures if isinstance(failures, list) else [failures]
    try:
        fix_context = validate_fix_failures(ctx, selected_failures)
        ctx.db.update_job(job_id, status="creating_worktree")
        module = str(ctx.db.get_job(job_id).get("module") or "")
        worktree = create_worktree(ctx.config, job_id, f"fix-{module}-{fix_context['primary_failure_id']}", emit)
        job = ctx.db.get_job(job_id)
        test_target_repo = fix_context["test_target_repo"]
        prepared_paths = prepare_worktree_dependencies(ctx.config, worktree.path, module, test_target_repo, emit)
        metadata = job.get("metadata") or {}
        configured_candidates = metadata.get("fix_candidate_repos") or repair_candidate_repos(ctx.config, module)
        candidate_repos = [
            normalize_repo_path(str(path))
            for path in configured_candidates
            if normalize_repo_path(str(path)) in prepared_paths
        ]
        candidate_repos = list(dict.fromkeys(candidate_repos))
        if not candidate_repos:
            raise RuntimeError("No repair candidate source repositories are available in the worktree.")
        checkout_dependency_remote_base(
            ctx.config,
            worktree.path,
            test_target_repo,
            ctx.config.test_base_branch,
            emit,
        )
        ctx.db.update_job(
            job_id,
            branch=worktree.branch,
            worktree_path=str(worktree.path),
            metadata=ctx._merge_metadata(
                job_id,
                {
                    **fix_context,
                    "superproject_branch": worktree.branch,
                    "fix_candidate_repos": candidate_repos,
                    "prepared_paths": prepared_paths,
                },
            ),
        )

        artifact_dir = ctx._artifact_dir(job_id)
        copied_tests = _copy_failure_test_files(worktree.path, fix_context, emit)
        test_repo_path = worktree.path / test_target_repo
        test_repo_baseline_diff = git_diff(test_repo_path)

        gtest_filter = fix_context["gtest_filter"]
        ctx._run_configure_and_build(job_id, worktree.path)
        before_output = ctx._run_tests(job_id, worktree.path, gtest_filter, artifact_name="gtest_reproduce_before_fix.txt")
        for item in fix_context["selected_tests"]:
            full_name = f"{item['test_suite']}.{item['test_name']}"
            _require_gtest_status(before_output, full_name, "FAILED", "Every selected test must fail before repair.")
        validation_commands = _validation_commands(ctx, job_id, worktree.path, gtest_filter, artifact_dir)

        prompt = bug_fix_prompt(
            selected_failures,
            candidate_repos=candidate_repos,
            test_repo=test_target_repo,
            test_file=", ".join(copied_tests),
            gtest_filter=gtest_filter,
            before_output=before_output,
            configure_command=validation_commands["configure"],
            build_command=validation_commands["build"],
            test_command=validation_commands["test"],
        )
        (artifact_dir / "bug_fix_prompt.md").write_text(prompt, encoding="utf-8")
        emit("info", f"Wrote prompt artifact: {artifact_dir / 'bug_fix_prompt.md'}")

        ctx.db.update_job(job_id, status="running_agent")
        runner = HarnessRunner(ctx.config, emit)
        result = runner.run(
            prompt,
            worktree.path,
            job.get("harness_session_id"),
            skill_names=ctx._bug_fix_skill_names(),
            on_session_started=lambda session_id: ctx.db.update_job(
                job_id,
                harness_session_id=session_id,
            ),
        )
        (artifact_dir / "agent_result.txt").write_text(result.final_response, encoding="utf-8")
        if result.session_id:
            ctx.db.update_job(job_id, harness_session_id=result.session_id)

        target_repo, changed_module_files = _detect_fix_target(
            worktree.path,
            candidate_repos,
            test_target_repo,
            test_repo_baseline_diff,
            prepared_paths,
        )
        target_path = worktree.path / target_repo
        ctx.db.update_job(
            job_id,
            metadata=ctx._merge_metadata(
                job_id,
                {
                    "fix_target_repo": target_repo,
                    "target_repo": target_repo,
                    "target_repo_path": str(target_path),
                    "target_branch": worktree.branch,
                    "target_base_branch": ctx.config.module_base_branch,
                },
            ),
        )
        emit("info", f"Detected repair source repository from the agent diff: {target_repo}")
        ctx.db.update_job(job_id, status="checking_format")
        format_summary = _run_fix_format_check(
            worktree.path,
            target_path,
            changed_module_files,
            artifact_dir,
            emit,
            config=ctx.config,
        )
        ctx.db.update_job(
            job_id,
            metadata=ctx._merge_metadata(job_id, {"fix_validation": {"format": format_summary}}),
        )
        ctx._run_configure_and_build(job_id, worktree.path)
        after_output = ctx._run_tests(job_id, worktree.path, gtest_filter, artifact_name="gtest_verify_after_fix.txt")
        for item in fix_context["selected_tests"]:
            full_name = f"{item['test_suite']}.{item['test_name']}"
            _require_gtest_status(after_output, full_name, "OK", "Every selected test must pass after repair.")
        ctx.db.update_job(
            job_id,
            metadata=ctx._merge_metadata(
                job_id,
                {
                    "fix_validation": {
                        "format": format_summary,
                        "target_test": {"filter": gtest_filter, "status": "passed"},
                    }
                },
            ),
        )
        full_output = ctx._run_tests(job_id, worktree.path, "*", artifact_name="gtest_full_after_fix.txt")
        full_test_count = _require_full_tests_pass(full_output)
        ctx.db.update_job(
            job_id,
            metadata=ctx._merge_metadata(
                job_id,
                {
                    "fix_validation": {
                        "format": format_summary,
                        "target_test": {"filter": gtest_filter, "status": "passed"},
                        "module_full_tests": {"filter": "*", "passed": full_test_count},
                    }
                },
            ),
        )

        ctx.db.update_job(job_id, status="running_memory_audit")
        memory_summary = run_selected_memory_audit(
            ctx,
            job_id,
            worktree.path,
            gtest_filter,
            artifact_dir=artifact_dir,
        )
        _require_memory_audit_pass(memory_summary)

        ctx._write_job_artifacts(job_id, worktree.path, artifact_dir)
        ctx.db.update_job(
            job_id,
            status="needs_review",
            metadata=ctx._merge_metadata(
                job_id,
                {
                    "artifact_dir": str(artifact_dir),
                    "fix_validated": True,
                    "fix_validation": {
                        "format": format_summary,
                        "target_test": {"filter": gtest_filter, "status": "passed"},
                        "module_full_tests": {"filter": "*", "passed": full_test_count},
                        "memory_audit": memory_summary,
                    },
                },
            ),
        )
        for failure in selected_failures:
            ctx.db.update_failure(str(failure["id"]), status="fix_ready")
        emit("info", "Bug fix job finished and is ready for review.")
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        for failure in selected_failures:
            ctx.db.update_failure(str(failure["id"]), status="fix_failed")
        emit("error", str(exc))


def _source_test_target_repo(ctx, source_job: dict[str, Any]) -> str:
    metadata = source_job.get("metadata") or {}
    return normalize_repo_path(str(metadata.get("target_repo") or ctx._test_target_repo()))


def _is_submitted_failure(failure: dict[str, Any], source_job: dict[str, Any]) -> bool:
    metadata = source_job.get("metadata") or {}
    failure_ids = {str(item) for item in metadata.get("skip_failure_ids") or []}
    submitted_names = {str(item) for item in metadata.get("submitted_test_names") or []}
    full_name = ".".join(
        part
        for part in [str(failure.get("test_suite") or ""), str(failure.get("test_name") or "")]
        if part
    )
    return str(failure.get("id") or "") in failure_ids or full_name in submitted_names


def _selected_manifest_test(manifest: dict[str, Any], failure: dict[str, Any]) -> dict[str, str] | None:
    suite = str(failure.get("test_suite") or "")
    name = str(failure.get("test_name") or "")
    for item in manifest.get("tests") or []:
        if str(item.get("suite") or "") == suite and str(item.get("name") or "") == name:
            return {
                "file": normalize_repo_path(str(item.get("file") or "")),
                "suite": suite,
                "name": name,
                "api": str(item.get("api") or "").strip(),
            }
    return None


def _selected_test_block(file_path: Path, suite: str, name: str) -> str:
    text = file_path.read_text(encoding="utf-8", errors="replace")
    rel_path = normalize_repo_path(file_path.name)
    for start, end, found_suite, found_name in _test_blocks(text, rel_path):
        if found_suite == suite and found_name == name:
            return text[start:end]
    raise RuntimeError(f"Could not find generated failure test {suite}.{name} in {file_path}")


def _copy_failure_test_file(worktree: Path, fix_context: dict[str, Any], emit) -> str:
    test_target_repo = normalize_repo_path(str(fix_context["test_target_repo"]))
    rel_file = normalize_repo_path(str(fix_context["generated_test_file"]))
    source_repo = Path(str(fix_context["source_worktree_path"])) / test_target_repo
    target_repo = worktree / test_target_repo
    selected = [
        {
            "file": rel_file,
            "suite": str(fix_context["test_suite"]),
            "name": str(fix_context["test_name"]),
        }
    ]
    selected_blocks = _extract_selected_test_blocks(source_repo, selected)
    target_file = target_repo / rel_file
    if not target_file.exists():
        raise RuntimeError(f"Base repair worktree is missing the target test file: {target_file}")

    target_source = _read_utf8_source(target_file)
    target_blocks = _test_blocks(target_source.text, rel_file)
    selected_key = (selected_blocks[0].suite, selected_blocks[0].name)
    target_match = next(
        (
            (start, end)
            for start, end, suite, name in target_blocks
            if (suite, name) == selected_key
        ),
        None,
    )
    if target_match is None:
        _insert_selected_test_blocks(target_repo, selected_blocks, emit)
    else:
        start, end = target_match
        replacement = _normalize_newlines(selected_blocks[0].text.strip(), target_source.newline)
        updated = target_source.text[:start] + replacement + target_source.text[end:]
        _write_utf8_source(
            target_file,
            Utf8Source(updated, target_source.newline, target_source.has_bom),
        )
        emit("info", f"Replaced {selected_key[0]}.{selected_key[1]} in current {rel_file}.")

    emit("info", f"Installed generated failure test into repair worktree: {test_target_repo}/{rel_file}")
    return f"{test_target_repo}/{rel_file}"


def _copy_failure_test_files(worktree: Path, fix_context: dict[str, Any], emit) -> list[str]:
    installed = []
    for item in _selected_tests_from_context(fix_context):
        path = _copy_failure_test_file(worktree, item, emit)
        if path not in installed:
            installed.append(path)
    return installed


def _selected_tests_from_context(fix_context: dict[str, Any]) -> list[dict[str, Any]]:
    selected = list(fix_context.get("selected_tests") or [])
    if selected:
        return selected
    required = ("source_worktree_path", "test_target_repo", "generated_test_file", "test_suite", "test_name")
    if all(fix_context.get(key) for key in required):
        return [fix_context]
    return []


def _validation_commands(ctx, job_id: str, worktree: Path, gtest_filter: str, artifact_dir: Path) -> dict[str, str]:
    build_dir = worktree / "build" / "vscode"
    mapping = ctx._command_mapping(worktree, build_dir, gtest_filter, artifact_dir=artifact_dir)
    return {
        "configure": ctx.config.configure_command.format(**mapping),
        "build": ctx.config.build_command.format(**mapping),
        "test": ctx._agent_test_command(mapping, build_dir),
    }


def _require_gtest_status(output: str, full_name: str, expected_status: str, message: str) -> None:
    status = _gtest_status(output, full_name)
    if status == expected_status:
        return
    if status == "SKIPPED":
        raise RuntimeError(f"{message} The selected test was skipped instead: {full_name}")
    if status:
        raise RuntimeError(f"{message} Observed {status} for {full_name}.")
    raise RuntimeError(f"{message} No GTest result line was found for {full_name}.")


def _gtest_status(output: str, full_name: str) -> str:
    for status in ("FAILED", "OK", "SKIPPED"):
        pattern = re.compile(rf"^\[\s*{status}\s*\]\s+{re.escape(full_name)}(?:\s|\(|$)", re.MULTILINE)
        if pattern.search(output or ""):
            return status
    return ""


def _detect_fix_target(
    worktree: Path,
    candidate_repos: list[str],
    test_repo: str,
    test_repo_baseline_diff: str,
    prepared_paths: list[str],
) -> tuple[str, list[str]]:
    test_rel = normalize_repo_path(test_repo)
    test_path = worktree / test_rel
    if git_diff(test_path) != test_repo_baseline_diff:
        raise RuntimeError("The agent changed the reproduced generated test file. Fix jobs may only modify module source code.")

    normalized_candidates = list(dict.fromkeys(normalize_repo_path(path) for path in candidate_repos))
    changed_candidates: list[tuple[str, list[str]]] = []
    unexpected: list[str] = []
    for rel_path in normalized_candidates:
        repo_path = worktree / rel_path
        changes = [
            normalize_repo_path(_status_path(line))
            for line in git_status(repo_path).splitlines()
            if _status_path(line)
        ]
        if changes:
            changed_candidates.append((rel_path, changes))
            unexpected.extend(f"{rel_path}/{path}" for path in changes if _is_include_path(path))

    for rel_path in dict.fromkeys(normalize_repo_path(path) for path in prepared_paths):
        if rel_path in normalized_candidates or rel_path == test_rel:
            continue
        repo_path = worktree / rel_path
        if repo_path.exists():
            nested = git_status(repo_path)
            if nested.strip():
                unexpected.append(f"{rel_path}:\n{nested.rstrip()}")

    prepared = set(normalize_repo_path(path) for path in prepared_paths)
    for line in git_status(worktree).splitlines():
        path = normalize_repo_path(_status_path(line))
        if not path or _is_ignored_worktree_artifact(path):
            continue
        if any(_is_under(path, rel_path) for rel_path in prepared):
            continue
        unexpected.append(line)

    if unexpected:
        raise RuntimeError(
            "Bug-fix jobs may only change implementation files in one repair candidate repository and must not edit include/ or tests. "
            "Unexpected changes:\n" + "\n".join(unexpected)
        )
    if not changed_candidates:
        raise RuntimeError("The agent did not change any repair candidate source files, so no repair can be reviewed.")
    if len(changed_candidates) > 1:
        repos = ", ".join(repo for repo, _changes in changed_candidates)
        raise RuntimeError(f"The agent changed more than one source repository ({repos}); split the repair by repository.")
    return changed_candidates[0]


def _ensure_fix_scope(
    worktree: Path,
    module_repo: str,
    test_repo: str,
    test_repo_baseline_diff: str,
    allowed_dependency_paths: list[str] | None = None,
) -> list[str]:
    module_rel = normalize_repo_path(module_repo)
    test_rel = normalize_repo_path(test_repo)
    module_path = worktree if module_rel == "." else worktree / module_rel
    test_path = worktree if test_rel == "." else worktree / test_rel

    current_test_diff = git_diff(test_path)
    if current_test_diff != test_repo_baseline_diff:
        raise RuntimeError("The agent changed the reproduced generated test file. Fix jobs may only modify module source code.")

    unexpected: list[str] = []
    for line in git_status(worktree).splitlines():
        path = _status_path(line)
        if not path:
            continue
        norm = normalize_repo_path(path)
        if _is_ignored_worktree_artifact(norm):
            continue
        if _is_under(norm, test_rel):
            continue
        if _is_under(norm, module_rel):
            if _is_include_path(norm):
                unexpected.append(line)
            continue
        if any(_is_under(norm, normalize_repo_path(path)) for path in (allowed_dependency_paths or [])):
            dependency = next(
                normalize_repo_path(path)
                for path in (allowed_dependency_paths or [])
                if _is_under(norm, normalize_repo_path(path))
            )
            dependency_path = worktree / dependency
            if dependency_path.exists() and not git_status(dependency_path).strip():
                continue
        unexpected.append(line)

    module_status = git_status(module_path)
    module_changes = []
    for line in module_status.splitlines():
        path = normalize_repo_path(_status_path(line))
        if not path:
            continue
        module_changes.append(path)
        if _is_include_path(path):
            unexpected.append(f"{module_rel}/{path}")

    if unexpected:
        raise RuntimeError(
            "Bug-fix jobs may only change implementation files under the selected module and must not edit include/ or tests. "
            "Unexpected changes:\n"
            + "\n".join(unexpected)
        )
    if not module_changes:
        raise RuntimeError("The agent did not change any module source files, so no repair can be reviewed.")
    return module_changes


_CLANG_FORMAT_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}


def _run_fix_format_check(
    worktree: Path,
    module_path: Path,
    changed_files: list[str],
    artifact_dir: Path,
    emit,
    *,
    config,
) -> dict[str, Any]:
    format_files = [
        path
        for path in changed_files
        if Path(path).suffix.lower() in _CLANG_FORMAT_EXTENSIONS and (module_path / path).is_file()
    ]
    output_path = artifact_dir / "format_check_output.txt"
    if not format_files:
        message = "No changed C/C++ implementation files require clang-format validation."
        output_path.write_text(message, encoding="utf-8")
        emit("info", message)
        return {"status": "passed", "files": []}

    resolved = resolve_clang_format(config)
    if not resolved.matches_gme:
        emit("warn", describe_version_mismatch(resolved))
    clang_format = resolved.path
    version = f"clang-format version {resolved.version}" if resolved.version else "clang-format version unknown"

    style_file = worktree / ".clang-format"
    style = f"file:{style_file}" if style_file.exists() else "file"
    command = [clang_format, "--dry-run", "--Werror", f"--style={style}", *format_files]
    emit("cmd", "clang-format --dry-run --Werror --style=file " + " ".join(format_files))
    completed = subprocess.run(
        command,
        cwd=str(module_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(part.strip() for part in (version, completed.stdout, completed.stderr) if part.strip())
    output_path.write_text(output or "clang-format check passed.", encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"clang-format validation failed.\n{output[-4000:]}")
    emit("info", f"clang-format validation passed for {len(format_files)} changed files ({version}).")
    return {"status": "passed", "files": format_files, "version": version, "path": clang_format}


def _require_full_tests_pass(output: str) -> int:
    failed_counts = [
        int(match.group(1))
        for match in re.finditer(r"^\[\s*FAILED\s*\]\s+(\d+)\s+tests?\.?", output or "", re.MULTILINE)
    ]
    if any(count > 0 for count in failed_counts):
        raise RuntimeError(f"The configured module full test run reported failures.\n{(output or '')[-4000:]}")
    passed = re.search(r"^\[\s*PASSED\s*\]\s+(\d+)\s+tests?\.?", output or "", re.MULTILINE)
    if passed is None or int(passed.group(1)) <= 0:
        raise RuntimeError(f"The configured module full test run did not produce a successful GTest summary.\n{(output or '')[-4000:]}")
    return int(passed.group(1))


def _require_memory_audit_pass(summary: dict[str, Any]) -> None:
    total = int(summary.get("total") or 0)
    passed = int(summary.get("passed") or 0)
    leaking = int(summary.get("leaking") or 0)
    errors = int(summary.get("errors") or 0)
    not_applicable = int(summary.get("not_applicable") or 0)
    functional_failed = int(summary.get("functional_failed") or 0)
    if total <= 0 or passed != total or leaking or errors or not_applicable or functional_failed:
        raise RuntimeError(
            "Target-test memory audit did not pass cleanly: "
            f"total={total}, passed={passed}, leaking={leaking}, errors={errors}, "
            f"not_applicable={not_applicable}, functional_failed={functional_failed}."
        )


def _status_path(line: str) -> str:
    value = line[3:] if len(line) > 3 else ""
    if " -> " in value:
        value = value.split(" -> ", 1)[1]
    return value.strip()


def _is_under(path: str, root: str) -> bool:
    root = normalize_repo_path(root)
    if root == ".":
        return True
    return path == root or path.startswith(f"{root}/")


def _is_include_path(path: str) -> bool:
    norm = normalize_repo_path(path)
    return norm == "include" or norm.startswith("include/") or "/include/" in norm


def _is_ignored_worktree_artifact(path: str) -> bool:
    norm = normalize_repo_path(path)
    name = Path(norm).name
    return "/" not in norm and name.endswith(".csv") and name.startswith("timer_res")
