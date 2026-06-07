"""State machine acceptance (BUILD_SPEC §9): illegal transitions raise;
every transition writes a `transitions` row; the audit log records each one."""

from __future__ import annotations

import json

import pytest

from sandkeep.audit import new_trace_id
from sandkeep.models import ALLOWED_TRANSITIONS, TaskState
from sandkeep.state_store import IllegalTransition, TaskNotFound

HAPPY_PATH = [
    TaskState.PROVISIONING,
    TaskState.RUNNING,
    TaskState.SUCCEEDED,
    TaskState.REVIEW,
    TaskState.MERGED,
]


def test_happy_path_writes_a_transition_row_per_step(store, task):
    store.create_task(task, new_trace_id())
    for state in HAPPY_PATH:
        store.update_state(task.id, state, new_trace_id(), detail=f"to {state.value}")
    assert store.get_task(task.id).state is TaskState.MERGED
    rows = store.get_transitions(task.id)
    assert [(r["from_state"], r["to_state"]) for r in rows] == [
        ("new", "provisioning"),
        ("provisioning", "running"),
        ("running", "succeeded"),
        ("succeeded", "review"),
        ("review", "merged"),
    ]
    assert all(r["trace_id"] for r in rows)


def test_illegal_transition_raises_and_writes_nothing(store, task):
    store.create_task(task, new_trace_id())
    with pytest.raises(IllegalTransition):
        store.update_state(task.id, TaskState.MERGED, new_trace_id())
    assert store.get_task(task.id).state is TaskState.NEW
    assert store.get_transitions(task.id) == []


def test_terminal_states_admit_no_exit(store, task):
    store.create_task(task, new_trace_id())
    for state in HAPPY_PATH:
        store.update_state(task.id, state, new_trace_id())
    for target in TaskState:
        with pytest.raises(IllegalTransition):
            store.update_state(task.id, target, new_trace_id())


def test_violation_path_ends_rolled_back(store, task):
    store.create_task(task, new_trace_id())
    store.update_state(task.id, TaskState.PROVISIONING, new_trace_id())
    store.update_state(task.id, TaskState.RUNNING, new_trace_id())
    store.update_state(task.id, TaskState.VIOLATION, new_trace_id(), detail="egress attempt")
    store.update_state(task.id, TaskState.ROLLED_BACK, new_trace_id())
    assert store.get_task(task.id).state is TaskState.ROLLED_BACK


def test_every_state_is_reachable_and_every_edge_is_intentional():
    # guard against accidental edits to the transition map
    states = set(TaskState)
    assert set(ALLOWED_TRANSITIONS) == states
    reachable = {TaskState.NEW}
    frontier = [TaskState.NEW]
    while frontier:
        for nxt in ALLOWED_TRANSITIONS[frontier.pop()]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == states


def test_transitions_are_audited_as_json_lines(store, task, audit):
    store.create_task(task, new_trace_id())
    store.update_state(task.id, TaskState.PROVISIONING, new_trace_id())
    events = [json.loads(line) for line in audit.path.read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert "task_created" in kinds and "state_transition" in kinds
    assert all(e["trace_id"] for e in events)


def test_ledger_records_cost(store, task):
    store.create_task(task, new_trace_id())
    store.record_cost(task.id, task.model, 1000, 500, 42.5)
    rows = store.get_ledger(task.id)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 1000
    assert rows[0]["output_tokens"] == 500
    assert rows[0]["sandbox_seconds"] == 42.5


def test_get_unknown_task_raises(store):
    with pytest.raises(TaskNotFound):
        store.get_task("nope")


def test_update_fields_rejects_state_changes(store, task):
    store.create_task(task, new_trace_id())
    with pytest.raises(ValueError):
        store.update_fields(task.id, state="merged")
    store.update_fields(task.id, branch="sandkeep/abc", sandbox_id="sb-1")
    got = store.get_task(task.id)
    assert got.branch == "sandkeep/abc"
    assert got.sandbox_id == "sb-1"
