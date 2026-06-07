"""Results contract acceptance (BUILD_SPEC §7): malformed/missing contract
data raises ContractError (the controller maps that to FAILED) — never a
crash, never a silently accepted bad contract."""

from __future__ import annotations

import json

import pytest

from sandkeep.results import ContractError, parse_results

TASK_ID = "abc123"


def _valid(**overrides) -> str:
    data = {
        "task_id": TASK_ID,
        "status": "succeeded",
        "summary": "Added input validation to parse_config().",
        "files_changed": ["parse_config.py"],
        "tests_run": 3,
        "tests_passed": 3,
        "risks": [],
        "followups": ["add property-based tests"],
    }
    data.update(overrides)
    return json.dumps(data)


def test_valid_contract_parses():
    contract = parse_results(_valid(), expected_task_id=TASK_ID)
    assert contract.task_id == TASK_ID
    assert contract.status == "succeeded"
    assert contract.files_changed == ["parse_config.py"]
    assert contract.tests_run == 3
    assert contract.followups == ["add property-based tests"]


def test_optional_fields_default():
    minimal = json.dumps(
        {"task_id": TASK_ID, "status": "failed", "summary": "could not", "files_changed": []}
    )
    contract = parse_results(minimal, expected_task_id=TASK_ID)
    assert contract.tests_run == 0
    assert contract.risks == []
    assert contract.duration_seconds == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "not json",
        "[1, 2]",  # not an object
        json.dumps({"task_id": TASK_ID}),  # missing fields
    ],
)
def test_malformed_contract_raises_not_crashes(bad):
    with pytest.raises(ContractError):
        parse_results(bad, expected_task_id=TASK_ID)


def test_task_id_mismatch_rejected():
    with pytest.raises(ContractError, match="mismatch"):
        parse_results(_valid(task_id="evil"), expected_task_id=TASK_ID)


def test_invalid_status_rejected():
    with pytest.raises(ContractError, match="status"):
        parse_results(_valid(status="partying"), expected_task_id=TASK_ID)


def test_wrong_types_rejected():
    with pytest.raises(ContractError):
        parse_results(_valid(files_changed="parse_config.py"), expected_task_id=TASK_ID)
    with pytest.raises(ContractError):
        parse_results(_valid(tests_run="three"), expected_task_id=TASK_ID)
    with pytest.raises(ContractError):
        parse_results(_valid(summary="  "), expected_task_id=TASK_ID)
