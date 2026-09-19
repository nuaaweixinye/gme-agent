"""Resolve the clang-format binary whose judgement this backend trusts.

GME's CI pins the formatter behind its own `check-format` target: it runs
`pip install clang-format==17.0.2` before building that target
(`.github/workflows/format_check.yml`). clang-format formats the same source
differently across major versions, so a backend that used whatever happened to be
on PATH would report "the format check passed" for source GME's pipeline then
rejects — and the repair and skip-PR paths both gate on that verdict.

Resolution is deliberate, in this order:

1. the interpreter's own environment (a `.venv` created by the installer, where
   `pip install -r requirements.txt` puts the pinned binary);
2. `clang_format_path`, when the operator names one explicitly;
3. `PATH`.

The first candidate whose version matches `GME_CLANG_FORMAT_VERSION` wins. If none
matches, the check fails with both versions named and the one-line fix, unless
`allow_clang_format_version_mismatch` is set, in which case the first candidate is
used with a warning from the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import os
import re
import shutil
import subprocess
import sys

from ..settings.config import GME_CLANG_FORMAT_VERSION

EXECUTABLE_NAMES = ("clang-format.exe", "clang-format")
_VERSION_PATTERN = re.compile(r"clang-format version\s+([0-9][0-9A-Za-z.\-]*)")
INSTALL_HINT = f"python -m pip install clang-format=={GME_CLANG_FORMAT_VERSION}"

VersionProbe = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class ClangFormatCandidate:
    path: str
    version: str
    origin: str


@dataclass(frozen=True, slots=True)
class ResolvedClangFormat:
    path: str
    version: str
    matches_gme: bool


def parse_clang_format_version(output: str) -> str:
    """Return the version named by `clang-format --version`, or "" when absent."""

    match = _VERSION_PATTERN.search(output or "")
    return match.group(1) if match else ""


def _probe_version(path: str) -> str:
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return ""
    return parse_clang_format_version(completed.stdout or "")


def candidate_paths(config: Any) -> list[tuple[str, str]]:
    """Return `(path, origin)` candidates in resolution order, deduplicated."""

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(path: str, origin: str) -> None:
        if not path:
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        candidates.append((path, origin))

    interpreter_dir = Path(sys.executable).resolve().parent
    # A virtualenv keeps its executables in `Scripts` on Windows and `bin` elsewhere,
    # so the interpreter's own directory is only the first place to look.
    for directory in (interpreter_dir, interpreter_dir / "Scripts", interpreter_dir / "bin"):
        found = next((directory / name for name in EXECUTABLE_NAMES if (directory / name).is_file()), None)
        if found is not None:
            add(str(found), "the interpreter environment")
            break

    configured = str(getattr(config, "clang_format_path", "") or "").strip()
    if configured:
        expanded = Path(configured).expanduser()
        if not expanded.is_file():
            # An explicit setting is authoritative: silently falling through to
            # another binary would run a formatter the operator did not choose.
            raise RuntimeError(
                f"clang_format_path is set to {expanded}, which is not a file. Point it at a "
                f"clang-format {GME_CLANG_FORMAT_VERSION} binary, or install the pinned version with: {INSTALL_HINT}"
            )
        add(str(expanded), "clang_format_path")

    for name in EXECUTABLE_NAMES:
        found = shutil.which(name)
        if found:
            add(found, "PATH")
            break

    return candidates


def inspect_clang_format_candidates(config: Any, *, probe: VersionProbe | None = None) -> list[ClangFormatCandidate]:
    """Probe every candidate, in order, without deciding anything."""

    version_of = probe or _probe_version
    return [ClangFormatCandidate(path=path, version=version_of(path), origin=origin) for path, origin in candidate_paths(config)]


def resolve_clang_format(config: Any, *, probe: VersionProbe | None = None) -> ResolvedClangFormat:
    """Return the binary to run, or raise with the mismatch spelled out."""

    expected = GME_CLANG_FORMAT_VERSION
    candidates = inspect_clang_format_candidates(config, probe=probe)
    if not candidates:
        raise RuntimeError(
            f"clang-format was not found. GME's check-format target runs clang-format {expected}; "
            f"install it with: {INSTALL_HINT}"
        )

    for candidate in candidates:
        if candidate.version == expected:
            return ResolvedClangFormat(path=candidate.path, version=candidate.version, matches_gme=True)

    if bool(getattr(config, "allow_clang_format_version_mismatch", False)):
        first = candidates[0]
        return ResolvedClangFormat(path=first.path, version=first.version, matches_gme=False)

    found = ", ".join(
        f"{candidate.path} ({candidate.origin}: {candidate.version or 'unknown version'})" for candidate in candidates
    )
    raise RuntimeError(
        f"clang-format {expected} is required (the version GME's check-format target runs), but found: {found}. "
        f"Install the pinned version with: {INSTALL_HINT}, or set clang_format_path, or set "
        f"allow_clang_format_version_mismatch to accept a different formatter."
    )


def describe_version_mismatch(resolved: ResolvedClangFormat) -> str:
    """One line naming the formatter that will be used and the risk it carries."""

    return (
        f"clang-format {resolved.version or 'unknown'} at {resolved.path} differs from the {GME_CLANG_FORMAT_VERSION} "
        f"GME's check-format target runs; formatting verdicts may not match the GME pipeline."
    )
