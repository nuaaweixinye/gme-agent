from __future__ import annotations

from pathlib import Path
import uuid
import shutil
import re

from ..execution.runner import merge_failures, parse_gtest_failures, parse_gtest_xml, run_template_command
from ..generated_tests import load_generated_tests_manifest


def run_tests_job(ctx, job_id: str, gtest_filter: str) -> None:
    job = ctx.db.get_job(job_id)
    path = job.get("worktree_path")
    if not path:
        ctx._emit(job_id, "error", "Job has no worktree path.")
        return
    try:
        ctx.db.update_job(job_id, status="running_tests")
        output = ctx._run_tests(job_id, Path(path), gtest_filter)
        ctx._record_failures(job_id, output, gtest_filter, artifact_dir=ctx._artifact_dir(job_id))
        ctx._write_job_artifacts(job_id, Path(path), ctx._artifact_dir(job_id))
        ctx.db.update_job(job_id, status="needs_review")
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        ctx._emit(job_id, "error", str(exc))


def run_build_job(ctx, job_id: str) -> None:
    job = ctx.db.get_job(job_id)
    path = job.get("worktree_path")
    if not path:
        ctx._emit(job_id, "error", "Job has no worktree path.")
        return
    try:
        ctx._run_configure_and_build(job_id, Path(path))
        ctx._write_job_artifacts(job_id, Path(path), ctx._artifact_dir(job_id))
        ctx.db.update_job(job_id, status="needs_review")
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        ctx._emit(job_id, "error", str(exc))


def run_configure_and_build(ctx, job_id: str, worktree: Path) -> str:
    emit = ctx._job_emit(job_id)
    ctx.db.update_job(job_id, status="building")
    build_dir = worktree / "build" / "vscode"
    _clean_build_dir(worktree, build_dir, emit)
    mapping = ctx._command_mapping(worktree, build_dir, "*", artifact_dir=ctx._artifact_dir(job_id))
    code, configure_output = run_template_command(ctx.config.configure_command, mapping, worktree, emit)
    if code != 0:
        raise RuntimeError(f"Configure failed with exit code {code}\n{configure_output[-4000:]}")
    code, build_output = run_template_command(ctx.config.build_command, mapping, worktree, emit)
    if code != 0:
        raise RuntimeError(f"Build failed with exit code {code}\n{build_output[-4000:]}")
    output = configure_output + "\n" + build_output
    (ctx._artifact_dir(job_id) / "build_output.txt").write_text(output, encoding="utf-8")
    return output


def _clean_build_dir(worktree: Path, build_dir: Path, emit) -> None:
    if not build_dir.exists():
        return
    resolved_worktree = worktree.resolve()
    resolved_build_dir = build_dir.resolve()
    if resolved_build_dir == resolved_worktree or resolved_worktree not in resolved_build_dir.parents:
        raise RuntimeError(f"Refusing to clean build directory outside worktree: {resolved_build_dir}")
    emit("info", f"Cleaning CMake build directory before configure: {build_dir}")
    if build_dir.is_dir():
        shutil.rmtree(build_dir)
    else:
        build_dir.unlink()


def run_tests(
    ctx,
    job_id: str,
    worktree: Path,
    gtest_filter: str,
    *,
    artifact_name: str = "gtest_output.txt",
) -> str:
    emit = ctx._job_emit(job_id)
    ctx.db.update_job(job_id, status="running_tests")
    build_dir = worktree / "build" / "vscode"
    artifact_dir = ctx._artifact_dir(job_id)
    mapping = ctx._command_mapping(worktree, build_dir, gtest_filter, artifact_dir=artifact_dir)
    xml_path = Path(mapping["gtest_xml_path"])
    if xml_path.exists():
        xml_path.unlink()
    code, output = run_template_command(ctx.config.test_command, mapping, worktree, emit)
    (artifact_dir / artifact_name).write_text(output, encoding="utf-8")
    if code != 0:
        emit("warn", f"Tests exited with code {code}")
    return output


def _manifest_attribution(manifest_test: dict) -> dict[str, str]:
    """Attribution keys a manifest entry actually declares, omitting the ones it does not.

    `upsert_failure` merges the incoming metadata over the stored blob, so writing an
    explicit `""` for a field the manifest merely omits is not "no attribution": it is an
    assertion that erases attribution an earlier observation established. That loss is
    invisible until the knowledge closed loop refuses to promote a divergence whose
    `metadata_json` no longer carries `interface_id`/`api_name` — a hard condition of
    promotion. A manifest that does not name the field therefore says nothing about it.
    """

    attribution: dict[str, str] = {}
    for key, source in (("interface_id", "interface_id"), ("api_name", "api")):
        value = str(manifest_test.get(source) or "").strip()
        if value:
            attribution[key] = value
    return attribution


def record_failures(ctx, job_id: str, test_output: str, gtest_filter: str, *, artifact_dir: Path) -> list[dict]:
    parsed = merge_failures(parse_gtest_xml(ctx._gtest_xml_path(artifact_dir)), parse_gtest_failures(test_output))
    failures = []
    job = ctx.db.get_job(job_id)
    manifest = load_generated_tests_manifest(
        Path(str(job.get("worktree_path") or ".")),
        ctx._job_target_repo(job),
    )
    manifest_by_test = {
        (str(item.get("suite") or ""), str(item.get("name") or "")): item
        for item in manifest.get("tests") or []
    }
    run_id = uuid.uuid4().hex
    reported_outcomes = _reported_gtest_outcomes(test_output)
    for (suite, test), status in reported_outcomes.items():
        ctx.db.upsert_test_case_result(
            job_id=job_id,
            test_suite=suite,
            test_name=test,
            status=status,
            run_id=run_id,
            gtest_filter=gtest_filter,
        )
    resolved = _resolve_stale_open_failures(ctx, job_id, parsed, test_output, gtest_filter, run_id)
    if resolved:
        ctx._emit(job_id, "info", f"Resolved {resolved} stale open failure records.")
    for item in parsed:
        test_key = (str(item.get("test_suite") or ""), str(item.get("test_name") or ""))
        manifest_test = manifest_by_test.get(test_key) or {}
        attribution = _manifest_attribution(manifest_test)
        failure_id = f"gmefail-{uuid.uuid4().hex[:10]}"
        failure = ctx.db.upsert_failure(
            failure_id=failure_id,
            job_id=job_id,
            test_suite=str(item.get("test_suite") or ""),
            test_name=str(item.get("test_name") or ""),
            file=str(item.get("file") or ""),
            line=int(item.get("line") or 0),
            reason=str(item.get("reason") or ""),
            reproduce_command=ctx._reproduce_command(gtest_filter),
            skip_id=failure_id,
            metadata={
                "gtest_filter": gtest_filter,
                "module": job.get("module") or "",
                "target_repo": ctx._job_target_repo(job),
                **attribution,
            },
        )
        ctx.db.add_failure_observation(
            run_id=run_id,
            failure_id=str(failure["id"]),
            job_id=job_id,
            outcome="failed",
            test_suite=str(item.get("test_suite") or ""),
            test_name=str(item.get("test_name") or ""),
            file=str(item.get("file") or ""),
            line=int(item.get("line") or 0),
            reason=str(item.get("reason") or ""),
            gtest_filter=gtest_filter,
        )
        failures.append(failure)
        ctx._emit(job_id, "warn", f"Observed failure {failure['id']}: {failure['test_suite']}.{failure['test_name']}")
    return failures


def _resolve_stale_open_failures(
    ctx,
    job_id: str,
    parsed: list[dict],
    test_output: str,
    gtest_filter: str,
    run_id: str,
) -> int:
    failed_keys = set(_failure_test_keys(parsed))
    scoped_keys = None if _is_broad_gtest_filter(gtest_filter) else set(_specific_gtest_filter_keys(gtest_filter))
    reported_outcomes = _reported_gtest_outcomes(test_output)
    resolved = 0
    for failure in ctx.db.list_failures():
        if failure.get("job_id") != job_id or failure.get("status") != "open":
            continue
        key = (str(failure.get("test_suite") or ""), str(failure.get("test_name") or ""))
        if (
            key in failed_keys
            or (scoped_keys is not None and key not in scoped_keys)
            or key not in reported_outcomes
        ):
            continue
        ctx.db.update_failure(str(failure["id"]), status="resolved")
        ctx.db.add_failure_observation(
            run_id=run_id,
            failure_id=str(failure["id"]),
            job_id=job_id,
            outcome=reported_outcomes[key],
            test_suite=key[0],
            test_name=key[1],
            file=str(failure.get("file") or ""),
            line=int(failure.get("line") or 0),
            reason="The test was in scope and did not fail in this run.",
            gtest_filter=gtest_filter,
        )
        resolved += 1
    return resolved


def _reported_gtest_outcomes(test_output: str) -> dict[tuple[str, str], str]:
    outcomes: dict[tuple[str, str], str] = {}
    pattern = re.compile(r"^\s*\[\s*(OK|SKIPPED|FAILED)\s*\]\s+([^\s]+)", re.MULTILINE)
    for status, full_name in pattern.findall(test_output or ""):
        if "." not in full_name:
            continue
        suite, test = full_name.rsplit(".", 1)
        outcomes[(suite, test)] = {
            "OK": "passed",
            "SKIPPED": "skipped",
            "FAILED": "failed",
        }[status]
    return outcomes


def _failure_test_keys(items: list[dict]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("test_suite") or ""), str(item.get("test_name") or ""))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _is_broad_gtest_filter(gtest_filter: str) -> bool:
    text = (gtest_filter or "*").strip() or "*"
    positive = text.split("-", 1)[0]
    patterns = [part for part in positive.split(":") if part]
    return not patterns or any("*" in pattern or "?" in pattern for pattern in patterns)


def _specific_gtest_filter_keys(gtest_filter: str) -> list[tuple[str, str]]:
    positive = (gtest_filter or "").split("-", 1)[0]
    keys: list[tuple[str, str]] = []
    for pattern in positive.split(":"):
        pattern = pattern.strip()
        if not pattern or "*" in pattern or "?" in pattern or "." not in pattern:
            continue
        suite, test = pattern.rsplit(".", 1)
        if suite and test:
            keys.append((suite, test))
    return keys
