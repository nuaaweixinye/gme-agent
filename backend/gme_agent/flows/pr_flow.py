from __future__ import annotations

from pathlib import Path

from ..git.diff import commit_all, create_pr, push_branch
from ..git.worktree import remove_worktree
from ..services.job_service import job_worktree_paths


def run_cleanup_job(ctx, job_id: str) -> None:
    emit = ctx._job_emit(job_id)
    job = ctx.db.get_job(job_id)
    paths = job_worktree_paths(ctx, job)
    if not paths:
        emit("warn", "Job has no worktree to clean.")
        return
    try:
        ctx.db.update_job(job_id, status="cleaning_worktree")
        for path in paths:
            remove_worktree(ctx.config, path, emit)
        metadata = ctx._merge_metadata(job_id, {"cleaned_worktree_path": str(paths[0]), "cleaned_worktree_paths": [str(path) for path in paths]})
        ctx.db.update_job(job_id, status="worktree_cleaned", worktree_path="", metadata=metadata)
        emit("info", "Removed worktree " + ", ".join(str(path) for path in paths))
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        emit("error", str(exc))


def run_pr_job(ctx, job_id: str) -> None:
    emit = ctx._job_emit(job_id)
    job = ctx.db.get_job(job_id)
    path = job.get("worktree_path")
    branch = job.get("branch")
    if not path or not branch:
        emit("error", "Job is missing worktree or branch.")
        return
    try:
        ctx.db.update_job(job_id, status="creating_pr")
        worktree = Path(path)
        ctx._write_job_artifacts(job_id, worktree, ctx._artifact_dir(job_id))
        target_repo = ctx._job_target_repo(job)
        target_path = ctx._target_repo_path(worktree, target_repo)
        target_branch = str(job.get("metadata", {}).get("target_branch") or branch)
        target_base_branch = str(job.get("metadata", {}).get("target_base_branch") or ctx.config.base_branch)
        commit_all(target_path, job["title"], emit)
        push_branch(ctx.config, target_path, target_branch, emit)
        pr_url = create_pr(ctx.config, target_path, job["title"], ctx._pr_body(job), emit, base_branch=target_base_branch)
        metadata = dict(job.get("metadata") or {})
        metadata["pr_url"] = pr_url
        ctx.db.update_job(job_id, status="pr_created", metadata=metadata)
    except Exception as exc:
        ctx.db.update_job(job_id, status="failed", error=str(exc))
        emit("error", str(exc))
