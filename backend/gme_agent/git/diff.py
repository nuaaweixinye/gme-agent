from __future__ import annotations

from pathlib import Path
from typing import Iterable
import re
import shutil
import subprocess

from ..settings.config import AgentConfig
from .worktree import EventCallback, is_git_repo, normalize_repo_path, run_git


IGNORED_WORKTREE_ARTIFACT_PATTERNS = ("timer_res",)
PR_URL_PATTERN = re.compile(r"https?://[^\s]+/pull/\d+(?:[^\s]*)?")


def ensure_only_target_repo_changed(
    worktree: str | Path,
    target_rel_path: str,
    *,
    allowed_support_paths: Iterable[str] | None = None,
) -> None:
    worktree_path = Path(worktree)
    target_rel = normalize_repo_path(target_rel_path)
    support_paths = {normalize_repo_path(path) for path in (allowed_support_paths or [])}
    support_paths.discard(".")
    support_paths.discard(target_rel)
    parent_status = git_status(worktree_path)
    unexpected = []
    for line in parent_status.splitlines():
        path = _status_path(line)
        if not path:
            continue
        norm = normalize_repo_path(path)
        if _is_ignored_worktree_artifact(norm):
            continue
        if target_rel != "." and (norm == target_rel or norm.startswith(f"{target_rel}/")):
            continue
        support_path = _matching_support_path(norm, support_paths)
        if support_path:
            support_repo = worktree_path / support_path
            if is_git_repo(support_repo):
                nested_status = git_status(support_repo)
                if nested_status.strip():
                    unexpected.append(f"{line}\nNested changes under {support_path}:\n{nested_status.rstrip()}")
            continue
        unexpected.append(line)

    if unexpected:
        raise RuntimeError(
            "The agent changed files outside the target repository. "
            f"Target repository: {target_rel}. Unexpected changes:\n" + "\n".join(unexpected)
        )


def git_diff(path: str | Path, emit: EventCallback | None = None) -> str:
    repo = Path(path)
    chunks: list[str] = []

    # Histogram diff aligns repetitive TEST_F blocks more reliably, avoiding
    # large false deletion counts when generated tests are inserted between them.
    tracked_diff = run_git(["diff", "--histogram", "--", "."], repo, emit)
    if tracked_diff.strip():
        chunks.append(tracked_diff.rstrip("\r\n"))

    untracked = run_git(["ls-files", "--others", "--exclude-standard"], repo, emit)
    for rel_path in [line.strip() for line in untracked.splitlines() if line.strip()]:
        cmd = ["git", "diff", "--no-index", "--", "/dev/null", rel_path]
        if emit:
            emit("cmd", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode not in (0, 1):
            output = (proc.stdout + proc.stderr).strip()
            raise RuntimeError(output or f"git failed: {' '.join(cmd)}")
        if proc.stdout.strip():
            chunks.append(proc.stdout.rstrip("\r\n"))

    return "\n".join(chunks) + ("\n" if chunks else "")


def git_status(path: str | Path, emit: EventCallback | None = None) -> str:
    return run_git(["status", "--short"], path, emit)


def commit_all(path: str | Path, message: str, emit: EventCallback) -> None:
    run_git(["add", "-A"], path, emit)
    status = git_status(path, emit)
    if not status.strip():
        emit("info", "No changes to commit.")
        return
    run_git(["commit", "-m", message], path, emit)


def commit_paths(path: str | Path, rel_paths: Iterable[str], message: str, emit: EventCallback) -> None:
    paths = [normalize_repo_path(rel_path) for rel_path in rel_paths if normalize_repo_path(rel_path) != "."]
    if not paths:
        raise RuntimeError("No paths were selected for commit.")
    run_git(["add", "--", *paths], path, emit)
    status = run_git(["status", "--short", "--", *paths], path, emit)
    if not status.strip():
        raise RuntimeError("No skip changes were found in the selected generated test files.")
    run_git(["commit", "-m", message, "--", *paths], path, emit)


def push_branch(config: AgentConfig, path: str | Path, branch: str, emit: EventCallback) -> None:
    run_git(["push", "-u", config.github_remote, branch], path, emit)


def create_pr(
    config: AgentConfig,
    path: str | Path,
    title: str,
    body: str,
    emit: EventCallback,
    *,
    base_branch: str | None = None,
) -> str:
    if shutil.which("gh") is None:
        raise RuntimeError("GitHub CLI 'gh' was not found on PATH.")
    cmd = [
        "gh",
        "pr",
        "create",
        "--base",
        base_branch or config.base_branch,
        "--title",
        title,
        "--body",
        body,
    ]
    emit("cmd", " ".join(cmd[:6]) + " ...")
    proc = subprocess.run(
        cmd,
        cwd=str(path),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = "\n".join(
        part.strip()
        for part in (getattr(proc, "stdout", ""), getattr(proc, "stderr", ""))
        if part and part.strip()
    )
    if output:
        emit("cmd", output)
    if proc.returncode != 0:
        raise RuntimeError(output or "gh pr create failed")
    matches = PR_URL_PATTERN.findall(output)
    if not matches:
        raise RuntimeError("gh pr create succeeded but did not return a pull request URL.")
    return matches[-1].rstrip(".,;)")


def _status_path(line: str) -> str:
    if not line:
        return ""
    value = line[3:] if len(line) > 3 else ""
    if " -> " in value:
        value = value.split(" -> ", 1)[1]
    return value.strip()


def _matching_support_path(path: str, support_paths: set[str]) -> str:
    for support_path in sorted(support_paths, key=len, reverse=True):
        if path == support_path or path.startswith(f"{support_path}/"):
            return support_path
    return ""


def _is_ignored_worktree_artifact(path: str) -> bool:
    norm = normalize_repo_path(path)
    if "/" in norm or norm == ".":
        return False
    name = Path(norm).name
    return name.endswith(".csv") and any(name.startswith(prefix) for prefix in IGNORED_WORKTREE_ARTIFACT_PATTERNS)
