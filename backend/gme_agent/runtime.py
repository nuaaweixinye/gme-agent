from __future__ import annotations

from pathlib import Path
import os
import sys


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    configured = os.environ.get("GME_AGENT_RESOURCE_ROOT")
    if configured:
        return Path(configured)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parents[2]
    return project_root()


def skill_root() -> Path:
    return resource_root() / "skills"
