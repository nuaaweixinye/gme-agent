from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import json


GME_MODE_DEFINE_PREFIXES = (
    "-DGME_FULL_MODE=",
    "-DGME_HUDONG_MODE=",
    "-DGME_YUNJI_MODE=",
    "-DGME_HAIZHOU_MODE=",
    "-DGME_IFGTC_MODE=",
)

DEFAULT_CONFIGURE_COMMAND = (
    'cmake -S {worktree} -B {build_dir} -G "Visual Studio 17 2022" -A x64 '
    "-DBUILD_ALL_MODULE=OFF -DBUILD_DEMO=OFF -DBUILD_BENCHTEST=OFF "
    "-DBUILD_TEST=ON -DBUILD_FORMAT=OFF {develop_module_option} {test_module_option}"
)


@dataclass(slots=True)
class AgentConfig:
    gme_repo_path: str = "C:/path/to/GME"
    worktree_root: str = "./worktrees"
    artifact_root: str = "./artifacts"
    database_path: str = "./gme_agent.db"
    base_branch: str = "main"
    test_base_branch: str = "main"
    module_base_branch: str = "develop"
    github_remote: str = "origin"
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    reasoning_effort: str = ""
    dsh_home: str = str(Path.home() / ".dsh")
    dsh_profile: str = "sdk"
    dsh_bin: str = ""
    agent_enabled: bool = True
    initialize_submodules: bool = False
    test_target_repo: str = "tests/gme"
    module_repo_root: str = "module"
    pr_strategy: str = "target_repo_only"
    use_builtin_skills: bool = True
    test_generation_skill: str = "gme-test-generation"
    bug_fix_skill: str = "gme-bug-fix"
    auto_run_build: bool = False
    auto_run_tests: bool = False
    auto_apply_skips: bool = False
    auto_rerun_after_skip: bool = False
    auto_create_pr: bool = False
    configure_command: str = DEFAULT_CONFIGURE_COMMAND
    build_command: str = "cmake --build {build_dir} --config Debug --target tests --parallel"
    test_command: str = "{test_executable} --gtest_filter={gtest_filter} --gtest_output=xml:{gtest_xml_path}"
    test_executable: str = "{build_dir}/Debug/tests.exe"
    gtest_xml_path: str = "{artifact_dir}/gtest.xml"

    def resolved(self) -> "AgentConfig":
        data = asdict(self)
        for key in ("gme_repo_path", "worktree_root", "artifact_root", "database_path", "dsh_home"):
            # Anchor relative roots against the backend working directory so
            # git (cwd = GME repo) and Python (cwd = backend) resolve the
            # same absolute worktree paths.
            data[key] = str(Path(data[key]).expanduser().resolve())
        return AgentConfig(**data)


def load_config(path: Path) -> AgentConfig:
    if not path.exists():
        cfg = AgentConfig()
        save_config(path, cfg)
        return cfg

    raw = json.loads(path.read_text(encoding="utf-8"))
    defaults = asdict(AgentConfig())
    defaults.update({k: v for k, v in raw.items() if k in defaults})
    defaults = _normalize_command_templates(defaults)
    return AgentConfig(**defaults).resolved()


def save_config(path: Path, config: AgentConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


def config_from_json(data: dict[str, Any], current: AgentConfig) -> AgentConfig:
    merged = asdict(current)
    for key, value in data.items():
        if key in merged:
            merged[key] = value
    merged = _normalize_command_templates(merged)
    return AgentConfig(**merged).resolved()


def _normalize_command_templates(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(data)
    for field in ("configure_command", "build_command", "test_command", "test_executable", "gtest_xml_path"):
        value = cleaned.get(field)
        if isinstance(value, str):
            cleaned[field] = " ".join(value.replace("-DFORCE_RUN_ALL={force_run_all}", "").split())
    configure = cleaned.get("configure_command")
    if isinstance(configure, str):
        cleaned["configure_command"] = _remove_legacy_gme_mode_overrides(configure)
    return cleaned


def _remove_legacy_gme_mode_overrides(command: str) -> str:
    parts = [part for part in command.split() if not part.startswith(GME_MODE_DEFINE_PREFIXES)]
    return " ".join(parts)
