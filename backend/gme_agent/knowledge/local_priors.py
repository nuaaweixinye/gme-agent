"""Read-only collection of failure priors for one test-generation task.

The task database already records which interfaces diverged between GME and
ACIS. This module turns those rows into attributed priors: a prior carries the
assertion summary, the reproduce command, how many distinct runs failed, and
whether the closed loop has confirmed it yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

FAILED_OUTCOME = "failed"


@dataclass(frozen=True, slots=True)
class Prior:
    failure_id: str
    test_suite: str
    test_name: str
    file: str
    reason: str
    reproduce_command: str
    interface_id: str
    api_name: str
    module: str
    stable_runs: int
    injected_count: int
    confirmed: bool
    match_kind: str = "module"

    @property
    def key(self) -> str:
        return f"{self.test_suite}.{self.test_name}"

    @property
    def attribution(self) -> str:
        return self.interface_id or self.api_name


def collect_priors(
    db: Any,
    *,
    module: str,
    interface_ids: Sequence[str] = (),
    api_names: Sequence[str] = (),
    limit: int = 8,
) -> list[Prior]:
    """Return priors for the interfaces this task will touch.

    Matching is three-way and ranked: an exact `interface_id` match beats an
    `api_name` match, which beats a same-module match. The module tier matters
    because a free-text task has no structured selection and its `api_name` is
    a title, not a symbol — without the module tier local injection would be
    empty exactly when nothing else identifies the target.

    A prior must carry attribution (`interface_id` or `api_name`): an
    unattributed failure is kept in the task database but never injected.
    """

    wanted_interfaces = _wanted(interface_ids)
    wanted_apis = _wanted(api_names)
    wanted_module = (module or "").strip()
    ranked: list[tuple[tuple[int, bool, int, str, str], Prior]] = []
    for failure in db.list_failures():
        metadata = failure.get("metadata") or {}
        interface_id = str(metadata.get("interface_id") or "").strip()
        api_name = str(metadata.get("api_name") or "").strip()
        if not (interface_id or api_name):
            continue
        failure_module = str(metadata.get("module") or "").strip()
        rank = _match_rank(
            wanted_module=wanted_module,
            wanted_interfaces=wanted_interfaces,
            wanted_apis=wanted_apis,
            failure_module=failure_module,
            interface_id=interface_id,
            api_name=api_name,
        )
        if rank is None:
            continue
        knowledge = metadata.get("knowledge")
        knowledge = knowledge if isinstance(knowledge, dict) else {}
        failure_id = str(failure.get("id") or "")
        prior = Prior(
            failure_id=failure_id,
            test_suite=str(failure.get("test_suite") or ""),
            test_name=str(failure.get("test_name") or ""),
            file=str(failure.get("file") or ""),
            reason=str(failure.get("reason") or ""),
            reproduce_command=str(failure.get("reproduce_command") or ""),
            interface_id=interface_id,
            api_name=api_name,
            module=failure_module,
            stable_runs=count_stable_runs(db, failure_id),
            injected_count=_injected_count(knowledge),
            confirmed=bool(knowledge.get("confirmed_at")),
            match_kind=_MATCH_KINDS[rank],
        )
        ranked.append(
            (
                (rank, not prior.confirmed, -prior.stable_runs, prior.test_suite, prior.test_name),
                prior,
            )
        )
    ranked.sort(key=lambda item: item[0])
    return [prior for _key, prior in ranked[: max(int(limit), 0)]]


def count_stable_runs(db: Any, failure_id: str) -> int:
    """Count distinct runs in which this failure was observed as failing."""

    if not failure_id:
        return 0
    run_ids = {
        str(observation.get("run_id") or "")
        for observation in db.list_failure_observations(failure_id)
        if str(observation.get("outcome") or "") == FAILED_OUTCOME
    }
    run_ids.discard("")
    return len(run_ids)


def _wanted(values: Sequence[str]) -> set[str]:
    return {str(value).strip() for value in values or () if str(value).strip()}


_MATCH_KINDS = ("interface", "api", "module")


def _match_rank(
    *,
    wanted_module: str,
    wanted_interfaces: set[str],
    wanted_apis: set[str],
    failure_module: str,
    interface_id: str,
    api_name: str,
) -> int | None:
    """Rank one failure against the task's targets: 0 interface, 1 api, 2 module."""

    if interface_id and interface_id in wanted_interfaces:
        return 0
    if api_name and api_name in wanted_apis:
        return 1
    if wanted_module and failure_module == wanted_module:
        return 2
    return None


def _injected_count(knowledge: dict[str, Any]) -> int:
    value = knowledge.get("injected_count")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
