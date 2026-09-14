from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re


MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def list_interface_catalogs(catalog_root: str | Path | None = None) -> list[str]:
    root = _catalog_root(catalog_root)
    if not root.is_dir():
        return []
    return sorted(path.stem for path in root.glob("*.json") if path.is_file())


def load_interface_catalog(
    module: str,
    catalog_root: str | Path | None = None,
) -> dict[str, Any]:
    normalized = module.strip()
    if not normalized or not MODULE_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(f"Invalid interface catalog module: {module!r}")

    path = _catalog_root(catalog_root) / f"{normalized}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Interface catalog does not exist: {path}")

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Interface catalog must be a JSON object: {path}")
    if value.get("schema_version") != 1:
        raise ValueError(f"Unsupported interface catalog schema: {path}")
    if value.get("module") != normalized:
        raise ValueError(
            f"Interface catalog module mismatch: expected {normalized}, got {value.get('module')}"
        )
    if not isinstance(value.get("interfaces"), list):
        raise ValueError(f"Interface catalog has no interfaces list: {path}")
    return value


def _catalog_root(value: str | Path | None) -> Path:
    return Path(value) if value is not None else Path(__file__).resolve().parent / "catalogs"
