"""Closed-loop bookkeeping: confirm divergences and count injections.

A failure is promoted to an injectable prior only when it is a real divergence
(not a link/compile/timeout failure), still present in the task's
`.gme-agent/generated_tests.json`, carries attribution, and reproduced in at
least `min_stable_runs` distinct runs. Promotion writes
`failures.metadata_json.knowledge` and never adds a table or changes `status`.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence
import re

from ..generated_tests import generated_test_keys
from ..storage.db import now_ts
from .local_priors import count_stable_runs

DIVERGENCE_MARKERS = ("same_acis_gme",)
DIVERGENCE_IDENTIFIER = re.compile(r"\b(?:gme|acis)_[a-z0-9_]+\b")

INVALID_MARKERS = (
    "lnk2019",
    "lnk2001",
    "unresolved external",
    "is not a member",
    "no member named",
    "compile error",
    "error c2",
    "error c3",
    "fatal error",
    "timed out",
    "timeout",
    "no such file",
)

EventCallback = Callable[[str, str], None]


def is_divergence_reason(reason: str) -> bool:
    """True when a failure message looks like a GME/ACIS observable difference.

    The corpus uses one comparison helper per scenario — `gme_answer`, `gme_result`,
    `gme_first`, `acis_result` — so a fixed list of names silently misses the next
    one: a real recorded divergence comparing `gme_first` was missed by exactly such
    a list. Matching the identifier shape covers the family, and the invalid-marker
    screen still keeps link/compile/timeout failures out.
    """

    text = (reason or "").lower()
    if not text.strip():
        return False
    if any(marker in text for marker in INVALID_MARKERS):
        return False
    if any(marker in text for marker in DIVERGENCE_MARKERS):
        return True
    return bool(DIVERGENCE_IDENTIFIER.search(text))


def promote_confirmed_failures(
    db: Any,
    *,
    failures: Sequence[dict[str, Any]],
    manifest: dict[str, Any],
    min_stable_runs: int = 2,
    on_event: EventCallback | None = None,
) -> list[str]:
    """Mark stable, attributed divergences as injectable priors in the future."""

    emit = on_event or (lambda _level, _message: None)
    threshold = max(int(min_stable_runs), 1)
    manifest_keys = generated_test_keys(manifest or {})
    promoted: list[str] = []
    for failure in failures or ():
        failure_id = str(failure.get("id") or "")
        if not failure_id:
            continue
        metadata = _mapping(failure.get("metadata"))
        interface_id = str(metadata.get("interface_id") or "").strip()
        api_name = str(metadata.get("api_name") or "").strip()
        if not (interface_id or api_name):
            continue
        test_key = (str(failure.get("test_suite") or ""), str(failure.get("test_name") or ""))
        if test_key not in manifest_keys:
            continue
        if not is_divergence_reason(str(failure.get("reason") or "")):
            continue
        stable_runs = count_stable_runs(db, failure_id)
        if stable_runs < threshold:
            continue
        # Write from the stored row, not from the caller's snapshot. The caller's dict
        # may predate another write to the same failure (a re-run's `injected_count`,
        # for one), and promotion must not clobber fields it does not own.
        try:
            stored = db.get_failure(failure_id)
        except KeyError:
            continue
        stored_metadata = _mapping(stored.get("metadata"))
        knowledge = _mapping(stored_metadata.get("knowledge"))
        knowledge.update(
            {
                "confirmed_at": now_ts(),
                "stability": stable_runs,
                "injected_count": _count(knowledge.get("injected_count")),
            }
        )
        stored_metadata["knowledge"] = knowledge
        db.update_failure(failure_id, metadata=stored_metadata)
        promoted.append(failure_id)
        emit(
            "info",
            f"knowledge/promoted {test_key[0]}.{test_key[1]} stability={stable_runs} "
            f"interface={interface_id or api_name}",
        )
    return promoted


def record_injection_counts(db: Any, failure_ids: Sequence[str]) -> None:
    """Increment `metadata.knowledge.injected_count` for injected priors."""

    for failure_id in dict.fromkeys(str(value) for value in failure_ids if str(value)):
        try:
            failure = db.get_failure(failure_id)
        except KeyError:
            continue
        metadata = _mapping(failure.get("metadata"))
        knowledge = _mapping(metadata.get("knowledge"))
        knowledge["injected_count"] = _count(knowledge.get("injected_count")) + 1
        knowledge["last_injected_at"] = now_ts()
        metadata["knowledge"] = knowledge
        db.update_failure(failure_id, metadata=metadata)


def _mapping(value: Any) -> dict[str, Any]:
    """Read a JSON object that a hand-edited row can leave in any shape.

    `dict()` on a list raises, and both callers run inside flows that must not fail
    because stored metadata is malformed; `metadata_json` is user-visible.
    """

    return dict(value) if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    """Read a counter out of a JSON blob the user can hand-edit.

    Task 7 shipped this function with a bare `int(...)`. A corrupt value raised,
    and because `record_injection_counts` runs inside the injection path, that
    exception discarded the whole knowledge block; the same bare `int(...)` sat in
    `promote_confirmed_failures`. Neither may raise.
    """

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
