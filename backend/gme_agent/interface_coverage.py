from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import json

from .git.worktree import normalize_repo_path


EXISTING_TEST_COVERAGE_PATH = ".gme-agent/existing_test_coverage.json"
INTERFACE_CONTRACTS_PATH = ".gme-agent/interface_contracts.json"
INTERFACE_COVERAGE_PLAN_PATH = ".gme-agent/interface_coverage_plan.json"

REQUIRED_COVERAGE_CATEGORIES = (
    "documented_contract",
    "success_partitions",
    "boundary_tolerance",
    "invalid_error",
    "state_sequence",
    "output_invariants",
    "resource_lifecycle",
    "implementation_branches",
    "numerical_robustness",
)

COVERED_GAP_STATUSES = frozenset({"covered_passed", "covered_difference_found"})
BLOCKED_GAP_STATUSES = frozenset(
    {
        "blocked_invalid_contract",
        "blocked_unlinkable",
        "blocked_private_api",
        "blocked_no_reliable_oracle",
    }
)
GAP_STATUSES = frozenset({"planned", "rejected_duplicate", *COVERED_GAP_STATUSES, *BLOCKED_GAP_STATUSES})
INTERFACE_STATUSES = frozenset({"complete", "saturated", "blocked", "budget_exhausted"})


def require_interface_coverage_artifacts(
    worktree: str | Path,
    target_repo: str,
    selected_interfaces: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    previous_entries: set[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    root = Path(worktree)
    expected = _expected_interfaces(selected_interfaces)
    existing = _load_artifact(root, EXISTING_TEST_COVERAGE_PATH)
    contracts = _load_artifact(root, INTERFACE_CONTRACTS_PATH)
    plan = _load_artifact(root, INTERFACE_COVERAGE_PLAN_PATH)

    _validate_analysis_artifact(
        existing,
        expected,
        "existing_test_coverage.json",
        ("existing_tests", "covered_scenarios"),
    )
    contract_interfaces = _validate_analysis_artifact(
        contracts,
        expected,
        "interface_contracts.json",
        ("candidate_gaps",),
    )
    contract_gaps = _validate_contract_gaps(contract_interfaces, expected)
    _validate_coverage_checklists(contract_interfaces, expected, contract_gaps)
    plan_interfaces, gaps = _validate_plan(plan, expected, contract_gaps)
    _validate_generated_test_mappings(
        manifest,
        target_repo,
        expected,
        gaps,
        previous_entries=previous_entries,
    )

    status_counts: Counter[str] = Counter()
    interface_summaries: list[dict[str, Any]] = []
    for interface_id in expected:
        item = plan_interfaces[interface_id]
        item_counts = Counter(str(gap.get("status") or "") for gap in item["gaps"])
        status_counts.update(item_counts)
        interface_summaries.append(
            {
                "interface_id": interface_id,
                "status": item["status"],
                "gap_count": len(item["gaps"]),
                "gap_status_counts": dict(sorted(item_counts.items())),
            }
        )
    return {
        "artifact_paths": {
            "existing_test_coverage": EXISTING_TEST_COVERAGE_PATH,
            "interface_contracts": INTERFACE_CONTRACTS_PATH,
            "interface_coverage_plan": INTERFACE_COVERAGE_PLAN_PATH,
        },
        "interfaces": interface_summaries,
        "gap_status_counts": dict(sorted(status_counts.items())),
    }


def _load_artifact(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    if not path.is_file():
        raise RuntimeError(f"The agent must write {relative_path}")
    content = path.read_bytes()
    if content.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError(f"{relative_path} must be UTF-8 JSON without a BOM")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{relative_path} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError(f"{relative_path} must be a schema_version 1 JSON object")
    if not isinstance(value.get("interfaces"), list):
        raise RuntimeError(f"{relative_path} must contain an interfaces array")
    return value


def _expected_interfaces(selected_interfaces: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    expected: dict[str, dict[str, str]] = {}
    for item in selected_interfaces:
        interface_id = str(item.get("id") or "").strip()
        if not interface_id or interface_id in expected:
            raise RuntimeError("Structured test generation contains invalid or duplicate interface IDs")
        expected[interface_id] = {
            "unique_symbol": str(item.get("unique_symbol") or "").strip(),
            "target_file": normalize_repo_path(str(item.get("target_file") or "")),
        }
    if not expected:
        raise RuntimeError("Structured test generation has no selected interfaces")
    return expected


def _index_selected_interfaces(
    artifact: dict[str, Any],
    expected: dict[str, dict[str, str]],
    artifact_name: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in artifact["interfaces"]:
        if not isinstance(raw, dict):
            raise RuntimeError(f"{artifact_name} contains a non-object interface entry")
        interface_id = str(raw.get("interface_id") or "").strip()
        if not interface_id:
            raise RuntimeError(f"{artifact_name} contains an interface without interface_id")
        if interface_id in indexed:
            raise RuntimeError(f"{artifact_name} contains duplicate interface_id: {interface_id}")
        indexed[interface_id] = raw
    missing = [interface_id for interface_id in expected if interface_id not in indexed]
    if missing:
        raise RuntimeError(f"{artifact_name} is missing selected interfaces: {', '.join(missing)}")
    unexpected = [interface_id for interface_id in indexed if interface_id not in expected]
    if unexpected:
        raise RuntimeError(f"{artifact_name} contains unselected interfaces: {', '.join(unexpected)}")
    return indexed


def _validate_analysis_artifact(
    artifact: dict[str, Any],
    expected: dict[str, dict[str, str]],
    artifact_name: str,
    list_fields: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    indexed = _index_selected_interfaces(artifact, expected, artifact_name)
    for interface_id, identity in expected.items():
        item = indexed[interface_id]
        _validate_interface_identity(item, identity, artifact_name, interface_id)
        for list_field in list_fields:
            if not isinstance(item.get(list_field), list):
                raise RuntimeError(f"{artifact_name} interface {interface_id} must contain {list_field}")
    return indexed


def _validate_contract_gaps(
    interfaces: dict[str, dict[str, Any]],
    expected: dict[str, dict[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    gaps: dict[tuple[str, str], dict[str, Any]] = {}
    for interface_id in expected:
        for gap in interfaces[interface_id]["candidate_gaps"]:
            if not isinstance(gap, dict):
                raise RuntimeError(f"interface_contracts.json interface {interface_id} contains a non-object gap")
            gap_id = str(gap.get("gap_id") or "").strip()
            scenario = str(gap.get("scenario") or "").strip()
            key = (interface_id, gap_id)
            if not gap_id or key in gaps:
                raise RuntimeError(
                    f"interface_contracts.json contains an invalid or duplicate gap_id for {interface_id}"
                )
            if not scenario:
                raise RuntimeError(f"interface_contracts.json gap {interface_id}/{gap_id} is missing scenario")
            if not str(gap.get("category") or "").strip():
                raise RuntimeError(f"interface_contracts.json gap {interface_id}/{gap_id} is missing category")
            if not isinstance(gap.get("preconditions"), list):
                raise RuntimeError(f"interface_contracts.json gap {interface_id}/{gap_id} is missing preconditions")
            if not str(gap.get("oracle") or "").strip():
                raise RuntimeError(f"interface_contracts.json gap {interface_id}/{gap_id} is missing oracle")
            if not str(gap.get("uncovered_evidence") or "").strip():
                raise RuntimeError(
                    f"interface_contracts.json gap {interface_id}/{gap_id} is missing uncovered_evidence"
                )
            gaps[key] = gap
    return gaps


def _validate_coverage_checklists(
    interfaces: dict[str, dict[str, Any]],
    expected: dict[str, dict[str, str]],
    contract_gaps: dict[tuple[str, str], dict[str, Any]],
) -> None:
    for interface_id in expected:
        checklist = interfaces[interface_id].get("coverage_checklist")
        if not isinstance(checklist, list):
            raise RuntimeError(f"interface_contracts.json interface {interface_id} must contain coverage_checklist")
        seen_categories: set[str] = set()
        referenced_gaps: set[tuple[str, str]] = set()
        for entry in checklist:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"interface_contracts.json interface {interface_id} contains a non-object checklist entry"
                )
            category = str(entry.get("category") or "").strip()
            if category not in REQUIRED_COVERAGE_CATEGORIES:
                raise RuntimeError(
                    f"interface_contracts.json interface {interface_id} has invalid coverage category: "
                    f"{category or '<missing>'}"
                )
            if category in seen_categories:
                raise RuntimeError(
                    f"interface_contracts.json interface {interface_id} repeats coverage category: {category}"
                )
            seen_categories.add(category)
            findings = entry.get("findings")
            if not isinstance(findings, list) or not any(str(value or "").strip() for value in findings):
                raise RuntimeError(
                    f"interface_contracts.json interface {interface_id} category {category} needs findings"
                )
            for field in ("existing_test_refs", "candidate_gap_ids", "blocked_scenarios"):
                if not isinstance(entry.get(field), list):
                    raise RuntimeError(
                        f"interface_contracts.json interface {interface_id} category {category} must contain {field}"
                    )
            for raw_gap_id in entry["candidate_gap_ids"]:
                gap_id = str(raw_gap_id or "").strip()
                key = (interface_id, gap_id)
                if not gap_id or key not in contract_gaps:
                    raise RuntimeError(
                        f"interface_contracts.json interface {interface_id} category {category} "
                        f"references an unknown candidate gap: {gap_id or '<missing>'}"
                    )
                referenced_gaps.add(key)
        missing_categories = [
            category for category in REQUIRED_COVERAGE_CATEGORIES if category not in seen_categories
        ]
        if missing_categories:
            raise RuntimeError(
                f"interface_contracts.json interface {interface_id} did not review coverage categories: "
                + ", ".join(missing_categories)
            )
        missing_gap_refs = [
            gap_id
            for gap_interface_id, gap_id in contract_gaps
            if gap_interface_id == interface_id and (gap_interface_id, gap_id) not in referenced_gaps
        ]
        if missing_gap_refs:
            raise RuntimeError(
                f"interface_contracts.json interface {interface_id} checklist omits candidate gaps: "
                + ", ".join(missing_gap_refs)
            )


def _validate_plan(
    artifact: dict[str, Any],
    expected: dict[str, dict[str, str]],
    contract_gaps: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    artifact_name = "interface_coverage_plan.json"
    indexed = _index_selected_interfaces(artifact, expected, artifact_name)
    gaps: dict[tuple[str, str], dict[str, Any]] = {}
    for interface_id, identity in expected.items():
        item = indexed[interface_id]
        _validate_interface_identity(item, identity, artifact_name, interface_id)
        status = str(item.get("status") or "").strip()
        if status not in INTERFACE_STATUSES:
            raise RuntimeError(
                f"{artifact_name} interface {interface_id} has invalid terminal status: {status or '<missing>'}"
            )
        _validate_closure_audit(item, interface_id)
        raw_gaps = item.get("gaps")
        if not isinstance(raw_gaps, list):
            raise RuntimeError(f"{artifact_name} interface {interface_id} must contain gaps")
        for gap in raw_gaps:
            if not isinstance(gap, dict):
                raise RuntimeError(f"{artifact_name} interface {interface_id} contains a non-object gap")
            gap_id = str(gap.get("gap_id") or "").strip()
            scenario = str(gap.get("scenario") or "").strip()
            gap_status = str(gap.get("status") or "").strip()
            key = (interface_id, gap_id)
            if not gap_id or key in gaps:
                raise RuntimeError(f"{artifact_name} contains an invalid or duplicate gap_id for {interface_id}")
            if not scenario:
                raise RuntimeError(f"{artifact_name} gap {interface_id}/{gap_id} is missing scenario")
            contract_gap = contract_gaps.get(key)
            if contract_gap is None:
                raise RuntimeError(f"{artifact_name} contains a gap absent from interface contracts: {interface_id}/{gap_id}")
            if scenario != str(contract_gap.get("scenario") or "").strip():
                raise RuntimeError(f"{artifact_name} scenario differs from interface contracts: {interface_id}/{gap_id}")
            if gap_status not in GAP_STATUSES:
                raise RuntimeError(
                    f"{artifact_name} gap {interface_id}/{gap_id} has invalid status: {gap_status or '<missing>'}"
                )
            if gap_status == "planned" and status != "budget_exhausted":
                raise RuntimeError(
                    f"{artifact_name} leaves gap {interface_id}/{gap_id} planned without budget_exhausted status"
                )
            if gap_status in BLOCKED_GAP_STATUSES | {"rejected_duplicate"} and not str(
                gap.get("reason") or ""
            ).strip():
                raise RuntimeError(f"{artifact_name} gap {interface_id}/{gap_id} must explain its status")
            gaps[key] = gap
    missing_contract_gaps = [
        f"{interface_id}/{gap_id}"
        for interface_id, gap_id in contract_gaps
        if (interface_id, gap_id) not in gaps
    ]
    if missing_contract_gaps:
        raise RuntimeError(
            f"{artifact_name} omits candidate gaps from interface contracts: "
            + ", ".join(missing_contract_gaps)
        )
    return indexed, gaps


def _validate_closure_audit(item: dict[str, Any], interface_id: str) -> None:
    audit = item.get("closure_audit")
    if not isinstance(audit, dict) or audit.get("performed") is not True:
        raise RuntimeError(
            f"interface_coverage_plan.json interface {interface_id} must contain a performed closure_audit"
        )
    reviewed = audit.get("reviewed_categories")
    if not isinstance(reviewed, list):
        raise RuntimeError(
            f"interface_coverage_plan.json interface {interface_id} closure_audit must list reviewed_categories"
        )
    reviewed_categories = {str(value or "").strip() for value in reviewed}
    missing = [category for category in REQUIRED_COVERAGE_CATEGORIES if category not in reviewed_categories]
    if missing:
        raise RuntimeError(
            f"interface_coverage_plan.json interface {interface_id} closure_audit omitted categories: "
            + ", ".join(missing)
        )
    remaining = audit.get("remaining_unplanned_scenarios")
    if not isinstance(remaining, list) or remaining:
        raise RuntimeError(
            f"interface_coverage_plan.json interface {interface_id} closure_audit has unplanned scenarios"
        )
    evidence = audit.get("evidence")
    if not isinstance(evidence, list) or not any(str(value or "").strip() for value in evidence):
        raise RuntimeError(
            f"interface_coverage_plan.json interface {interface_id} closure_audit needs evidence"
        )


def _validate_generated_test_mappings(
    manifest: dict[str, Any],
    target_repo: str,
    expected: dict[str, dict[str, str]],
    gaps: dict[tuple[str, str], dict[str, Any]],
    *,
    previous_entries: set[tuple[str, str, str]] | None,
) -> None:
    mapped: dict[tuple[str, str], dict[str, Any]] = {}
    seen_new_entries: set[tuple[str, str, str]] = set()
    for test in manifest.get("tests") or []:
        entry_key = (
            str(test.get("file") or ""),
            str(test.get("suite") or ""),
            str(test.get("name") or ""),
        )
        is_previous = previous_entries is not None and entry_key in previous_entries
        if is_previous:
            continue
        if entry_key in seen_new_entries:
            raise RuntimeError(
                f"Generated test manifest contains a duplicate test: {entry_key[1]}.{entry_key[2]}"
            )
        seen_new_entries.add(entry_key)
        interface_id = str(test.get("interface_id") or "").strip()
        gap_id = str(test.get("gap_id") or "").strip()
        scenario = str(test.get("scenario") or "").strip()
        if not interface_id or not gap_id or not scenario:
            raise RuntimeError(
                "Every newly generated test must include interface_id, gap_id, and scenario"
            )
        if interface_id not in expected:
            raise RuntimeError(f"Generated test maps to an unselected interface: {interface_id}")
        gap = gaps.get((interface_id, gap_id))
        if gap is None:
            raise RuntimeError(f"Generated test maps to an unknown coverage gap: {interface_id}/{gap_id}")
        if str(gap.get("status") or "") not in COVERED_GAP_STATUSES:
            raise RuntimeError(f"Generated test maps to a non-covered gap: {interface_id}/{gap_id}")
        if scenario != str(gap.get("scenario") or "").strip():
            raise RuntimeError(f"Generated test scenario does not match coverage gap: {interface_id}/{gap_id}")
        expected_file = _target_relative_path(expected[interface_id]["target_file"], target_repo)
        if str(test.get("file") or "") != expected_file:
            raise RuntimeError(
                f"Generated test for {interface_id} must stay in selected target file {expected_file}"
            )
        key = (interface_id, gap_id)
        if key in mapped:
            raise RuntimeError(f"Multiple generated tests map to the same coverage gap: {interface_id}/{gap_id}")
        mapped[key] = test

    for key, gap in gaps.items():
        if str(gap.get("status") or "") not in COVERED_GAP_STATUSES:
            continue
        test_ref = gap.get("test")
        if not isinstance(test_ref, dict):
            raise RuntimeError(f"Covered gap {key[0]}/{key[1]} must contain a test reference")
        test = mapped.get(key)
        if test is None:
            raise RuntimeError(f"Covered gap {key[0]}/{key[1]} has no generated test mapping")
        if str(test_ref.get("suite") or "") != str(test.get("suite") or "") or str(
            test_ref.get("name") or ""
        ) != str(test.get("name") or ""):
            raise RuntimeError(f"Covered gap {key[0]}/{key[1]} test reference does not match manifest")


def _validate_interface_identity(
    item: dict[str, Any],
    expected: dict[str, str],
    artifact_name: str,
    interface_id: str,
) -> None:
    if str(item.get("unique_symbol") or "").strip() != expected["unique_symbol"]:
        raise RuntimeError(f"{artifact_name} has the wrong unique_symbol for {interface_id}")
    if normalize_repo_path(str(item.get("target_file") or "")) != expected["target_file"]:
        raise RuntimeError(f"{artifact_name} has the wrong target_file for {interface_id}")


def _target_relative_path(value: str, target_repo: str) -> str:
    path = normalize_repo_path(value)
    target = normalize_repo_path(target_repo)
    prefix = f"{target}/"
    return path[len(prefix) :] if path.startswith(prefix) else path
