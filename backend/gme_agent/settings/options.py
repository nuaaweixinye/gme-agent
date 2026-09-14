from __future__ import annotations

from pathlib import Path
import subprocess

from .config import AgentConfig
from ..git.worktree import normalize_repo_path
from ..runtime import skill_root


def load_config_options(config: AgentConfig) -> dict:
    repo = Path(config.gme_repo_path)
    submodules = _load_submodules(repo)
    module_root = normalize_repo_path(config.module_repo_root)
    module_repos = sorted(item["path"] for item in submodules if item["path"].startswith(f"{module_root}/"))
    modules = sorted(path.split("/", 1)[1] for path in module_repos if "/" in path)
    test_repos = sorted(item["path"] for item in submodules if item["path"].startswith("tests/"))
    module_roots = sorted({path.split("/", 1)[0] for path in module_repos}) or [module_root]

    return {
        "branches": _git_lines(repo, ["branch", "--format=%(refname:short)"]),
        "remotes": _git_lines(repo, ["remote"]),
        "submodules": submodules,
        "test_repos": test_repos,
        "module_roots": module_roots,
        "module_repos": module_repos,
        "modules": modules,
        "reasoning_effort_options": ["", "low", "medium", "high", "xhigh", "max", "ultra"],
        "pr_strategy_options": ["target_repo_only"],
        "builtin_skills": _builtin_skills(),
    }


def _load_submodules(repo: Path) -> list[dict[str, str]]:
    if not (repo / ".gitmodules").exists():
        return []
    lines = _git_lines(repo, ["config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|branch)$"])
    by_name: dict[str, dict[str, str]] = {}
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        key, value = parts
        if not key.startswith("submodule.") or "." not in key[len("submodule.") :]:
            continue
        name_and_field = key[len("submodule.") :]
        name, field = name_and_field.rsplit(".", 1)
        item = by_name.setdefault(name, {"name": name, "path": "", "branch": ""})
        if field == "path":
            item["path"] = normalize_repo_path(value)
        elif field == "branch":
            item["branch"] = value.strip()
    return sorted((item for item in by_name.values() if item["path"]), key=lambda item: item["path"])


def _git_lines(repo: Path, args: list[str]) -> list[str]:
    if not repo.exists():
        return []
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _builtin_skills() -> list[str]:
    root = skill_root()
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if (path / "SKILL.md").exists())
