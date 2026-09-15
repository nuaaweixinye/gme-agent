from __future__ import annotations

import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from ..runtime import skill_root
from ..settings.config import AgentConfig


EventCallback = Callable[[str, str], None]

STDERR_TAIL_CHARACTERS = 4000
EVENT_TAIL_CHARACTERS = 2000

ACCEPTED_FINISH_REASONS = ("completed", "max-tokens")

TOOL_EVENT_TAIL_CHARACTERS = 600
MAX_TOOL_EVENTS_PER_RUN = 200
EVENT_OMISSION_MARK = "…"

_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(api[_-]?key|authorization|token)\b\s*[=:]\s*(?!bearer\b)[^\s,;]+")
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


@dataclass(slots=True)
class HarnessResult:
    final_response: str
    session_id: str
    finish_reason: str | None = None
    raw: str = ""


def tool_notification_events(notification: Any) -> list[tuple[str, str]]:
    """Map one SDK notification to the bounded event lines it should record.

    Only the two DSH session events that describe tool activity are surfaced:
    `tool/call` carries the tool name and its arguments JSON, and `tool/result`
    carries the tool-result content blocks. Everything else is ignored so the
    task event log stays readable.
    """

    if getattr(notification, "method", "") != "session.event":
        return []
    payload = getattr(notification, "payload", None)
    if not isinstance(payload, dict):
        return []
    event = payload.get("event")
    if not isinstance(event, dict):
        return []
    data = event.get("data")
    data = data if isinstance(data, dict) else {}
    kind = event.get("type")
    if kind == "tool/call":
        name = str(data.get("name") or "?")
        arguments = str(data.get("arguments") or "")
        return [("info", f"agent/tool-call {name} {arguments}".rstrip())]
    if kind == "tool/result":
        return [_tool_result_event(data)]
    return []


def _tool_result_event(data: dict[str, Any]) -> tuple[str, str]:
    message = data.get("message")
    message = message if isinstance(message, dict) else {}
    blocks = message.get("content")
    blocks = blocks if isinstance(blocks, list) else []
    level = "info"
    call_id = ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool-result":
            continue
        call_id = call_id or str(block.get("toolCallId") or "")
        if block.get("isError"):
            level = "warn"
        parts.append(_block_text(block.get("content")))
    if not parts:
        error = data.get("error")
        if isinstance(error, dict):
            level = "warn"
            parts.append(str(error.get("message") or "tool failed"))
    text = "\n".join(part for part in parts if part)
    return (level, f"agent/tool-result {call_id or '?'} {text}".rstrip())


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _bounded_event_text(text: str, limit: int) -> str:
    """Bound one event line while keeping its prefix.

    `_cap_text` keeps the tail, which is right for a traceback and wrong for a tool
    event: the tool name and call identity live in the first few characters, so a long
    arguments blob would erase exactly what the event exists to record.
    """

    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit - len(head) - len(EVENT_OMISSION_MARK)) :]
    return f"{head}{EVENT_OMISSION_MARK}{tail}"


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
            recorded_events = 0
            suppressed_events = 0
            event_errors = 0

            def on_notification(notification: Any) -> None:
                nonlocal recorded_events, suppressed_events, event_errors
                try:
                    lines = tool_notification_events(notification)
                    for level, text in lines:
                        if recorded_events >= MAX_TOOL_EVENTS_PER_RUN:
                            suppressed_events += 1
                            continue
                        recorded_events += 1
                        self.emit(level, _bounded_event_text(self._redact(text), TOOL_EVENT_TAIL_CHARACTERS))
                except Exception:
                    event_errors += 1

            with DeepSeekHarness(sdk_config) as harness:
                self._emit_bounded("info", f"Running DeepSeek Harness agent session {session_id}.")
                if on_session_started:
                    on_session_started(session_id)
                with self._staged_skills(worktree, skill_names or []) as staged_skill_names:
                    result = harness.run(
                        self._run_input(prompt, staged_skill_names),
                        session_id=session_id,
                        resume_if_exists=True,
                        on_notification=on_notification,
                    )
        except Exception as exc:
            raise RuntimeError(self._sanitize_error(str(exc))) from None

        if suppressed_events:
            self._emit_quietly(
                "warn",
                f"agent/tool-events-suppressed {suppressed_events} notification(s) omitted after "
                f"{MAX_TOOL_EVENTS_PER_RUN} recorded tool events.",
            )
        if event_errors:
            self._emit_quietly(
                "warn",
                f"agent/tool-event-errors {event_errors} notification(s) could not be recorded.",
            )

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
    def _redact(cls, text: str) -> str:
        redacted = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
        return _BEARER_TOKEN.sub("bearer [REDACTED]", redacted)

    @classmethod
    def _sanitize_error(cls, text: str) -> str:
        return cls._cap_text(cls._redact(text), STDERR_TAIL_CHARACTERS)

    def _emit_quietly(self, level: str, message: str) -> None:
        """Report a diagnostic without letting a broken emitter fail the turn.

        The two summary lines below run after the agent turn has already been read
        back; a job-event writer that is broken enough to raise must not turn a
        finished turn into a failed task.
        """

        try:
            self._emit_bounded(level, message)
        except Exception:
            pass

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
