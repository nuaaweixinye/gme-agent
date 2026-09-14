from __future__ import annotations

import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from ..runtime import skill_root
from ..settings.config import AgentConfig


EventCallback = Callable[[str, str], None]

STDERR_TAIL_CHARACTERS = 4000
EVENT_TAIL_CHARACTERS = 2000

ACCEPTED_FINISH_REASONS = ("completed", "max-tokens")

_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(api[_-]?key|authorization|token)\b\s*[=:]\s*(?!bearer\b)[^\s,;]+")
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


@dataclass(slots=True)
class HarnessResult:
    final_response: str
    session_id: str
    finish_reason: str | None = None
    raw: str = ""


class HarnessRunner:
    def __init__(self, config: AgentConfig, emit: EventCallback):
        self.config = config
        self.emit = emit

    def run(
        self,
        prompt: str,
        cwd: str | Path,
        session_id: str | None = None,
        skill_names: list[str] | None = None,
        on_session_started: Callable[[str], None] | None = None,
    ) -> HarnessResult:
        if not self.config.agent_enabled:
            raise RuntimeError("DeepSeek Harness agent execution is disabled in config.")

        try:
            from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "DeepSeek Harness Python SDK is required. Install it with: python -m pip install -r requirements.txt"
            ) from exc

        worktree = Path(cwd).resolve()
        session_id = session_id or f"gme-{uuid.uuid4().hex}"
        sdk_config = DeepSeekHarnessConfig(
            provider=self.config.provider,
            model=self.config.model,
            reasoning_effort=self.config.reasoning_effort or None,
            cwd=str(worktree),
            runtime_cwd=str(worktree),
            dsh_bin=self.config.dsh_bin or None,
            profile=self.config.dsh_profile,
            patches=(str(self._sdk_patch_path().resolve()),),
            dsh_home=str(Path(self.config.dsh_home).expanduser().resolve()),
        )
        try:
            with DeepSeekHarness(sdk_config) as harness:
                self._emit_bounded("info", f"Running DeepSeek Harness agent session {session_id}.")
                if on_session_started:
                    on_session_started(session_id)
                with self._staged_skills(worktree, skill_names or []) as staged_skill_names:
                    result = harness.run(
                        self._run_input(prompt, staged_skill_names),
                        session_id=session_id,
                        resume_if_exists=True,
                    )
        except Exception as exc:
            raise RuntimeError(self._sanitize_error(str(exc))) from None

        finish_reason = result.finish_reason
        if finish_reason == "completed":
            pass
        elif finish_reason == "max-tokens":
            self._emit_bounded(
                "warn",
                "Harness agent reached the model token limit; validating the produced changes anyway.",
            )
        elif finish_reason in ("error", "aborted"):
            raise RuntimeError(f"DeepSeek Harness agent turn ended with finish reason {finish_reason!r}.")
        else:
            raise RuntimeError("DeepSeek Harness agent turn ended without a finish reason.")

        return HarnessResult(
            final_response=result.final_response or "",
            session_id=session_id,
            finish_reason=finish_reason,
            raw=str(result),
        )

    @staticmethod
    def _sdk_patch_path() -> Path:
        return Path(__file__).resolve().parent / "sdk.patch.yml"

    def _emit_bounded(self, level: str, message: str) -> None:
        self.emit(level, self._cap_text(message, EVENT_TAIL_CHARACTERS))

    @classmethod
    def _sanitize_error(cls, text: str) -> str:
        redacted = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
        redacted = _BEARER_TOKEN.sub("bearer [REDACTED]", redacted)
        return cls._cap_text(redacted, STDERR_TAIL_CHARACTERS)

    @staticmethod
    def _cap_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        skipped = len(text) - limit
        return f"...[{skipped} earlier characters omitted]...\n{text[-limit:]}"

    @staticmethod
    def _run_input(prompt: str, skill_names: list[str]) -> str:
        if not skill_names:
            return prompt

        invocation = "\n".join(f"${name}" for name in skill_names)
        return f"{invocation}\n\n{prompt}"

    @contextmanager
    def _staged_skills(self, cwd: str | Path, skill_names: list[str]) -> Iterator[list[str]]:
        resolved_skills = self._resolved_skills(skill_names)
        if not resolved_skills:
            yield []
            return

        skills_root = Path(cwd).resolve() / ".agents" / "skills"
        created_targets: list[Path] = []
        staged_names: list[str] = []
        try:
            for name, skill_file in resolved_skills:
                source_dir = Path(skill_file).parent
                target_dir = skills_root / name
                if target_dir.exists():
                    if not (target_dir / "SKILL.md").is_file():
                        raise RuntimeError(f"Harness agent skill target exists but has no SKILL.md: {target_dir}")
                    self.emit("info", f"Using existing repository agent skill: {name} ({target_dir / 'SKILL.md'})")
                else:
                    target_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(source_dir, target_dir)
                    created_targets.append(target_dir)
                    self.emit("info", f"Staged agent skill for this task: {name} ({target_dir / 'SKILL.md'})")
                staged_names.append(name)
            yield staged_names
        finally:
            for target_dir in reversed(created_targets):
                shutil.rmtree(target_dir, ignore_errors=True)
            for directory in (skills_root, skills_root.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def _resolved_skills(self, skill_names: list[str]) -> list[tuple[str, str]]:
        if not self.config.use_builtin_skills:
            return []

        resolved = []
        seen = set()
        for name in skill_names:
            if name in seen:
                continue
            seen.add(name)
            skill_dir = self._builtin_skill_dir(name)
            if not skill_dir:
                self.emit("warn", f"Built-in agent skill not found: {name}")
                continue
            skill_file = (skill_dir / "SKILL.md").resolve()
            self.emit("info", f"Using built-in agent skill: {name} ({skill_file})")
            resolved.append((name, str(skill_file)))
        return resolved

    @staticmethod
    def _builtin_skill_dir(name: str) -> Path | None:
        safe_name = "".join(ch for ch in name if ch.isalnum() or ch == "-")
        if not safe_name:
            return None
        path = skill_root() / safe_name
        return path if (path / "SKILL.md").exists() else None
