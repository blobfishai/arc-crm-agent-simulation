"""Deterministic, contract-driven verifier (HubScore).  Zero LLM calls.

Adapted from the FactoryBench-100 evaluator (Apache-2.0, BlobfishAI); see NOTICE.
The verifier never runs inside the agent's tool surface: it reads the sealed
task contract and the episode's SQLite world after the episode ends.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from .families import SUBMIT_TOOL
from .validation import canonical_argument
from .world import World, normalize_answer_fields

METRIC = "HubScore"


# --------------------------------------------------------------------------- #
# Trace matching
# --------------------------------------------------------------------------- #


def result_contains(actual: Any, expected: Any) -> bool:
    """Return whether one nested provider response contains an expected fragment."""

    if isinstance(expected, dict):
        if isinstance(actual, dict) and all(key in actual and result_contains(actual[key], value) for key, value in expected.items()):
            return True
        if isinstance(actual, dict):
            return any(result_contains(value, expected) for value in actual.values())
        if isinstance(actual, list):
            return any(result_contains(value, expected) for value in actual)
        return False
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        return all(any(result_contains(actual_item, expected_item) for actual_item in actual) for expected_item in expected)
    return actual == expected


def requirement_matches(entry: dict[str, Any], requirement: dict[str, Any]) -> bool:
    if entry["tool"] != requirement["tool"]:
        return False
    match = requirement.get("match")
    if match == "result_contains":
        fragment = requirement.get("expected_result_contains")
        return fragment is not None and result_contains(entry.get("result"), fragment)
    if match == "successful_tool_call":
        return True
    expected_arguments = requirement.get("arguments")
    return expected_arguments is None or canonical_argument(entry.get("arguments", {})) == canonical_argument(expected_arguments)


def missing_required_investigations(task: dict[str, Any], trace: list[dict[str, Any]], *, before_index: int | None = None) -> list[dict[str, Any]]:
    """Return unsatisfied investigations, independent of call order."""

    successful = [entry for entry in trace if entry.get("success") and (before_index is None or entry["index"] < before_index)]
    missing: list[dict[str, Any]] = []
    for investigation in task.get("required_investigations", []):
        matched = any(requirement_matches(entry, requirement) for requirement in investigation.get("any_of", []) for entry in successful)
        if not matched:
            missing.append(investigation)
    return missing


def missing_post_write_verifications(task: dict[str, Any], trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return provider readbacks not observed after their successful mutation."""

    successful = [entry for entry in trace if entry.get("success")]
    missing: list[dict[str, Any]] = []
    for verification in task.get("post_write_verifications", []):
        mutation_indexes = [entry["index"] for entry in successful if entry["tool"] == verification["after_tool"]]
        if not mutation_indexes:
            missing.append(verification)
            continue
        mutation_index = min(mutation_indexes)
        matched = any(
            entry["index"] > mutation_index
            and requirement_matches(entry, requirement)
            and result_contains(entry.get("result"), verification.get("expected_result_contains", {}))
            for requirement in verification.get("any_of", [])
            for entry in successful
        )
        if not matched:
            missing.append(verification)
    return missing


# --------------------------------------------------------------------------- #
# Payload grading
# --------------------------------------------------------------------------- #


def _nested_subset_mismatches(actual: Any, expected: Any, path: str = "payload") -> dict[str, dict[str, Any]]:
    mismatches: dict[str, dict[str, Any]] = {}
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return {path: {"expected": expected, "actual": actual}}
        for key, value in expected.items():
            child = f"{path}.{key}"
            if key not in actual:
                mismatches[child] = {"expected": value, "actual": None, "reason": "missing key"}
                continue
            mismatches.update(_nested_subset_mismatches(actual[key], value, child))
        return mismatches
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return {path: {"expected": expected, "actual": actual}}
        if len(actual) != len(expected):
            return {path: {"expected_length": len(expected), "actual_length": len(actual)}}
        for index, value in enumerate(expected):
            mismatches.update(_nested_subset_mismatches(actual[index], value, f"{path}[{index}]"))
        return mismatches
    if actual != expected:
        mismatches[path] = {"expected": expected, "actual": actual}
    return mismatches


def _decoded_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_decoded_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_decoded_text(item) for item in value)
    if not isinstance(value, str):
        return str(value)
    variants = [value]
    try:
        padded = value + "=" * (-len(value) % 4)
        variants.append(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        pass
    return " ".join(variants)


def _leaf_paths(value: Any, path: str = "") -> set[str]:
    if isinstance(value, dict):
        return {leaf for key, item in value.items() for leaf in _leaf_paths(item, f"{path}.{key}" if path else key)}
    if isinstance(value, list):
        return {leaf for index, item in enumerate(value) for leaf in _leaf_paths(item, f"{path}[{index}]")}
    return {path}


def _searchable(payload: dict[str, Any]) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", _decoded_text(payload.get("arguments", payload)).casefold())


def _normalized_fragment(fragment: Any) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", str(fragment).casefold()).strip()


def _fact_tokens(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", text.casefold())) + " "


def _argument_text_mismatches(payload: dict[str, Any], requirements: Any) -> dict[str, Any]:
    """Match literal facts in named string arguments, never adjacent fields.

    Empty fact lists require nonempty text. Token boundaries reject, for example,
    BF-71010 in place of BF-7101. No substring/fuzzy or semantic scoring is used.
    """

    if not isinstance(requirements, dict):
        return {"contract": {"reason": "text requirements must be an object"}}
    arguments = payload.get("arguments")
    mismatches: dict[str, Any] = {}
    for field, facts in requirements.items():
        if not isinstance(facts, list) or any(
            not isinstance(fact, str) or not _fact_tokens(fact).strip() for fact in facts
        ):
            mismatches[field] = {"reason": "text facts must be nonempty strings"}
            continue
        actual = arguments.get(field) if isinstance(arguments, dict) else None
        if not isinstance(actual, str) or not actual.strip():
            mismatches[field] = {"reason": "required argument is not nonempty text"}
            continue
        tokens = _fact_tokens(actual)
        missing = [fact for fact in facts if _fact_tokens(fact) not in tokens]
        if missing:
            mismatches[field] = {"missing_text_facts": missing}
    return mismatches


def payload_assertion_mismatches(row: dict[str, Any], assertion: dict[str, Any]) -> dict[str, Any]:
    """Grade the actual provider payload persisted for a state assertion."""

    if not any(key in assertion for key in ("payload_contains", "payload_text_contains", "payload_text_any_of", "payload_allowed_argument_paths", "payload_argument_text")):
        return {}
    raw_payload = row.get("payload_json")
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except json.JSONDecodeError:
        return {"payload_json": {"reason": "invalid JSON", "actual": raw_payload}}
    if not isinstance(payload, dict):
        return {"payload_json": {"reason": "payload is not an object", "actual": payload}}
    evidence: dict[str, Any] = {}
    if "payload_argument_text" in assertion:
        text_mismatches = _argument_text_mismatches(payload, assertion["payload_argument_text"])
        if text_mismatches:
            evidence["argument_text_mismatches"] = text_mismatches
    expected_subset = assertion.get("payload_contains")
    if expected_subset is not None:
        nested = _nested_subset_mismatches(payload, expected_subset)
        if nested:
            evidence["payload_mismatches"] = nested
    expected_text = assertion.get("payload_text_contains", [])
    if expected_text:
        searchable = _searchable(payload)
        missing = [str(fragment) for fragment in expected_text if _normalized_fragment(fragment) not in searchable]
        if missing:
            evidence["missing_payload_text"] = missing
    expected_groups = assertion.get("payload_text_any_of", [])
    if expected_groups:
        searchable = _searchable(payload)
        missing_groups = [
            [str(fragment) for fragment in group]
            for group in expected_groups
            if not any(_normalized_fragment(fragment) and _normalized_fragment(fragment) in searchable for fragment in group)
        ]
        if missing_groups:
            evidence["missing_payload_text_any_of"] = missing_groups
    allowed_paths = assertion.get("payload_allowed_argument_paths")
    if allowed_paths is not None:
        unexpected = sorted(_leaf_paths(payload.get("arguments", {})) - set(allowed_paths))
        if unexpected:
            evidence["unexpected_payload_paths"] = unexpected
    return evidence


# --------------------------------------------------------------------------- #
# Episode verification
# --------------------------------------------------------------------------- #


def _query_rows(world: World, assertion: dict[str, Any]) -> list[dict[str, Any]]:
    where = assertion["where"]
    clauses = " AND ".join(f"{column} IS ?" if value is None else f"{column} = ?" for column, value in where.items())
    query = f"SELECT * FROM {assertion['table']}"
    params: list[Any] = []
    if clauses:
        query += f" WHERE {clauses}"
        params = list(where.values())
    return [dict(row) for row in world.connection.execute(query, params).fetchall()]


def _values_match(actual: dict[str, Any], expected: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    mismatches: dict[str, Any] = {}
    for field, expected_value in expected.items():
        actual_value = actual.get(field)
        if isinstance(expected_value, float) and isinstance(actual_value, (int, float)):
            matched = abs(float(actual_value) - expected_value) <= 1e-6
        else:
            matched = actual_value == expected_value
        if not matched:
            mismatches[field] = {"expected": expected_value, "actual": actual_value}
    return not mismatches, mismatches


def aggregate_milestones(task: dict[str, Any], atomic_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll deterministic atomic checks into the task's semantic milestones."""

    milestones = task.get("rubric_milestones", [])
    if not milestones:
        return [{**check, "earned_weight": float(check["weight"]) if check["passed"] else 0.0} for check in atomic_checks]
    by_id: dict[str, dict[str, Any]] = {}
    for check in atomic_checks:
        check_id = str(check["id"])
        if check_id in by_id:
            raise ValueError(f"duplicate atomic check id: {check_id}")
        by_id[check_id] = check
    assigned: set[str] = set()
    aggregated: list[dict[str, Any]] = []
    for milestone in milestones:
        criterion_ids = [str(value) for value in milestone["criterion_ids"]]
        if not criterion_ids:
            raise ValueError(f"milestone {milestone['id']} has no criteria")
        reused = sorted(set(criterion_ids) & assigned)
        if reused:
            raise ValueError(f"atomic checks assigned to multiple milestones: {reused}")
        missing = sorted(set(criterion_ids) - set(by_id))
        if missing:
            raise ValueError(f"milestone {milestone['id']} references missing checks: {missing}")
        assigned.update(criterion_ids)
        subchecks = [by_id[criterion_id] for criterion_id in criterion_ids]
        atomic_weight = sum(float(check["weight"]) for check in subchecks)
        expected_atomic_weight = float(milestone.get("atomic_weight", atomic_weight))
        if abs(atomic_weight - expected_atomic_weight) > 1e-6:
            raise ValueError(f"milestone {milestone['id']} atomic weight changed: expected {expected_atomic_weight}, observed {atomic_weight}")
        passed_weight = sum(float(check["weight"]) for check in subchecks if check["passed"])
        milestone_weight = float(milestone["weight"])
        aggregated.append(
            {
                "id": milestone["id"],
                "category": milestone["category"],
                "description": milestone["description"],
                "weight": milestone_weight,
                "earned_weight": round(milestone_weight * passed_weight / atomic_weight, 6),
                "passed": all(check["passed"] for check in subchecks),
                "evidence": {
                    "passed_criteria": sum(check["passed"] for check in subchecks),
                    "total_criteria": len(subchecks),
                    "subchecks": subchecks,
                },
            }
        )
    unassigned = sorted(set(by_id) - assigned)
    if unassigned:
        raise ValueError(f"atomic checks omitted from the rubric: {unassigned}")
    return aggregated


def verify_episode(task: dict[str, Any], world: World) -> dict[str, Any]:
    """Compute HubScore for one finished episode from executable checks only."""

    write_tools = world.family.write_tools
    checks: list[dict[str, Any]] = []
    successful = [entry for entry in world.trace if entry["success"]]
    first_write_index = min((entry["index"] for entry in successful if entry["tool"] in write_tools), default=len(world.trace) + 1)

    for investigation in task.get("required_investigations", []):
        missing = missing_required_investigations({"required_investigations": [investigation]}, world.trace, before_index=first_write_index)
        checks.append(
            {
                "id": investigation["id"],
                "description": investigation["description"],
                "weight": float(investigation.get("weight", 1.0)),
                "passed": not missing,
                "evidence": {
                    "satisfied_by": [
                        {"index": entry["index"], "tool": entry["tool"]}
                        for entry in successful
                        if entry["index"] < first_write_index and any(entry["tool"] == call["tool"] for call in investigation["any_of"])
                    ],
                    "missing": [item["id"] for item in missing],
                },
            }
        )

    for verification in task.get("post_write_verifications", []):
        missing_readbacks = missing_post_write_verifications({"post_write_verifications": [verification]}, world.trace)
        checks.append(
            {
                "id": verification["id"],
                "description": verification["description"],
                "weight": float(verification.get("weight", 1.0)),
                "passed": not missing_readbacks,
                "evidence": {
                    "missing": [item["id"] for item in missing_readbacks],
                    "satisfied_by": [
                        {"index": entry["index"], "tool": entry["tool"]}
                        for entry in successful
                        if any(entry["tool"] == requirement["tool"] for requirement in verification.get("any_of", []))
                    ],
                },
            }
        )

    for assertion in task["expected"]["assertions"]:
        rows = _query_rows(world, assertion)
        passed = True
        evidence: dict[str, Any] = {"matching_rows": len(rows)}
        if "count" in assertion:
            passed = len(rows) == assertion["count"]
            evidence["expected_count"] = assertion["count"]
        if "values" in assertion:
            if len(rows) != 1:
                passed = False
                evidence["expected_unique_row"] = True
            else:
                values_passed, mismatches = _values_match(rows[0], assertion["values"])
                passed = passed and values_passed
                evidence["mismatches"] = mismatches
        if len(rows) == 1:
            payload_mismatches = payload_assertion_mismatches(rows[0], assertion)
            passed = passed and not payload_mismatches
            evidence.update(payload_mismatches)
        checks.append(
            {
                "id": assertion["id"],
                "description": assertion["description"],
                "weight": float(assertion.get("weight", 1.0)),
                "passed": passed,
                "evidence": evidence,
            }
        )

    submitted = {
        row["field"]: row["value"]
        for row in world.connection.execute("SELECT field, value FROM answers WHERE task_id = ? ORDER BY field", (task["task_id"],)).fetchall()
    }
    expected_answer = normalize_answer_fields(task, task["expected"]["answer"])
    for criterion in [*task["expected"].get("answer_checks", []), *task["expected"].get("calculations", [])]:
        field = criterion["field"]
        checks.append(
            {
                "id": criterion["id"],
                "description": criterion["description"],
                "weight": float(criterion.get("weight", 1.0)),
                "passed": submitted.get(field) == expected_answer[field],
                "evidence": {"field": field, "expected": expected_answer[field], "submitted": submitted.get(field)},
            }
        )

    written_tables = {
        row["table_name"]
        for row in world.connection.execute("SELECT DISTINCT table_name FROM audit_log WHERE task_id = ?", (task["task_id"],)).fetchall()
    }
    disallowed = sorted(written_tables - set(task["allowed_write_tables"]))
    checks.append(
        {
            "id": "write_scope",
            "description": f"Kept every successful write inside {task['task_id']}'s declared state, collaboration, answer, and audit tables.",
            "weight": 1.0,
            "passed": not disallowed,
            "evidence": {"written_tables": sorted(written_tables), "disallowed": disallowed},
        }
    )
    mutation_errors = [
        {"index": entry["index"], "tool": entry["tool"], "error": entry["result"].get("error")}
        for entry in world.trace
        if not entry["success"] and entry["tool"] in write_tools - {SUBMIT_TOOL} and not entry["result"].get("retryable")
    ]
    checks.append(
        {
            "id": "no_rejected_mutation",
            "description": "Completed without a rejected state-changing call; failed exploratory reads and retryable provider faults do not erase a correct outcome.",
            "weight": 1.0,
            "passed": not mutation_errors,
            "evidence": {"errors": mutation_errors},
        }
    )

    atomic_checks = checks
    milestones = aggregate_milestones(task, atomic_checks)
    passed_milestones = sum(1 for check in milestones if check["passed"])
    passed_weight = sum(float(check["earned_weight"]) for check in milestones)
    total_weight = sum(float(check["weight"]) for check in milestones)
    return {
        "task_id": task["task_id"],
        "metric": METRIC,
        "score": round(passed_weight / total_weight * 100, 2),
        "passed_checks": passed_milestones,
        "total_checks": len(milestones),
        "passed_weight": round(passed_weight, 2),
        "total_weight": round(total_weight, 2),
        "strict_pass": passed_milestones == len(milestones),
        "passed_atomic_checks": sum(check["passed"] for check in atomic_checks),
        "total_atomic_checks": len(atomic_checks),
        "checks": milestones,
    }


__all__ = [
    "METRIC",
    "aggregate_milestones",
    "missing_post_write_verifications",
    "missing_required_investigations",
    "payload_assertion_mismatches",
    "requirement_matches",
    "result_contains",
    "verify_episode",
]
