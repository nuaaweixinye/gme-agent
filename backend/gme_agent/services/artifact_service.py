from __future__ import annotations

from pathlib import Path
import json

from ..settings.config import AgentConfig
from ..git.diff import git_diff


def artifact_dir_for_job(config: AgentConfig, job_id: str) -> Path:
    path = Path(config.artifact_root) / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_job_artifacts(ctx, job_id: str, worktree: Path, artifact_dir: Path) -> None:
    emit = ctx._job_emit(job_id)
    job = ctx.db.get_job(job_id)
    target_repo = ctx._job_target_repo(job)
    target_path = ctx._target_repo_path(worktree, target_repo)
    diff = git_diff(target_path, emit)
    (artifact_dir / "diff.patch").write_text(diff, encoding="utf-8")
    manifest = {
        "job": job,
        "artifact_dir": str(artifact_dir),
        "worktree_path": str(worktree),
        "target_repo": target_repo,
        "target_repo_path": str(target_path),
        "failures": [f for f in ctx.db.list_failures() if f.get("job_id") == job_id],
        "test_results": ctx.db.list_test_case_results(job_id),
    }
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
