from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import csv
import json
import re
import shlex
import shutil
import subprocess


_LEAK_RE = re.compile(r"(?m)^\s*Leaks:\s*(\d+)")
_BAD_DELETE_RE = re.compile(r"(?m)^\s*Bad delete pointers:\s*(\d+)")
_SKIPPED_RE = re.compile(r"(?m)^\[\s*SKIPPED\s*\]\s+")
_FAILED_RE = re.compile(r"(?m)^\[\s*FAILED\s*\]\s+")


def run_memory_audit_job(ctx, job_id: str, gtest_filter: str) -> None:
    job = ctx.db.get_job(job_id)
    worktree_path = str(job.get("worktree_path") or "")
    if not worktree_path:
        ctx._emit(job_id, "error", "Job has no worktree path.")
        return

    worktree = Path(worktree_path)
    artifact_dir = ctx._artifact_dir(job_id)
    try:
        ctx.db.update_job(job_id, status="running_memory_audit")
        summary = run_selected_memory_audit(
            ctx,
            job_id,
            worktree,
            gtest_filter or "*",
            artifact_dir=artifact_dir,
        )
        metadata = ctx._merge_metadata(job_id, {"memory_audit": summary})
        ctx.db.update_job(job_id, status="needs_review", metadata=metadata)
        ctx._write_job_artifacts(job_id, worktree, artifact_dir)
        if summary["leaking"] or summary["errors"]:
            ctx._emit(
                job_id,
                "warn",
                "Memory audit completed with "
                f"{summary['leaking']} leaking and {summary['errors']} indeterminate tests.",
            )
        else:
            ctx._emit(job_id, "info", f"Memory audit passed for {summary['total']} tests.")
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        ctx._emit(job_id, "error", str(exc))


def run_selected_memory_audit(
    ctx,
    job_id: str,
    worktree: Path,
    gtest_filter: str,
    *,
    artifact_dir: Path,
) -> dict[str, Any]:
    emit = ctx._job_emit(job_id)
    build_dir = worktree / "build" / "memory-audit"
    _clean_build_dir(worktree, build_dir, emit)

    mapping = ctx._command_mapping(worktree, build_dir, gtest_filter, artifact_dir=artifact_dir)
    configure_command = [
        "cmake",
        "-S",
        str(worktree),
        "-B",
        str(build_dir),
        "-G",
        "Visual Studio 17 2022",
        "-A",
        "x64",
        "-DBUILD_ALL_MODULE=OFF",
        "-DBUILD_DEMO=OFF",
        "-DBUILD_BENCHTEST=OFF",
        "-DBUILD_TEST=ON",
        "-DBUILD_FORMAT=OFF",
        "-DTEST_MEMORY_AUDIT=ON",
    ]
    for option_name in ("develop_module_option", "test_module_option"):
        option = str(mapping.get(option_name) or "").strip()
        if option:
            configure_command.extend(shlex.split(option))

    command_log: list[str] = []
    _require_command_success(configure_command, worktree, emit, command_log, "Memory audit configure")
    _require_command_success(
        ["cmake", "--build", str(build_dir), "--config", "Release", "--target", "tests", "--parallel"],
        worktree,
        emit,
        command_log,
        "Memory audit build",
    )

    test_executable = build_dir / "Release" / "tests.exe"
    if not test_executable.exists():
        raise FileNotFoundError(f"Memory audit test executable does not exist: {test_executable}")

    list_command = [
        str(test_executable),
        "--gtest_list_tests",
        f"--gtest_filter={gtest_filter}",
    ]
    list_code, list_output = _run_command(list_command, worktree, emit)
    command_log.extend([_display_command(list_command), list_output])
    if list_code != 0:
        raise RuntimeError(f"Failed to list memory audit tests with exit code {list_code}\n{list_output[-4000:]}")

    test_names = _parse_gtest_list(list_output)
    if not test_names:
        raise RuntimeError(f"Memory audit filter did not match any tests: {gtest_filter}")

    process_root = build_dir / "check_leak" / "agent"
    process_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, test_name in enumerate(test_names, 1):
        result = _audit_single_test(
            test_executable,
            test_name,
            process_root / f"{index:04d}_{_sanitize_test_name(test_name)}",
            emit,
        )
        results.append(result)
        command_log.append(result["command"])
        command_log.append(result["output"])
        emit(
            "info" if result["memory_status"] in {"passed", "not_applicable"} else "warn",
            f"Memory audit {result['memory_status']}: {test_name} "
            f"(functional={result['functional_status']}, leaks={result['leaks']}, "
            f"bad_delete={result['bad_delete']})",
        )

    summary = _summarize_results(gtest_filter, results)
    _write_audit_artifacts(artifact_dir, summary, command_log)
    return summary


def _audit_single_test(
    test_executable: Path,
    test_name: str,
    process_dir: Path,
    emit: Callable[[str, str], None],
) -> dict[str, Any]:
    if process_dir.exists():
        shutil.rmtree(process_dir)
    process_dir.mkdir(parents=True, exist_ok=True)
    log_path = process_dir / "gme_mmgr.1.log"
    command = [str(test_executable), f"--gtest_filter={test_name}"]
    command_text = _display_command(command)
    emit("cmd", command_text)

    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=str(process_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=600,
        )
        return_code = completed.returncode
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = 124
        stdout = _decoded_timeout_output(exc.stdout)
        stderr = _decoded_timeout_output(exc.stderr)
        output = "\n".join(part for part in (stdout, stderr, "Memory audit test timed out after 600 seconds.") if part)

    functional_status = _functional_status(return_code, output, timed_out)
    leaks: int | None = None
    bad_delete: int | None = None
    memory_status = "error"
    detail = ""
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8-sig", errors="replace")
        leak_match = _LEAK_RE.search(log_text)
        bad_delete_match = _BAD_DELETE_RE.search(log_text)
        if leak_match and bad_delete_match:
            leaks = int(leak_match.group(1))
            bad_delete = int(bad_delete_match.group(1))
            memory_status = "leaking" if leaks > 0 or bad_delete > 0 else "passed"
            detail = str(log_path)
        else:
            detail = f"Memory audit markers are incomplete in {log_path}."
    elif functional_status == "skipped":
        memory_status = "not_applicable"
        detail = "Skipped before the test body initialized the memory audit logger."
    else:
        detail = "gme_mmgr.1.log was not generated."

    return {
        "test": test_name,
        "return_code": return_code,
        "functional_status": functional_status,
        "memory_status": memory_status,
        "leaks": leaks,
        "bad_delete": bad_delete,
        "detail": detail,
        "log_path": str(log_path) if log_path.exists() else "",
        "command": command_text,
        "output": output,
    }


def _functional_status(return_code: int, output: str, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if _SKIPPED_RE.search(output):
        return "skipped"
    if return_code == 0 and not _FAILED_RE.search(output):
        return "passed"
    return "failed"


def _summarize_results(gtest_filter: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "gtest_filter": gtest_filter,
        "total": len(results),
        "passed": sum(item["memory_status"] == "passed" for item in results),
        "leaking": sum(item["memory_status"] == "leaking" for item in results),
        "errors": sum(item["memory_status"] == "error" for item in results),
        "not_applicable": sum(item["memory_status"] == "not_applicable" for item in results),
        "functional_failed": sum(item["functional_status"] == "failed" for item in results),
        "results": results,
    }


def _write_audit_artifacts(artifact_dir: Path, summary: dict[str, Any], command_log: list[str]) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "memory_audit_results.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    with (artifact_dir / "memory_audit_results.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(
            ["test", "functional_status", "memory_status", "leaks", "bad_delete", "return_code", "detail"]
        )
        for item in summary["results"]:
            writer.writerow(
                [
                    item["test"],
                    item["functional_status"],
                    item["memory_status"],
                    item["leaks"] if item["leaks"] is not None else "",
                    item["bad_delete"] if item["bad_delete"] is not None else "",
                    item["return_code"],
                    item["detail"],
                ]
            )
    (artifact_dir / "memory_audit_output.txt").write_text("\n\n".join(command_log), encoding="utf-8")


def _require_command_success(
    command: list[str],
    cwd: Path,
    emit: Callable[[str, str], None],
    command_log: list[str],
    label: str,
) -> None:
    code, output = _run_command(command, cwd, emit)
    command_log.extend([_display_command(command), output])
    if code != 0:
        raise RuntimeError(f"{label} failed with exit code {code}\n{output[-4000:]}")


def _run_command(
    command: list[str],
    cwd: Path,
    emit: Callable[[str, str], None],
) -> tuple[int, str]:
    emit("cmd", _display_command(command))
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    for line in output.splitlines():
        emit("cmd", line)
    return completed.returncode, output


def _parse_gtest_list(output: str) -> list[str]:
    tests: list[str] = []
    current_suite = ""
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        if raw_line[:1].isspace():
            if not current_suite:
                continue
            test_name = raw_line.strip().split("#", 1)[0].strip()
            if test_name:
                tests.append(f"{current_suite}{test_name}")
            continue
        suite = raw_line.strip().split("#", 1)[0].strip()
        current_suite = suite if suite.endswith(".") else ""
    return tests


def _clean_build_dir(worktree: Path, build_dir: Path, emit: Callable[[str, str], None]) -> None:
    if not build_dir.exists():
        return
    resolved_worktree = worktree.resolve()
    resolved_build_dir = build_dir.resolve()
    if resolved_build_dir == resolved_worktree or resolved_worktree not in resolved_build_dir.parents:
        raise RuntimeError(f"Refusing to clean memory audit build directory outside worktree: {resolved_build_dir}")
    emit("info", f"Cleaning memory audit build directory: {build_dir}")
    if build_dir.is_dir():
        shutil.rmtree(build_dir)
    else:
        build_dir.unlink()


def _sanitize_test_name(test_name: str) -> str:
    sanitized = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in test_name)
    return sanitized[:120]


def _display_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def _decoded_timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
