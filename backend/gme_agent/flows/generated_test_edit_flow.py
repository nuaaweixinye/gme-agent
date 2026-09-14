from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

from ..generated_tests import (
    generated_test_filter,
    generated_tests_manifest_path,
    load_generated_tests_manifest,
    require_generated_tests_manifest,
)
from ..git.worktree import normalize_repo_path


_TEST_DECL_RE = re.compile(
    r"(?m)^[ \t]*(TEST|TEST_F|TEST_P|TYPED_TEST|TYPED_TEST_P)\s*\(\s*([^,\s]+)\s*,\s*([^)]+?)\s*\)\s*\{"
)


def delete_generated_tests(ctx, job_id: str, tests: list[dict[str, str]]) -> dict[str, Any]:
    job = ctx.db.get_job(job_id)
    if job.get("type") != "test_generation":
        raise ValueError("Only test-generation jobs can delete generated tests.")
    worktree_value = str(job.get("worktree_path") or "")
    if not worktree_value:
        raise RuntimeError("Selected job has no worktree path.")
    worktree = Path(worktree_value)
    if not worktree.exists():
        raise RuntimeError(f"Selected job worktree does not exist: {worktree}")

    selected = _selected_test_keys(tests)
    if not selected:
        raise ValueError("No generated tests were selected.")

    target_repo = ctx._job_target_repo(job)
    target_path = ctx._target_repo_path(worktree, target_repo)
    manifest = require_generated_tests_manifest(worktree, target_repo)
    manifest_tests = list(manifest.get("tests") or [])
    manifest_keys = {(item["suite"], item["name"]) for item in manifest_tests}
    missing = selected - manifest_keys
    if missing:
        names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing))
        raise RuntimeError(f"Selected tests are not listed in generated_tests.json: {names}")

    by_file: dict[str, set[tuple[str, str]]] = {}
    for item in manifest_tests:
        key = (item["suite"], item["name"])
        if key in selected:
            by_file.setdefault(item["file"], set()).add(key)

    emit = ctx._job_emit(job_id)
    for rel_path, remove_tests in sorted(by_file.items()):
        file_path = target_path / normalize_repo_path(rel_path)
        if not file_path.exists():
            raise RuntimeError(f"Generated test file was not found: {rel_path}")
        text = file_path.read_text(encoding="utf-8", errors="replace")
        updated = _remove_test_blocks(text, remove_tests, rel_path)
        file_path.write_text(updated, encoding="utf-8")
        emit("info", f"Deleted {len(remove_tests)} generated test(s) from {rel_path}.")

    remaining_tests = [item for item in manifest_tests if (item["suite"], item["name"]) not in selected]
    _write_generated_tests_manifest(worktree, remaining_tests)
    _write_generated_tests_notes(worktree, target_repo, str(job.get("module") or ""), remaining_tests)
    deleted_failures = ctx.db.delete_failures_for_tests(job_id, sorted(selected))
    if deleted_failures:
        emit("info", f"Removed {deleted_failures} failure record(s) for deleted generated tests.")
    deleted_results = ctx.db.delete_test_case_results_for_tests(job_id, sorted(selected))
    if deleted_results:
        emit("info", f"Removed {deleted_results} persisted test result(s) for deleted generated tests.")

    refreshed_manifest = load_generated_tests_manifest(worktree, target_repo)
    metadata = ctx._merge_metadata(
        job_id,
        {
            "generated_tests": refreshed_manifest["tests"],
            "generated_test_files": refreshed_manifest["files"],
            "generated_gtest_filter": generated_test_filter(refreshed_manifest),
        },
    )
    updated_job = ctx.db.update_job(job_id, status="needs_review", metadata=metadata, error=None)
    ctx._write_job_artifacts(job_id, worktree, ctx._artifact_dir(job_id))
    emit("info", f"Deleted {len(selected)} selected generated test(s).")
    return updated_job


def _selected_test_keys(tests: list[dict[str, str]]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for item in tests or []:
        if not isinstance(item, dict):
            continue
        suite = str(item.get("suite") or item.get("test_suite") or "").strip()
        name = str(item.get("name") or item.get("test_name") or "").strip()
        if suite and name:
            result.add((suite, name))
    return result


def _write_generated_tests_manifest(worktree: Path, tests: list[dict[str, str]]) -> None:
    path = generated_tests_manifest_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tests": tests}, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_generated_tests_notes(worktree: Path, target_repo: str, module: str, tests: list[dict[str, str]]) -> None:
    path = worktree / ".gme-agent" / "generated_tests.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Generated Tests: {module or 'module'}",
        "",
        "## Generated Test Cases",
        "",
    ]
    if tests:
        for index, item in enumerate(tests, 1):
            lines.extend(
                [
                    f"{index}. `{item['suite']}.{item['name']}`",
                    f"   - File: `{normalize_repo_path(target_repo)}/{item['file']}`",
                    f"   - API: `{item.get('api') or ''}`",
                    f"   - Anchor: `{item.get('anchor') or ''}`",
                ]
            )
    else:
        lines.append("No generated tests remain.")
    lines.extend(["", "## Suggested GTest Filter", "", "`" + ":".join(f"{item['suite']}.{item['name']}" for item in tests) + "`", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _remove_test_blocks(text: str, remove_tests: set[tuple[str, str]], rel_path: str) -> str:
    if not remove_tests:
        return text
    chunks: list[str] = []
    last = 0
    removed: set[tuple[str, str]] = set()
    for start, end, suite, name in _test_blocks(text, rel_path):
        key = (suite, name)
        if key not in remove_tests:
            continue
        block_start = _include_leading_doc_comment(text, start)
        chunks.append(text[last:block_start])
        last = _include_trailing_blank_line(text, end)
        removed.add(key)
    chunks.append(text[last:])
    missing = remove_tests - removed
    if missing:
        names = ", ".join(f"{suite}.{name}" for suite, name in sorted(missing))
        raise RuntimeError(f"Could not find generated tests to delete from {rel_path}: {names}")
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
            raise RuntimeError(f"Could not find test body end for {_clean_name(match.group(2))}.{_clean_name(match.group(3))} in {rel_path}.")
        blocks.append((match.start(), end + 1, _clean_name(match.group(2)), _clean_name(match.group(3))))
        pos = end + 1


def _include_leading_doc_comment(text: str, start: int) -> int:
    cursor = start
    while cursor > 0 and text[cursor - 1] in " \t\r\n":
        cursor -= 1
    prefix = text[:cursor].rstrip()
    if not prefix.endswith("*/"):
        return start
    comment_end = len(prefix)
    comment_start = text.rfind("/*", 0, comment_end)
    if comment_start < 0:
        return start
    between = text[comment_start:comment_end]
    if "TEST_" in between or "TEST(" in between:
        return start
    return comment_start


def _include_trailing_blank_line(text: str, end: int) -> int:
    cursor = end
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    if cursor < len(text) and text[cursor] == "\r":
        cursor += 1
    if cursor < len(text) and text[cursor] == "\n":
        cursor += 1
    return cursor


def _clean_name(value: str) -> str:
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
