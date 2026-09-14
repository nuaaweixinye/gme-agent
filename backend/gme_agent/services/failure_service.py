from __future__ import annotations

from typing import Any


FAILURE_STATUSES = {"open", "resolved", "fixing", "fix_ready", "fixed", "ignored", "fix_failed"}


def update_failure_status(db, failure_id: str, status: str) -> dict[str, Any]:
    if status not in FAILURE_STATUSES:
        raise ValueError(f"Unsupported failure status: {status}")
    return db.update_failure(failure_id, status=status)


def failure_filter(failure: dict[str, Any]) -> str:
    suite = failure.get("test_suite") or ""
    test = failure.get("test_name") or ""
    return f"{suite}.{test}" if suite and test else "*"
