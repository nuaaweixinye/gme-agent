from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import shutil
from typing import Callable
import re
import subprocess
import xml.etree.ElementTree as ET


EventCallback = Callable[[str, str], None]


def run_template_command(
    template: str,
    mapping: dict[str, str],
    cwd: str | Path,
    emit: EventCallback,
    *,
    timeout_seconds: int = 3600,
) -> tuple[int, str]:
    if not template.strip():
        emit("info", "Command template is empty; skipping.")
        return 0, ""

    command = resolve_command_executable(template.format(**mapping))
    emit("cmd", command)
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            emit("cmd", line)
        code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = 124
        emit("error", f"Command timed out after {timeout_seconds}s.")
    return code, "\n".join(lines)


def resolve_command_executable(command: str) -> str:
    match = re.match(r"^(\s*)cmake(?:\.exe)?(?=\s|$)", command, re.IGNORECASE)
    if not match or shutil.which("cmake"):
        return command
    cmake = _find_visual_studio_cmake()
    if not cmake:
        return command
    return f'{match.group(1)}"{cmake}"{command[match.end():]}'


@lru_cache(maxsize=1)
def _find_visual_studio_cmake() -> str:
    install_dir = str(os.environ.get("VSINSTALLDIR") or "").strip()
    if install_dir:
        candidate = Path(install_dir) / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
        if candidate.is_file():
            return str(candidate)

    vswhere_candidates = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        base = str(os.environ.get(variable) or "").strip()
        if base:
            vswhere_candidates.append(Path(base) / "Microsoft Visual Studio/Installer/vswhere.exe")
    for vswhere in vswhere_candidates:
        if not vswhere.is_file():
            continue
        try:
            result = subprocess.run(
                [str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except OSError:
            continue
        install_dir = result.stdout.strip().splitlines()
        if not install_dir:
            continue
        candidate = Path(install_dir[-1]) / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
        if candidate.is_file():
            return str(candidate)
    return ""


FAILED_RE = re.compile(r"^\[\s+FAILED\s+\]\s+([A-Za-z0-9_./:-]+)")
LOCATION_RE = re.compile(r"^(.+\.(?:cpp|cxx|cc|hxx|hpp|h)):(\d+): Failure")


def parse_gtest_failures(output: str) -> list[dict[str, str | int]]:
    failures: list[dict[str, str | int]] = []
    seen: set[str] = set()
    current_file = ""
    current_line: int | None = None

    for raw in output.splitlines():
        line = raw.strip()
        loc = LOCATION_RE.match(line)
        if loc:
            current_file = loc.group(1)
            current_line = int(loc.group(2))
            continue

        match = FAILED_RE.match(line)
        if not match:
            continue

        full = match.group(1)
        if "." not in full:
            continue
        if full in seen:
            continue
        seen.add(full)
        suite, test = full.rsplit(".", 1)
        failures.append(
            {
                "test_suite": suite,
                "test_name": test,
                "file": current_file,
                "line": current_line or 0,
                "reason": "GTest reported failure.",
            }
        )
    return failures


def parse_gtest_xml(path: str | Path) -> list[dict[str, str | int]]:
    xml_path = Path(path)
    if not xml_path.exists():
        return []

    data = xml_path.read_bytes()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as original_error:
        repaired = data.decode("utf-8", errors="replace")
        repaired = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "\ufffd", repaired)
        try:
            root = ET.fromstring(repaired.encode("utf-8"))
        except ET.ParseError:
            raise original_error
    failures: list[dict[str, str | int]] = []
    for testcase in root.iter("testcase"):
        failure_nodes = list(testcase.findall("failure")) + list(testcase.findall("error"))
        if not failure_nodes:
            continue
        suite = testcase.attrib.get("classname") or ""
        test = testcase.attrib.get("name") or ""
        file = testcase.attrib.get("file") or ""
        line = int(testcase.attrib.get("line") or 0)
        message = failure_nodes[0].attrib.get("message") or (failure_nodes[0].text or "").strip()
        failures.append(
            {
                "test_suite": suite,
                "test_name": test,
                "file": file,
                "line": line,
                "reason": message[:1000] or "GTest XML reported failure.",
            }
        )
    return failures


def merge_failures(*groups: list[dict[str, str | int]]) -> list[dict[str, str | int]]:
    merged: list[dict[str, str | int]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group:
            key = (
                str(item.get("test_suite") or ""),
                str(item.get("test_name") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged
