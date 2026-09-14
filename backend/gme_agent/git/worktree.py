from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import subprocess
import time

from ..settings.config import AgentConfig


EventCallback = Callable[[str, str], None]


def run_git(args: list[str], cwd: str | Path, emit: EventCallback | None = None) -> str:
    cmd = ["git", *args]
    if emit:
        emit("cmd", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if emit and proc.stdout.strip():
        emit("cmd", proc.stdout.strip())
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout.strip() or f"git failed: {' '.join(cmd)}")
    return proc.stdout


def run_git_with_retry(
    args: list[str],
    cwd: str | Path,
    emit: EventCallback | None = None,
    *,
    attempts: int = 3,
    delay_seconds: float = 2.0,
) -> str:
    last_error: RuntimeError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return run_git(args, cwd, emit)
        except RuntimeError as exc:
            last_error = exc
            if attempt >= max(1, attempts):
                raise
            if emit:
                emit("warn", f"Git command failed ({attempt}/{attempts}); retrying in {delay_seconds * attempt:g}s.")
            time.sleep(delay_seconds * attempt)
    raise last_error or RuntimeError("Git command failed without an error message.")


def normalize_repo_path(path: str | Path | None) -> str:
    value = str(path or ".").replace("\\", "/").strip("/")
    return value or "."


def is_git_repo(path: Path) -> bool:
    if not (path / ".git").exists():
        return False
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(path),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def origin_remote(repo: Path) -> str:
    try:
        remotes = {line.strip() for line in run_git(["remote"], repo).splitlines() if line.strip()}
    except RuntimeError:
        return ""
    return "origin" if "origin" in remotes else (sorted(remotes)[0] if remotes else "")


def remote_ref_exists(repo: Path, remote: str, branch: str) -> bool:
    try:
        run_git(["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"], repo)
        return True
    except RuntimeError:
        return False


@dataclass(slots=True)
class WorktreeInfo:
    branch: str
    path: Path


def create_worktree(config: AgentConfig, job_id: str, prefix: str, emit: EventCallback) -> WorktreeInfo:
    repo = Path(config.gme_repo_path)
    root = Path(config.worktree_root)
    root.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_prefix = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in prefix).strip("-")
    branch = f"gme-agent/{safe_prefix}-{stamp}-{job_id[:8]}"
    path = root / f"{safe_prefix}-{stamp}-{job_id[:8]}"

    if path.exists():
        raise RuntimeError(f"Worktree path already exists: {path}")

    emit("info", f"Creating worktree {path} from local {config.base_branch}")
    run_git(["worktree", "add", "-b", branch, str(path), config.base_branch], repo, emit)
    _sync_superproject_worktree(config, path, emit)
    emit("info", "Created task worktree; dependencies will be prepared with the local-cache-first workflow.")
    return WorktreeInfo(branch=branch, path=path)


def create_remote_worktree(config: AgentConfig, job_id: str, prefix: str, emit: EventCallback) -> WorktreeInfo:
    repo = Path(config.gme_repo_path)
    root = Path(config.worktree_root)
    root.mkdir(parents=True, exist_ok=True)

    remote = (config.github_remote or "origin").strip()
    base_branch = (config.base_branch or "main").strip()
    remotes = {line.strip() for line in run_git(["remote"], repo).splitlines() if line.strip()}
    if remote not in remotes:
        raise RuntimeError(f"Git remote '{remote}' was not found in GME repository: {repo}")

    emit("info", f"Fetching latest GME base {remote}/{base_branch}.")
    run_git_with_retry(["fetch", remote, base_branch], repo, emit)
    remote_ref = f"{remote}/{base_branch}"
    if not remote_ref_exists(repo, remote, base_branch):
        raise RuntimeError(f"GME base branch was not found: {remote_ref}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_prefix = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in prefix).strip("-")
    suffix = str(time.time_ns())[-6:]
    branch = f"gme-agent/{safe_prefix}-{stamp}-{job_id[:8]}-{suffix}"
    path = root / f"{safe_prefix}-{stamp}-{job_id[:8]}-{suffix}"
    if path.exists():
        raise RuntimeError(f"Worktree path already exists: {path}")

    emit("info", f"Creating isolated PR verification worktree {path} from {remote_ref}.")
    run_git(["worktree", "add", "-b", branch, str(path), remote_ref], repo, emit)
    return WorktreeInfo(branch=branch, path=path)


def remove_worktree(config: AgentConfig, path: str | Path, emit: EventCallback) -> None:
    target = Path(path).resolve()
    root = Path(config.worktree_root).resolve()
    if target == root or root not in target.parents:
        raise RuntimeError(f"Refusing to remove worktree outside configured root: {target}")
    run_git(["worktree", "remove", "--force", str(target)], config.gme_repo_path, emit)


def _sync_superproject_worktree(config: AgentConfig, worktree: Path, emit: EventCallback) -> None:
    remote = (config.github_remote or "").strip()
    if not remote:
        return

    try:
        remotes = {line.strip() for line in run_git(["remote"], worktree).splitlines() if line.strip()}
    except RuntimeError:
        remotes = set()
    if remote not in remotes:
        emit("warn", f"Git remote '{remote}' was not found; keeping local {config.base_branch}.")
        return

    emit("info", f"Updating task GME worktree to {remote}/{config.base_branch}.")
    run_git_with_retry(["fetch", remote, config.base_branch], worktree, emit)
    run_git(["reset", "--hard", f"{remote}/{config.base_branch}"], worktree, emit)
