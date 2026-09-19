from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import codecs
import re
import shutil
import subprocess
import time
import uuid

from ..harness.runner import HarnessRunner
from ..execution.clang_format import describe_version_mismatch, resolve_clang_format
from ..git.diff import commit_paths, create_pr, push_branch
from ..git.repositories import cleanup_worktree_dependencies, prepare_worktree_dependencies
from ..git.worktree import create_remote_worktree, normalize_repo_path, remove_worktree, run_git
from ..prompts import skip_known_failure_prompt
from .memory_audit_flow import run_selected_memory_audit


@dataclass(frozen=True, slots=True)
class SelectedTestBlock:
    rel_path: str
    suite: str
    name: str
    text: str
    previous_test: tuple[str, str] | None
    next_test: tuple[str, str] | None


@dataclass(frozen=True, slots=True)
class Utf8Source:
    text: str
    newline: str
    has_bom: bool


def run_skip_pr_job(ctx, job_id: str) -> None:
    run_selected_tests_pr_job(ctx, job_id, None)


def run_selected_tests_pr_job(ctx, job_id: str, selected_tests: list[dict[str, str]] | None) -> None:
    emit = ctx._job_emit(job_id)
    job = ctx.db.get_job(job_id)
    worktree_path = job.get("worktree_path")
    branch = job.get("branch")
    if not worktree_path or not branch:
        emit("error", "Job is missing worktree or branch.")
        return

    verification_worktree = None
    verification_dependencies: list[str] = []
    try:
        worktree = Path(worktree_path)
        artifact_dir = ctx._artifact_dir(job_id)
        target_repo = ctx._job_target_repo(job)
        target_path = ctx._target_repo_path(worktree, target_repo)
        manifest_tests = _job_generated_tests(job)
        if not manifest_tests:
            raise RuntimeError("The selected job has no generated_tests.json entries to submit.")

        open_failures = _latest_open_failures_for_job(ctx.db.list_failures(), job_id)
        requested_tests = selected_tests
        if requested_tests is None:
            requested_tests = [
                {
                    "suite": str(failure.get("test_suite") or ""),
                    "name": str(failure.get("test_name") or ""),
                }
                for failure in open_failures
            ]
        selected_manifest_tests = _selected_manifest_tests(manifest_tests, requested_tests)
        selected_keys = {(item["suite"], item["name"]) for item in selected_manifest_tests}
        submitted_names = _submitted_test_names(job)
        duplicate_names = sorted(f"{suite}.{name}" for suite, name in selected_keys if f"{suite}.{name}" in submitted_names)
        if duplicate_names:
            raise RuntimeError("Selected tests were already submitted in a PR: " + ", ".join(duplicate_names))

        test_log = _latest_test_output(artifact_dir)
        passing_keys, skipped_keys, failures = _classify_selected_tests(
            selected_keys,
            open_failures,
            test_log,
        )
        commit_rel_paths = _selected_pr_test_paths(selected_manifest_tests, target_path)
        required_skip_keys = skipped_keys | {
            (str(failure.get("test_suite") or ""), str(failure.get("test_name") or ""))
            for failure in failures
        }
        # Keep the generation worktree as the unskipped reproduction source. Skip
        # markers belong only to the fresh Test PR worktree created below.
        selected_blocks = _extract_selected_test_blocks(target_path, selected_manifest_tests)

        pr_branch = _selected_pr_branch_name(job)
        target_base_branch = str(job.get("metadata", {}).get("target_base_branch") or ctx.config.test_base_branch)
        remote_base_ref = f"{ctx.config.github_remote}/{target_base_branch}"

        ctx.db.update_job(job_id, status="creating_pr")
        verification_worktree = create_remote_worktree(
            ctx.config,
            job_id,
            f"pr-verify-{job.get('module') or 'tests'}",
            emit,
        )
        verification_dependencies = prepare_worktree_dependencies(
            ctx.config,
            verification_worktree.path,
            str(job.get("module") or ""),
            target_repo,
            emit,
        )
        verification_target_path = ctx._target_repo_path(verification_worktree.path, target_repo)
        _checkout_fresh_pr_branch(
            verification_target_path,
            pr_branch,
            ctx.config.github_remote,
            target_base_branch,
            emit,
        )
        base_sources = _insert_selected_test_blocks(verification_target_path, selected_blocks, emit)

        if failures:
            allowed_files = [normalize_repo_path(f"{target_repo}/{path}") for path in commit_rel_paths]
            prompt = skip_known_failure_prompt(test_log, failures, target_repo, allowed_files)
            (artifact_dir / "skip_prompt.md").write_text(prompt, encoding="utf-8")

            ctx.db.update_job(job_id, status="applying_skips")
            runner = HarnessRunner(ctx.config, emit)
            result = runner.run(
                prompt,
                verification_worktree.path,
                skill_names=ctx._test_skill_names(),
                on_session_started=lambda session_id: ctx.db.update_job(
                    job_id,
                    harness_session_id=session_id,
                ),
            )
            (artifact_dir / "agent_skip_result.txt").write_text(result.final_response, encoding="utf-8")
            if result.session_id:
                ctx.db.update_job(job_id, harness_session_id=result.session_id)

        _format_generated_tests(
            verification_worktree.path,
            verification_target_path,
            commit_rel_paths,
            emit,
            config=ctx.config,
        )
        for rel_path in commit_rel_paths:
            source = _read_utf8_source(verification_target_path / rel_path)
            _require_skips_for_tests(
                source.text,
                required_skip_keys & _manifest_keys_for_path(manifest_tests, rel_path),
                rel_path,
            )
        _validate_fresh_pr_files(verification_target_path, base_sources, selected_blocks, remote_base_ref)

        ctx._run_configure_and_build(job_id, verification_worktree.path)
        gtest_filter = _test_keys_filter(selected_keys)
        output = ctx._run_tests(
            job_id,
            verification_worktree.path,
            gtest_filter,
            artifact_name="gtest_output_selected_pr.txt",
        )
        _require_selected_tests_reported(output, selected_keys)
        ctx._write_job_artifacts(job_id, verification_worktree.path, artifact_dir)
        _validate_selected_test_results(output, passing_keys, required_skip_keys)

        ctx.db.update_job(job_id, status="running_memory_audit")
        memory_summary = run_selected_memory_audit(
            ctx,
            job_id,
            verification_worktree.path,
            gtest_filter,
            artifact_dir=artifact_dir,
        )
        if memory_summary["leaking"] or memory_summary["errors"] or memory_summary["functional_failed"]:
            raise RuntimeError(
                "Selected tests did not pass PR memory validation: "
                f"leaking={memory_summary['leaking']}, errors={memory_summary['errors']}, "
                f"functional_failed={memory_summary['functional_failed']}"
            )

        ctx.db.update_job(job_id, status="creating_pr")
        _validate_fresh_pr_files(verification_target_path, base_sources, selected_blocks, remote_base_ref)
        _require_only_selected_paths_changed(verification_target_path, commit_rel_paths)
        title = _skip_pr_title(job, selected_manifest_tests)
        commit_paths(verification_target_path, commit_rel_paths, title, emit)
        _require_single_commit_on_base(verification_target_path, remote_base_ref)
        push_branch(ctx.config, verification_target_path, pr_branch, emit)
        pr_url = create_pr(
            ctx.config,
            verification_target_path,
            title,
            _selected_pr_body(selected_manifest_tests, failures, has_skips=bool(required_skip_keys)),
            emit,
            base_branch=target_base_branch,
        )
        metadata = dict(ctx.db.get_job(job_id).get("metadata") or {})
        metadata["pr_url"] = pr_url
        selected_names = [f"{item['suite']}.{item['name']}" for item in selected_manifest_tests]
        metadata["selected_pr_url"] = pr_url
        metadata["selected_pr_branch"] = pr_branch
        metadata["submitted_test_names"] = _ordered_unique(
            [*list(metadata.get("submitted_test_names") or []), *selected_names]
        )
        metadata["test_prs"] = [
            *list(metadata.get("test_prs") or []),
            {
                "url": pr_url,
                "branch": pr_branch,
                "tests": selected_names,
                "skipped_tests": sorted(f"{suite}.{name}" for suite, name in required_skip_keys),
            },
        ]
        failure_ids = [str(failure.get("id") or "") for failure in failures if failure.get("id")]
        metadata["skip_failure_ids"] = _ordered_unique(
            [*list(metadata.get("skip_failure_ids") or []), *failure_ids]
        )
        metadata["skip_commit_paths"] = commit_rel_paths
        metadata["skip_source_worktree_untouched"] = True
        metadata.pop("skip_local_restored_paths", None)
        if failures or required_skip_keys:
            metadata["skip_pr_url"] = pr_url
            metadata["skip_pr_branch"] = pr_branch
        ctx.db.update_job(job_id, status="pr_created", metadata=metadata)
        try:
            ctx._write_job_artifacts(job_id, worktree, artifact_dir)
        except Exception as artifact_exc:
            emit("warn", f"PR was created, but refreshed local artifacts could not be written: {artifact_exc}")
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        emit("error", str(exc))
    finally:
        if verification_worktree is not None:
            cleanup_worktree_dependencies(
                ctx.config,
                verification_worktree.path,
                verification_dependencies,
                emit,
            )
            try:
                remove_worktree(ctx.config, verification_worktree.path, emit)
                run_git(["branch", "-D", verification_worktree.branch], ctx.config.gme_repo_path, emit)
            except Exception as cleanup_exc:
                emit("warn", f"Could not fully clean PR verification worktree: {cleanup_exc}")


def _selected_manifest_tests(
    manifest_tests: list[dict[str, str]],
    selected_tests: list[dict[str, str]],
) -> list[dict[str, str]]:
    requested: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in selected_tests:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("suite") or "").strip(), str(item.get("name") or "").strip())
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        requested.append(key)
    if not requested:
        raise RuntimeError("Select at least one generated test before creating a PR.")

    by_key = {(item["suite"], item["name"]): item for item in manifest_tests}
    missing = [f"{suite}.{name}" for suite, name in requested if (suite, name) not in by_key]
    if missing:
        raise RuntimeError("Only tests listed in .gme-agent/generated_tests.json can be submitted: " + ", ".join(missing))
    return [by_key[key] for key in requested]


def _classify_selected_tests(
    selected_keys: set[tuple[str, str]],
    open_failures: list[dict[str, Any]],
    test_output: str,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], list[dict[str, Any]]]:
    failure_by_key = {
        (str(item.get("test_suite") or ""), str(item.get("test_name") or "")): item
        for item in open_failures
    }
    passing: set[tuple[str, str]] = set()
    skipped: set[tuple[str, str]] = set()
    selected_failures: list[dict[str, Any]] = []
    unknown: list[str] = []
    failed_without_record: list[str] = []
    for key in sorted(selected_keys):
        full_name = f"{key[0]}.{key[1]}"
        failure = failure_by_key.get(key)
        status = _gtest_test_status(test_output, full_name)
        if failure is not None:
            selected_failures.append(failure)
        elif status == "OK":
            passing.add(key)
        elif status == "SKIPPED":
            skipped.add(key)
        elif status == "FAILED":
            failed_without_record.append(full_name)
        else:
            unknown.append(full_name)
    if failed_without_record:
        raise RuntimeError(
            "Selected failing tests have no current failure record; run the selected tests again first: "
            + ", ".join(failed_without_record)
        )
    if unknown:
        raise RuntimeError("Selected tests are unconfirmed; run them before creating a PR: " + ", ".join(unknown))
    return passing, skipped, selected_failures


def _submitted_test_names(job: dict[str, Any]) -> set[str]:
    values = (job.get("metadata") or {}).get("submitted_test_names") or []
    return {str(value) for value in values if str(value)}


def _selected_pr_test_paths(selected_tests: list[dict[str, str]], target_path: Path) -> list[str]:
    paths = _ordered_unique([normalize_repo_path(item["file"]) for item in selected_tests])
    missing = [path for path in paths if not (target_path / path).is_file()]
    if missing:
        raise RuntimeError("Selected generated test files were not found: " + ", ".join(missing))
    return paths


def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _latest_open_failures_for_job(failures: list[dict[str, Any]], job_id: str) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for failure in failures:
        if failure.get("job_id") != job_id or failure.get("status") != "open":
            continue
        key = (str(failure.get("test_suite") or ""), str(failure.get("test_name") or ""))
        if not key[0] or not key[1]:
            continue
        current = latest.get(key)
        if current is None or _failure_timestamp(failure) > _failure_timestamp(current):
            latest[key] = failure
    return sorted(latest.values(), key=_failure_timestamp, reverse=True)


def _failure_timestamp(failure: dict[str, Any]) -> str:
    return str(failure.get("updated_at") or failure.get("created_at") or "")


def _skip_pr_test_paths(
    job: dict[str, Any],
    failures: list[dict[str, Any]],
    target_path: Path,
    manifest_tests: list[dict[str, str]] | None = None,
) -> list[str]:
    if manifest_tests:
        return _manifest_test_paths_for_failures(failures, target_path, manifest_tests)
    return _generated_test_paths(job, failures, target_path)


def _generated_test_paths(job: dict[str, Any], failures: list[dict[str, Any]], target_path: Path) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    resolved_target = target_path.resolve()
    for failure in failures:
        path_value = str(failure.get("file") or "")
        if not path_value:
            continue
        try:
            rel = Path(path_value).resolve().relative_to(resolved_target)
        except ValueError:
            continue
        rel_path = normalize_repo_path(rel)
        if not _is_generated_test_file(rel_path) or rel_path in seen:
            continue
        seen.add(rel_path)
        result.append(rel_path)

    if result:
        return result

    fallback = _generated_test_path_for_module(str(job.get("module") or ""))
    if fallback:
        return [fallback]
    raise RuntimeError("Could not determine the generated test file to update.")


def _generated_test_path_for_module(module: str) -> str:
    module_name = normalize_repo_path(module)
    if module_name == ".":
        return ""
    module_token = "_".join(part for part in module_name.split("/") if part)
    return normalize_repo_path(f"src/{module_name}/gme_agent_{module_token}_generated_test.cpp")


def _is_generated_test_file(path: str) -> bool:
    name = Path(path).name
    return name.startswith("gme_agent_") and name.endswith("_generated_test.cpp")


def _job_generated_tests(job: dict[str, Any]) -> list[dict[str, str]]:
    tests = (job.get("metadata") or {}).get("generated_tests") or []
    result: list[dict[str, str]] = []
    for item in tests:
        if not isinstance(item, dict):
            continue
        file_path = normalize_repo_path(str(item.get("file") or ""))
        suite = str(item.get("suite") or "").strip()
        name = str(item.get("name") or "").strip()
        api = str(item.get("api") or "").strip()
        if file_path and suite and name:
            result.append({"file": file_path, "suite": suite, "name": name, "api": api})
    return result


def _manifest_test_paths_for_failures(
    failures: list[dict[str, Any]],
    target_path: Path,
    manifest_tests: list[dict[str, str]],
) -> list[str]:
    manifest_by_key = {(item["suite"], item["name"]): item for item in manifest_tests}
    missing: list[str] = []
    paths: list[str] = []
    seen: set[str] = set()
    for failure in failures:
        key = (str(failure.get("test_suite") or ""), str(failure.get("test_name") or ""))
        item = manifest_by_key.get(key)
        if item is None:
            missing.append(f"{key[0]}.{key[1]}")
            continue
        rel_path = item["file"] or _failure_rel_path(target_path, failure)
        if not rel_path:
            missing.append(f"{key[0]}.{key[1]}")
            continue
        if rel_path not in seen:
            seen.add(rel_path)
            paths.append(rel_path)
    if missing:
        raise RuntimeError("Only tests listed in .gme-agent/generated_tests.json can be skipped: " + ", ".join(missing))
    return paths


def _failure_rel_path(target_path: Path, failure: dict[str, Any]) -> str:
    path_value = str(failure.get("file") or "")
    if not path_value:
        return ""
    try:
        return normalize_repo_path(Path(path_value).resolve().relative_to(target_path.resolve()))
    except ValueError:
        return ""


def _skip_pr_branch_name(job: dict[str, Any]) -> str:
    module = _branch_slug(str(job.get("module") or "generated"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    job_token = str(job.get("id") or "job")[:8]
    suffix = uuid.uuid4().hex[:6]
    return f"gme-agent/skip-{module}-{stamp}-{job_token}-{suffix}"


def _selected_pr_branch_name(job: dict[str, Any]) -> str:
    module = _branch_slug(str(job.get("module") or "generated"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    job_token = str(job.get("id") or "job")[:8]
    suffix = uuid.uuid4().hex[:6]
    return f"gme-agent/tests-{module}-{stamp}-{job_token}-{suffix}"


def _branch_slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "generated"


def _checkout_new_skip_branch(target_path: Path, branch: str, emit) -> None:
    run_git(["checkout", "-b", branch], target_path, emit)


def _checkout_branch(target_path: Path, branch: str, emit) -> None:
    run_git(["checkout", branch], target_path, emit)


def _try_checkout_branch(target_path: Path, branch: str, emit) -> None:
    try:
        _checkout_branch(target_path, branch, emit)
    except Exception as exc:
        emit("warn", f"Could not switch back to {branch} before restoring generated tests: {exc}")


def _manifest_keys_for_path(manifest_tests: list[dict[str, str]], rel_path: str) -> set[tuple[str, str]]:
    normalized = normalize_repo_path(rel_path)
    return {
        (str(item.get("suite") or ""), str(item.get("name") or ""))
        for item in manifest_tests
        if normalize_repo_path(str(item.get("file") or "")) == normalized
    }


def _extract_selected_test_blocks(
    target_path: Path,
    selected_tests: list[dict[str, str]],
) -> list[SelectedTestBlock]:
    selected_by_path: dict[str, set[tuple[str, str]]] = {}
    for item in selected_tests:
        rel_path = normalize_repo_path(str(item.get("file") or ""))
        selected_by_path.setdefault(rel_path, set()).add(
            (str(item.get("suite") or ""), str(item.get("name") or ""))
        )

    extracted: list[SelectedTestBlock] = []
    for rel_path, selected_keys in selected_by_path.items():
        source = _read_utf8_source(target_path / rel_path)
        blocks = _test_blocks(source.text, rel_path)
        found: set[tuple[str, str]] = set()
        for index, (start, end, suite, name) in enumerate(blocks):
            key = (suite, name)
            if key not in selected_keys:
                continue
            previous_test = (blocks[index - 1][2], blocks[index - 1][3]) if index > 0 else None
            next_test = (blocks[index + 1][2], blocks[index + 1][3]) if index + 1 < len(blocks) else None
            extracted.append(
                SelectedTestBlock(
                    rel_path=rel_path,
                    suite=suite,
                    name=name,
                    text=source.text[start:end],
                    previous_test=previous_test,
                    next_test=next_test,
                )
            )
            found.add(key)
        missing = selected_keys - found
        if missing:
            names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing))
            raise RuntimeError(f"Could not extract selected tests from {rel_path}: {names}")
    return extracted


def _stash_task_target_changes(target_path: Path, job_id: str, emit) -> str:
    if not run_git(["status", "--porcelain"], target_path).strip():
        return ""
    previous = ""
    try:
        previous = run_git(["rev-parse", "--verify", "refs/stash"], target_path).strip()
    except RuntimeError:
        pass
    run_git(
        ["stash", "push", "--include-untracked", "-m", f"gme-agent-selected-pr-{job_id}"],
        target_path,
        emit,
    )
    current = run_git(["rev-parse", "--verify", "refs/stash"], target_path).strip()
    if not current or current == previous:
        raise RuntimeError("Git did not create a stash for the task test changes.")
    return current


def _restore_task_target_changes(target_path: Path, stash_commit: str, emit) -> None:
    run_git(["stash", "apply", stash_commit], target_path, emit)
    stash_ref = ""
    listing = run_git(["stash", "list", "--format=%gd%x09%H"], target_path)
    for line in listing.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].strip() == stash_commit:
            stash_ref = parts[0].strip()
            break
    if stash_ref:
        run_git(["stash", "drop", stash_ref], target_path, emit)
    else:
        emit("warn", f"Restored task changes, but could not find stash reference for {stash_commit}.")


def _checkout_fresh_pr_branch(
    target_path: Path,
    branch: str,
    remote: str,
    base_branch: str,
    emit,
) -> None:
    if not remote.strip():
        raise RuntimeError("Git remote is empty; cannot create a PR branch from the latest main.")
    run_git(["fetch", remote, base_branch], target_path, emit)
    remote_ref = f"{remote}/{base_branch}"
    run_git(["rev-parse", "--verify", remote_ref], target_path, emit)
    run_git(["checkout", "-b", branch, remote_ref], target_path, emit)


def _insert_selected_test_blocks(
    target_path: Path,
    selected_blocks: list[SelectedTestBlock],
    emit,
) -> dict[str, Utf8Source]:
    by_path: dict[str, list[SelectedTestBlock]] = {}
    for block in selected_blocks:
        by_path.setdefault(block.rel_path, []).append(block)

    base_sources: dict[str, Utf8Source] = {}
    for rel_path, file_blocks in by_path.items():
        file_path = target_path / rel_path
        if not file_path.exists():
            raise RuntimeError(f"Selected test target file no longer exists on the latest main: {rel_path}")
        source = _read_utf8_source(file_path)
        base_sources[rel_path] = source
        text = source.text
        for block in file_blocks:
            text = _insert_one_test_block(text, source.newline, block, rel_path)
            emit("info", f"Inserted {block.suite}.{block.name} into latest {rel_path}.")
        _write_utf8_source(file_path, Utf8Source(text, source.newline, source.has_bom))
    return base_sources


def _insert_one_test_block(text: str, newline: str, selected: SelectedTestBlock, rel_path: str) -> str:
    blocks = _test_blocks(text, rel_path)
    keyed = {(suite, name): (start, end) for start, end, suite, name in blocks}
    key = (selected.suite, selected.name)
    if key in keyed:
        raise RuntimeError(
            f"Selected test already exists on the latest main and cannot be submitted again: "
            f"{selected.suite}.{selected.name}"
        )

    offset: int | None = None
    if selected.previous_test and selected.previous_test in keyed:
        offset = keyed[selected.previous_test][1]
    elif selected.next_test and selected.next_test in keyed:
        offset = keyed[selected.next_test][0]
    else:
        same_suite = [(start, end) for start, end, suite, _name in blocks if suite == selected.suite]
        if same_suite:
            offset = same_suite[-1][1]
    if offset is None:
        raise RuntimeError(
            f"Could not find a safe insertion anchor for {selected.suite}.{selected.name} in latest {rel_path}."
        )

    block_text = _normalize_newlines(selected.text.strip(), newline)
    prefix = text[:offset]
    suffix = text[offset:]
    before = "" if prefix.endswith(newline * 2) else (newline if prefix.endswith(newline) else newline * 2)
    after = "" if suffix.startswith(newline * 2) else (newline if suffix.startswith(newline) else newline * 2)
    return prefix + before + block_text + after + suffix


def _read_utf8_source(path: Path) -> Utf8Source:
    raw = path.read_bytes()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Test source is not valid UTF-8: {path}: {exc}") from exc
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return Utf8Source(text=text, newline=newline, has_bom=has_bom)


def _write_utf8_source(path: Path, source: Utf8Source) -> None:
    payload = _normalize_newlines(source.text, source.newline).encode("utf-8")
    path.write_bytes((codecs.BOM_UTF8 if source.has_bom else b"") + payload)


def _normalize_newlines(text: str, newline: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _validate_fresh_pr_files(
    target_path: Path,
    base_sources: dict[str, Utf8Source],
    selected_blocks: list[SelectedTestBlock],
    remote_base_ref: str,
) -> None:
    selected_by_path: dict[str, set[tuple[str, str]]] = {}
    for block in selected_blocks:
        selected_by_path.setdefault(block.rel_path, set()).add((block.suite, block.name))

    for rel_path, base_source in base_sources.items():
        current_source = _read_utf8_source(target_path / rel_path)
        if current_source.has_bom != base_source.has_bom or current_source.newline != base_source.newline:
            raise RuntimeError(f"Preparing the PR changed the encoding or line endings of latest {rel_path}.")
        base_blocks = {
            (suite, name): base_source.text[start:end]
            for start, end, suite, name in _test_blocks(base_source.text, rel_path)
        }
        current_blocks = {
            (suite, name): current_source.text[start:end]
            for start, end, suite, name in _test_blocks(current_source.text, rel_path)
        }
        missing_original = set(base_blocks) - set(current_blocks)
        changed_original = {
            key for key, block_text in base_blocks.items() if current_blocks.get(key) != block_text
        }
        if missing_original or changed_original:
            names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing_original | changed_original))
            raise RuntimeError(f"Preparing the PR changed existing main tests in {rel_path}: {names}")
        added = set(current_blocks) - set(base_blocks)
        if added != selected_by_path.get(rel_path, set()):
            names = ", ".join(f"{suite}.{name}" for suite, name in sorted(added))
            raise RuntimeError(f"Latest-main PR contains unexpected generated tests in {rel_path}: {names or 'none'}")

    numstat = run_git(["diff", "--numstat", remote_base_ref, "--", *sorted(base_sources)], target_path)
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2 and parts[1] != "0":
            raise RuntimeError(f"Latest-main PR must be addition-only, but git reports deletions: {line}")


def _require_single_commit_on_base(target_path: Path, remote_base_ref: str) -> None:
    count = run_git(["rev-list", "--count", f"{remote_base_ref}..HEAD"], target_path).strip()
    if count != "1":
        raise RuntimeError(f"PR branch must contain exactly one commit on latest main; found {count or 'unknown'}.")


def _require_only_selected_paths_changed(target_path: Path, rel_paths: list[str]) -> None:
    allowed = {normalize_repo_path(path) for path in rel_paths}
    changed = {
        normalize_repo_path(line.strip())
        for line in run_git(["diff", "--name-only"], target_path).splitlines()
        if line.strip()
    }
    changed.update(
        normalize_repo_path(line.strip())
        for line in run_git(["ls-files", "--others", "--exclude-standard"], target_path).splitlines()
        if line.strip()
    )
    unexpected = changed - allowed
    if unexpected:
        raise RuntimeError(
            "PR validation produced changes outside the selected generated test files: "
            + ", ".join(sorted(unexpected))
        )


def _snapshot_generated_tests(target_path: Path, rel_paths: list[str]) -> dict[str, str]:
    snapshots: dict[str, str] = {}
    for rel_path in rel_paths:
        file_path = target_path / rel_path
        if not file_path.exists():
            raise RuntimeError(f"Generated test file was not found: {rel_path}")
        snapshots[rel_path] = file_path.read_text(encoding="utf-8", errors="replace")
    return snapshots


def _restore_generated_tests(target_path: Path, snapshots: dict[str, str], emit) -> None:
    for rel_path, text in snapshots.items():
        file_path = target_path / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(text, encoding="utf-8")
        emit("info", f"Restored full generated test file in local worktree: {rel_path}")


def _format_generated_tests(worktree: Path, target_path: Path, rel_paths: list[str], emit, *, config) -> None:
    if not rel_paths:
        return
    resolved = resolve_clang_format(config)
    if not resolved.matches_gme:
        emit("warn", describe_version_mismatch(resolved))
    clang_format = resolved.path

    style_file = worktree / ".clang-format"
    style = f"file:{style_file}" if style_file.exists() else "file"
    cmd = [clang_format, "-i", f"--style={style}", *rel_paths]
    emit("cmd", "clang-format -i --style=file " + " ".join(rel_paths))
    proc = subprocess.run(
        cmd,
        cwd=str(target_path),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.stdout.strip():
        emit("cmd", proc.stdout.strip())
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout.strip() or "clang-format failed.")


def _prune_generated_tests_to_failures(target_path: Path, rel_paths: list[str], failures: list[dict[str, Any]], emit) -> None:
    tests_by_path = _failure_tests_by_generated_path(target_path, rel_paths, failures)
    for rel_path in rel_paths:
        keep_tests = tests_by_path.get(rel_path, set())
        if not keep_tests:
            continue
        file_path = target_path / rel_path
        if not file_path.exists():
            raise RuntimeError(f"Generated test file was not found: {rel_path}")
        text = file_path.read_text(encoding="utf-8", errors="replace")
        pruned = _prune_generated_test_text(text, keep_tests, rel_path)
        file_path.write_text(pruned, encoding="utf-8")
        emit("info", f"Pruned {rel_path} to {len(keep_tests)} skipped failure test(s).")


def _prune_manifest_tests_to_failures(
    target_path: Path,
    rel_paths: list[str],
    failures: list[dict[str, Any]],
    manifest_tests: list[dict[str, str]],
    emit,
) -> None:
    keep_tests = {(str(failure.get("test_suite") or ""), str(failure.get("test_name") or "")) for failure in failures}
    _prune_manifest_tests_to_selection(target_path, rel_paths, keep_tests, keep_tests, manifest_tests, emit)


def _prune_manifest_tests_to_selection(
    target_path: Path,
    rel_paths: list[str],
    selected_tests: set[tuple[str, str]],
    required_skip_tests: set[tuple[str, str]],
    manifest_tests: list[dict[str, str]],
    emit,
) -> None:
    manifest_by_path: dict[str, set[tuple[str, str]]] = {}
    for item in manifest_tests:
        manifest_by_path.setdefault(item["file"], set()).add((item["suite"], item["name"]))

    found_selected: set[tuple[str, str]] = set()
    for rel_path in rel_paths:
        file_path = target_path / rel_path
        if not file_path.exists():
            raise RuntimeError(f"Generated test target file was not found: {rel_path}")
        generated_in_file = manifest_by_path.get(rel_path, set())
        selected_in_file = generated_in_file & selected_tests
        remove_tests = generated_in_file - selected_tests
        text = file_path.read_text(encoding="utf-8", errors="replace")
        pruned = _remove_test_blocks(text, remove_tests, rel_path) if remove_tests else text
        _require_tests_exist(pruned, selected_in_file, rel_path)
        _require_skips_for_tests(pruned, selected_in_file & required_skip_tests, rel_path)
        file_path.write_text(pruned, encoding="utf-8")
        found_selected.update(selected_in_file)
        emit("info", f"Prepared {rel_path} with {len(selected_in_file)} selected generated test(s).")

    missing = selected_tests - found_selected
    if missing:
        names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing))
        raise RuntimeError(f"Could not map selected tests to PR files: {names}")


def _failure_tests_by_generated_path(
    target_path: Path,
    rel_paths: list[str],
    failures: list[dict[str, Any]],
) -> dict[str, set[tuple[str, str]]]:
    result: dict[str, set[tuple[str, str]]] = {}
    rel_set = set(rel_paths)
    unresolved: list[tuple[str, str]] = []
    resolved_target = target_path.resolve()
    for failure in failures:
        key = (str(failure.get("test_suite") or ""), str(failure.get("test_name") or ""))
        if not key[0] or not key[1]:
            continue
        path_value = str(failure.get("file") or "")
        rel_path = ""
        if path_value:
            try:
                rel_path = normalize_repo_path(Path(path_value).resolve().relative_to(resolved_target))
            except ValueError:
                rel_path = ""
        if rel_path in rel_set:
            result.setdefault(rel_path, set()).add(key)
        else:
            unresolved.append(key)

    if unresolved:
        if len(rel_paths) == 1:
            result.setdefault(rel_paths[0], set()).update(unresolved)
        else:
            names = ", ".join(f"{suite}.{name}" for suite, name in unresolved)
            raise RuntimeError(f"Could not map failures to generated test files: {names}")
    return result


def _remove_test_blocks(text: str, remove_tests: set[tuple[str, str]], rel_path: str) -> str:
    if not remove_tests:
        return text
    blocks = _test_blocks(text, rel_path)
    chunks: list[str] = []
    last = 0
    removed: set[tuple[str, str]] = set()
    for start, end, suite, test_name in blocks:
        key = (suite, test_name)
        if key in remove_tests:
            chunks.append(text[last:start])
            last = end
            removed.add(key)
    chunks.append(text[last:])
    missing = remove_tests - removed
    if missing:
        names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing))
        raise RuntimeError(f"Could not find generated passing tests to remove from {rel_path}: {names}")
    return "".join(chunks)


def _require_skips_for_tests(text: str, keep_tests: set[tuple[str, str]], rel_path: str) -> None:
    if not keep_tests:
        return
    found: set[tuple[str, str]] = set()
    for start, end, suite, test_name in _test_blocks(text, rel_path):
        key = (suite, test_name)
        if key not in keep_tests:
            continue
        block_text = text[start:end]
        if "GTEST_SKIP" not in block_text:
            raise RuntimeError(f"Failure test {suite}.{test_name} in {rel_path} does not contain GTEST_SKIP().")
        found.add(key)
    missing = keep_tests - found
    if missing:
        names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing))
        raise RuntimeError(f"Could not find failure tests in {rel_path}: {names}")


def _require_tests_exist(text: str, expected_tests: set[tuple[str, str]], rel_path: str) -> None:
    if not expected_tests:
        return
    found = {(suite, name) for _start, _end, suite, name in _test_blocks(text, rel_path)}
    missing = expected_tests - found
    if missing:
        names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing))
        raise RuntimeError(f"Could not find selected tests in {rel_path}: {names}")


_TEST_DECL_RE = re.compile(
    r"(?m)^[ \t]*(TEST|TEST_F|TEST_P|TYPED_TEST|TYPED_TEST_P)\s*\(\s*([^,\s]+)\s*,\s*([^)]+?)\s*\)\s*\{"
)


def _prune_generated_test_text(text: str, keep_tests: set[tuple[str, str]], rel_path: str = "generated test") -> str:
    blocks = _test_blocks(text, rel_path)
    if not blocks:
        raise RuntimeError(f"No GoogleTest test blocks were found in {rel_path}.")

    kept: set[tuple[str, str]] = set()
    chunks: list[str] = []
    last = 0
    for start, end, suite, test_name in blocks:
        key = (suite, test_name)
        if key in keep_tests:
            block_text = text[start:end]
            if "GTEST_SKIP" not in block_text:
                raise RuntimeError(f"Failure test {suite}.{test_name} in {rel_path} does not contain GTEST_SKIP().")
            chunks.append(text[last:end])
            last = end
            kept.add(key)
        else:
            chunks.append(text[last:start])
            last = end
    chunks.append(text[last:])

    missing = keep_tests - kept
    if missing:
        names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing))
        raise RuntimeError(f"Could not find failure tests in {rel_path}: {names}")
    return "".join(chunks)


def _test_blocks(text: str, rel_path: str) -> list[tuple[int, int, str, str]]:
    blocks: list[tuple[int, int, str, str]] = []
    pos = 0
    while True:
        match = _TEST_DECL_RE.search(text, pos)
        if match is None:
            return blocks
        brace = text.find("{", match.end() - 1)
        if brace < 0:
            raise RuntimeError(f"Could not find test body start in {rel_path}.")
        end = _matching_brace(text, brace)
        if end < 0:
            suite = _clean_test_name(match.group(2))
            name = _clean_test_name(match.group(3))
            raise RuntimeError(f"Could not find test body end for {suite}.{name} in {rel_path}.")
        blocks.append((match.start(), end + 1, _clean_test_name(match.group(2)), _clean_test_name(match.group(3))))
        pos = end + 1


def _clean_test_name(value: str) -> str:
    return value.strip().rstrip()


def _matching_brace(text: str, start: int) -> int:
    depth = 0
    state = "code"
    i = start
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if ch == '"':
                state = "string"
                i += 1
                continue
            if ch == "'":
                state = "char"
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
        elif state == "string":
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                state = "code"
        elif state == "char":
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                state = "code"
        i += 1
    return -1


def _failure_suite_filter(failures: list[dict[str, Any]]) -> str:
    tests = []
    seen: set[tuple[str, str]] = set()
    for failure in failures:
        suite = str(failure.get("test_suite") or "")
        name = str(failure.get("test_name") or "")
        key = (suite, name)
        if suite and name and key not in seen:
            seen.add(key)
            tests.append(f"{suite}.{name}")
    return ":".join(tests) or "*"


def _test_keys_filter(test_keys: set[tuple[str, str]]) -> str:
    return ":".join(f"{suite}.{name}" for suite, name in sorted(test_keys)) or "*"


def _gtest_test_status(output: str, full_name: str) -> str:
    escaped = re.escape(full_name)
    for status in ("FAILED", "SKIPPED", "OK"):
        if re.search(rf"\[\s*{status}\s*\]\s+{escaped}(?:\s|$)", output or ""):
            return status
    return ""


def _require_selected_tests_reported(output: str, selected_tests: set[tuple[str, str]]) -> None:
    missing = [
        f"{suite}.{name}"
        for suite, name in sorted(selected_tests)
        if not _gtest_test_status(output, f"{suite}.{name}")
    ]
    if missing:
        raise RuntimeError("Selected tests did not appear in the verification output: " + ", ".join(missing))


def _validate_selected_test_results(
    output: str,
    passing_tests: set[tuple[str, str]],
    skipped_tests: set[tuple[str, str]],
) -> None:
    mismatches: list[str] = []
    for suite, name in sorted(passing_tests):
        full_name = f"{suite}.{name}"
        status = _gtest_test_status(output, full_name)
        if status != "OK":
            mismatches.append(f"{full_name} expected OK, got {status or 'missing'}")
    for suite, name in sorted(skipped_tests):
        full_name = f"{suite}.{name}"
        status = _gtest_test_status(output, full_name)
        if status != "SKIPPED":
            mismatches.append(f"{full_name} expected SKIPPED, got {status or 'missing'}")
    if mismatches:
        raise RuntimeError("Selected test verification did not match the expected result: " + "; ".join(mismatches))


def _skip_pr_title(job: dict[str, Any], selected_tests: list[dict[str, str]]) -> str:
    module = str(job.get("module") or "generated").strip() or "generated"
    apis = _ordered_unique([str(item.get("api") or "").strip() for item in selected_tests])
    if not apis:
        raise RuntimeError("Selected tests are missing API metadata; cannot create the PR title.")
    return f"test({module}): 补充 {'、'.join(apis)} 测试"


def _skip_pr_body(job: dict[str, Any], failures: list[dict[str, Any]], gtest_filter: str) -> str:
    tests = [
        {
            "suite": str(failure.get("test_suite") or ""),
            "name": str(failure.get("test_name") or ""),
        }
        for failure in failures
    ]
    return _selected_pr_body(tests, failures)


def _selected_pr_body(
    selected_tests: list[dict[str, str]],
    failures: list[dict[str, Any]],
    *,
    has_skips: bool = False,
) -> str:
    if failures or has_skips:
        intro = "该 PR 由 GME Test Agent 自动生成，新增 GME vs ACIS 对比测试，并对当前已确认存在差异的失败用例增加 skip。"
    else:
        intro = "该 PR 由 GME Test Agent 自动生成，新增选中的 GME vs ACIS 对比测试。"
    lines = [intro, "", "本次新增测试用例："]
    lines.extend(f"{item.get('suite')}.{item.get('name')}" for item in selected_tests)
    return "\n".join(lines)


def _latest_test_output(artifact_dir: Path) -> str:
    candidates = [
        artifact_dir / "gtest_output.txt",
        artifact_dir / "gtest_output_after_skip.txt",
        artifact_dir / "gtest_output_selected_pr.txt",
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return ""
    latest = max(existing, key=lambda path: path.stat().st_mtime_ns)
    return _read_text(latest)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")
