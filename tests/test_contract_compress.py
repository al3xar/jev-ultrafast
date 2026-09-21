"""Offline contracts for compress_run: the compressed subgoal response of plan section 2.2.

The input is the run state/record produced by the Jev service (T-1): run_id, Jev status,
history, decisions, final snapshot. No paid APIs are called; pure transformation over fixtures.
"""

from jev_ultrafast.contract import SubgoalResult, SubgoalStatus, compress_run


def elements():
    return [
        {"index": "1", "label": "User name", "role": "textbox"},
        {"index": "2", "label": "Password", "role": "textbox"},
        {"index": "3", "label": "Login", "role": "button"},
    ]


def history(n=3):
    return [
        {
            "step": i,
            "action": "User name" if i == 1 else "Password",
            "kind": "fill" if i < n else "click",
            "operation": "TYPE_TEXT" if i < n else "CLICK",
            "page_changed": True,
            "url": "https://range.test/login",
            "latency_ms": 10 * i,
        }
        for i in range(1, n + 1)
    ]


def record(**overrides):
    base = {
        "run_id": "jev-" + "a" * 32,
        "session_id": "campaign-1",
        "url": "https://range.test/login",
        "goal": "Log in as admin",
        "status": "done",
        "error": None,
        "elapsed_ms": 2210,
        "history": history(),
        "decisions": [{"x": 1}] * 5,
        "snapshot": {
            "url": "https://range.test/login",
            "title": "Login",
            "text": "Welcome\nFirst name: admin\nSurname: admin",
            "elements": elements(),
        },
        "created_at": 0,
    }
    base.update(overrides)
    return base


def test_done_state_produces_done_result():
    result = compress_run(record())
    assert isinstance(result, SubgoalResult)
    assert result.run_id == "jev-" + "a" * 32
    assert result.status is SubgoalStatus.DONE
    assert result.verified is None  # T-3 fills verified; pre-reserved here as null
    assert result.blocked_reason is None
    assert result.attack_tactic is None
    assert result.extracted.final_url == "https://range.test/login"
    assert result.extracted.forms_seen == 2
    assert "First name: admin" in result.extracted.reflected_text


def test_blocked_state_maps_to_blocked_with_reason():
    state = record(status="blocked")
    result = compress_run(state)
    assert result.status is SubgoalStatus.BLOCKED
    assert result.blocked_reason
    assert result.blocked_reason.lower()


def test_budget_exhaustion_error_maps_to_budget_exceeded():
    state = record(status="ready", error="Stopped at the 60-action demo budget", history=history(60))
    result = compress_run(state)
    assert result.status is SubgoalStatus.BUDGET_EXCEEDED
    assert result.budget.actions_used == 60
    assert "budget" in (result.blocked_reason or "").lower()


def test_model_call_budget_exhaustion_maps_to_budget_exceeded():
    state = record(status="ready", error="Reached the demo's model-call budget")
    result = compress_run(state)
    assert result.status is SubgoalStatus.BUDGET_EXCEEDED


def test_interrupted_loop_without_budget_error_maps_to_failed():
    state = record(status="ready", error="Dropdown execution was interrupted; inspect before retrying.")
    result = compress_run(state)
    assert result.status is SubgoalStatus.FAILED
    assert "interrupted" in (result.blocked_reason or "")


def test_run_id_is_synthesized_when_absent():
    state = record()
    del state["run_id"]
    result = compress_run(state)
    assert result.run_id.startswith("jev-")
    assert len(result.run_id.removeprefix("jev-")) == 32


def test_trace_and_screenshots_are_absent_from_main_payload():
    result = compress_run(record())
    payload = result.model_dump()  # full serialization: the contract has no trace/screenshots fields at all
    assert "trace" not in payload
    assert "screenshots" not in payload


def test_summary_is_short_natural_language():
    result = compress_run(record())
    assert 0 < len(result.summary) <= 500
    assert "Log in as admin" in result.summary  # the planner must see what the run was about


def test_budget_counters_come_from_history_and_decisions():
    result = compress_run(record())
    assert result.budget.actions_used == 3
    assert result.budget.decisions_used == 5
    assert result.budget.elapsed_ms == 2210


def test_evidence_hash_is_a_sha256_hex():
    result = compress_run(record())
    assert result.evidence_hash.startswith("sha256:")
    assert len(result.evidence_hash.removeprefix("sha256:")) == 64


def test_payload_validates_against_the_schema():
    for state, expected in [
        (record(), SubgoalStatus.DONE),
        (record(status="blocked"), SubgoalStatus.BLOCKED),
        (record(status="ready", error="Stopped at the 60-action demo budget"), SubgoalStatus.BUDGET_EXCEEDED),
        (record(status="ready", error="crash"), SubgoalStatus.FAILED),
    ]:
        result = compress_run(state)
        round_tripped = SubgoalResult.model_validate(result.model_dump())
        assert round_tripped.status is expected
        assert len(round_tripped.summary) <= 500


def test_reflected_text_is_bounded():
    long_text = "x" * 900
    state = record(snapshot={"url": "u", "title": "t", "text": long_text, "elements": []})
    result = compress_run(state)
    assert len(result.extracted.reflected_text) <= 500
