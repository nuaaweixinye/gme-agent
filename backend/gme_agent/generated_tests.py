from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import subprocess

from .git.worktree import normalize_repo_path


GENERATED_TESTS_MANIFEST = ".gme-agent/generated_tests.json"
UTF8_BOM = b"\xef\xbb\xbf"


def load_generated_tests_manifest(worktree: str | Path, target_repo: str = "tests/gme") -> dict[str, Any]:
    path = generated_tests_manifest_path(worktree)
    if not path.exists():
        return {"tests": [], "files": [], "gtest_filter": ""}

    content = path.read_bytes()
    has_bom = content.startswith(UTF8_BOM)
    raw = json.loads(content.decode("utf-8-sig"))
    if has_bom:
        path.write_bytes(content[len(UTF8_BOM) :])
    if isinstance(raw, list):
        raw_tests = raw
    elif isinstance(raw, dict):
        raw_tests = raw.get("tests") or []
    else:
        raw_tests = []

    tests: list[dict[str, Any]] = []
    for item in raw_tests:
        if not isinstance(item, dict):
            continue
        file_path = _normalize_manifest_file(str(item.get("file") or item.get("path") or ""), target_repo)
        suite = str(item.get("suite") or item.get("test_suite") or "").strip()
        name = str(item.get("name") or item.get("test_name") or "").strip()
        if not file_path or not suite or not name:
            continue
        tests.append(
            {
                "file": file_path,
                "suite": suite,
                "name": name,
                "api": str(item.get("api") or ""),
                "anchor": str(item.get("anchor") or ""),
                "notes": str(item.get("notes") or ""),
                "interface_id": str(item.get("interface_id") or "").strip(),
                "gap_id": str(item.get("gap_id") or "").strip(),
                "scenario": str(item.get("scenario") or "").strip(),
            }
        )

    files = sorted({test["file"] for test in tests})
    gtest_filter = ":".join(f"{test['suite']}.{test['name']}" for test in tests)
    return {"tests": tests, "files": files, "gtest_filter": gtest_filter}


def require_generated_tests_manifest(
    worktree: str | Path,
    target_repo: str = "tests/gme",
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    if not generated_tests_manifest_path(worktree).is_file():
        raise RuntimeError(f"The agent must write {GENERATED_TESTS_MANIFEST}")
    manifest = load_generated_tests_manifest(worktree, target_repo)
    if not allow_empty and not manifest["tests"]:
        raise RuntimeError(
            f"No generated test manifest entries were found. "
            f"The agent must write {GENERATED_TESTS_MANIFEST} with each added test file, suite, and name."
        )
    return manifest


def ensure_generated_tests_use_existing_files(worktree: str | Path, target_repo: str, files: list[str]) -> None:
    repo = Path(worktree) / normalize_repo_path(target_repo)
    missing: list[str] = []
    new_files: list[str] = []
    for rel_path in files:
        rel = normalize_repo_path(rel_path)
        if not (repo / rel).exists():
            missing.append(rel)
            continue
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{rel}"],
            cwd=str(repo),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            new_files.append(rel)
    if missing:
        raise RuntimeError("Generated test manifest points to missing test files: " + ", ".join(missing))
    if new_files:
        raise RuntimeError(
            "Generated tests must be inserted into existing test files, not new .cpp files: " + ", ".join(new_files)
        )


def ensure_generated_tests_use_selected_files(
    manifest: dict[str, Any],
    target_repo: str,
    selected_files: list[str],
    *,
    previous_entries: set[tuple[str, str, str]] | None = None,
) -> None:
    allowed = {
        normalized
        for value in selected_files
        if (normalized := _normalize_manifest_file(str(value or ""), target_repo))
    }
    if not allowed:
        raise RuntimeError("Structured test generation has no valid selected target files")

    unexpected: set[str] = set()
    for test in manifest.get("tests") or []:
        key = (
            str(test.get("file") or ""),
            str(test.get("suite") or ""),
            str(test.get("name") or ""),
        )
        if previous_entries is not None and key in previous_entries:
            continue
        file_path = str(test.get("file") or "")
        if file_path not in allowed:
            unexpected.add(file_path or "<missing>")
    if unexpected:
        raise RuntimeError(
            "Generated tests must stay in the selected target files: " + ", ".join(sorted(unexpected))
        )


def generated_test_entry_keys(manifest: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (
            str(test.get("file") or ""),
            str(test.get("suite") or ""),
            str(test.get("name") or ""),
        )
        for test in manifest.get("tests") or []
    }


def generated_tests_manifest_path(worktree: str | Path) -> Path:
    return Path(worktree) / GENERATED_TESTS_MANIFEST


def generated_test_keys(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    return {(str(test.get("suite") or ""), str(test.get("name") or "")) for test in manifest.get("tests") or []}


def generated_test_files(manifest: dict[str, Any]) -> list[str]:
    return [str(path) for path in manifest.get("files") or []]


def generated_test_filter(manifest: dict[str, Any]) -> str:
    return str(manifest.get("gtest_filter") or "")


def _normalize_manifest_file(value: str, target_repo: str) -> str:
    path = normalize_repo_path(value)
    if path == ".":
        return ""
    target = normalize_repo_path(target_repo)
    if path == target:
        return ""
    prefix = f"{target}/"
    if path.startswith(prefix):
        path = path[len(prefix) :]
    if not path.endswith((".cpp", ".cxx", ".cc")):
        return ""
    if not path.startswith("src/"):
        return ""
    return path
