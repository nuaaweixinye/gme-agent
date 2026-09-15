from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


DEFAULT_KNOWLEDGE_BASES: tuple[dict[str, str], ...] = (
    {"label": "kb00", "id": "<kb00-knowledge-base-id>"},
    {"label": "kb01", "id": "<kb01-knowledge-base-id>"},
    {"label": "kb02", "id": "<kb02-knowledge-base-id>"},
    {"label": "kb03", "id": "<kb03-knowledge-base-id>"},
)

# Deployment-specific: point this at a real WeKnora deployment in config.local.json
# (the file git ignores). An empty base URL degrades every source with a clear
# message instead of guessing an address.
DEFAULT_WEKNORA_BASE_URL = ""


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        # YAML-style spellings are accepted too, so a hand-edited config that
        # writes "yes"/"on"/"no"/"off" switches the flag as its author intended.
        # Anything else is malformed and degrades to the caller's default.
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return fallback


def _as_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # json.loads accepts the non-standard NaN/Infinity literals, and
        # int(nan)/int(inf) raise (ValueError/OverflowError). A hand-edited
        # config must degrade, never crash load_config at startup.
        try:
            return int(value)
        except (ValueError, OverflowError):
            return fallback
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return fallback
    return fallback


def _as_str(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


@dataclass(slots=True)
class KnowledgeBudgetConfig:
    max_priors: int = 8
    max_kb_hits: int = 6
    max_chars: int = 4000

    @classmethod
    def from_dict(cls, raw: Any) -> "KnowledgeBudgetConfig":
        data = raw if isinstance(raw, dict) else {}
        defaults = cls()
        return cls(
            max_priors=max(_as_int(data.get("max_priors"), defaults.max_priors), 0),
            max_kb_hits=max(_as_int(data.get("max_kb_hits"), defaults.max_kb_hits), 0),
            max_chars=max(_as_int(data.get("max_chars"), defaults.max_chars), 0),
        )


@dataclass(slots=True)
class ClosedLoopConfig:
    enabled: bool = True
    min_stable_runs: int = 2

    @classmethod
    def from_dict(cls, raw: Any) -> "ClosedLoopConfig":
        data = raw if isinstance(raw, dict) else {}
        defaults = cls()
        return cls(
            enabled=_as_bool(data.get("enabled"), defaults.enabled),
            min_stable_runs=max(_as_int(data.get("min_stable_runs"), defaults.min_stable_runs), 1),
        )


@dataclass(slots=True)
class WeKnoraConfig:
    base_url: str = DEFAULT_WEKNORA_BASE_URL
    api_key_env: str = "WEKNORA_API_KEY"
    timeout_ms: int = 3000
    knowledge_bases: list[dict[str, str]] = field(
        default_factory=lambda: [dict(item) for item in DEFAULT_KNOWLEDGE_BASES]
    )

    @classmethod
    def from_dict(cls, raw: Any) -> "WeKnoraConfig":
        data = raw if isinstance(raw, dict) else {}
        defaults = cls()
        bases: list[dict[str, str]] = []
        configured = data.get("knowledge_bases")
        configured = configured if isinstance(configured, list) else []
        for item in configured:
            if not isinstance(item, dict):
                continue
            label = _as_str(item.get("label"), "")
            knowledge_id = _as_str(item.get("id"), "")
            if label and knowledge_id:
                bases.append({"label": label, "id": knowledge_id})
        return cls(
            base_url=_as_str(data.get("base_url"), defaults.base_url).rstrip("/"),
            api_key_env=_as_str(data.get("api_key_env"), defaults.api_key_env),
            timeout_ms=max(_as_int(data.get("timeout_ms"), defaults.timeout_ms), 1),
            knowledge_bases=bases or defaults.knowledge_bases,
        )

    def source_ids(self) -> dict[str, str]:
        return {item["label"]: item["id"] for item in self.knowledge_bases}


@dataclass(slots=True)
class KnowledgeConfig:
    enabled: bool = False
    weknora: WeKnoraConfig = field(default_factory=WeKnoraConfig)
    budgets: KnowledgeBudgetConfig = field(default_factory=KnowledgeBudgetConfig)
    closed_loop: ClosedLoopConfig = field(default_factory=ClosedLoopConfig)

    @classmethod
    def from_dict(cls, raw: Any) -> "KnowledgeConfig":
        data = raw if isinstance(raw, dict) else {}
        defaults = cls()
        return cls(
            enabled=_as_bool(data.get("enabled"), defaults.enabled),
            weknora=WeKnoraConfig.from_dict(data.get("weknora")),
            budgets=KnowledgeBudgetConfig.from_dict(data.get("budgets")),
            closed_loop=ClosedLoopConfig.from_dict(data.get("closed_loop")),
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
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)

    def resolved(self) -> "AgentConfig":
        data = asdict(self)
        for key in ("gme_repo_path", "worktree_root", "artifact_root", "database_path", "dsh_home"):
            # Anchor relative roots against the backend working directory so
            # git (cwd = GME repo) and Python (cwd = backend) resolve the
            # same absolute worktree paths.
            data[key] = str(Path(data[key]).expanduser().resolve())
        data["knowledge"] = KnowledgeConfig.from_dict(data.get("knowledge"))
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
