"""Parse + validate the results contract (BUILD_SPEC §7).

The contract JSON and the patch are the ONLY things that cross back from
the sandbox. A missing or malformed contract is a FAILED task, never a
crash — callers catch ContractError and transition accordingly.
"""

from __future__ import annotations

import json

from .models import ResultsContract

VALID_STATUSES = frozenset({"succeeded", "failed", "timeout", "violation"})


class ContractError(Exception):
    pass


def parse_results(text: str, *, expected_task_id: str) -> ResultsContract:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractError(f"results.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("results.json must be a JSON object")

    for required in ("task_id", "status", "summary", "files_changed"):
        if required not in data:
            raise ContractError(f"results.json missing required field: {required}")

    if data["task_id"] != expected_task_id:
        raise ContractError(
            f"task_id mismatch: contract says {data['task_id']!r}, expected {expected_task_id!r}"
        )
    if data["status"] not in VALID_STATUSES:
        raise ContractError(f"invalid status: {data['status']!r}")
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        raise ContractError("summary must be a non-empty string")
    if not isinstance(data["files_changed"], list) or not all(
        isinstance(f, str) for f in data["files_changed"]
    ):
        raise ContractError("files_changed must be a list of strings")

    def _int(key: str) -> int:
        value = data.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ContractError(f"{key} must be an integer")
        return value

    def _str_list(key: str) -> list[str]:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ContractError(f"{key} must be a list of strings")
        return value

    return ResultsContract(
        task_id=data["task_id"],
        status=data["status"],
        summary=data["summary"],
        files_changed=data["files_changed"],
        tests_run=_int("tests_run"),
        tests_passed=_int("tests_passed"),
        risks=_str_list("risks"),
        followups=_str_list("followups"),
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        duration_seconds=float(data.get("duration_seconds", 0.0)),
    )
