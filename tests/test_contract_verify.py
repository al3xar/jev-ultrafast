"""Offline tests for the independent verify block (T-3) of the subgoal contract.

Jev's AGENTS.md: "Verify actual final outcomes independently. A DONE choice is not
proof of success." The verify spec is evaluated against the final snapshot regardless
of the run status. No paid APIs are called; pure transformation over fixtures.
"""

import pytest

from jev_ultrafast.contract import SubgoalStatus, compress_run, verify


def record(**overrides):
    base = {
        "run_id": "jev-" + "b" * 32,
        "session_id": "campaign-1",
        "url": "https://dvwa.range.local/vuln.php",
        "goal": "Set the security level to low",
        "status": "done",
        "error": None,
        "elapsed_ms": 900,
        "history": [
            {"step": 1, "action": "Security Level", "kind": "click", "operation": "SELECT", "url": "https://dvwa.range.local/vuln.php"},
            {"step": 2, "action": "Submit", "kind": "click", "operation": "CLICK", "url": "https://dvwa.range.local/low.php"},
        ],
        "decisions": [{"x": 1}, {"x": 2}],
        "snapshot": {
            "url": "https://dvwa.range.local/low.php",
            "title": "DVWA",
            "text": "Security level is now low\nWelcome, admin",
            "elements": [
                {"index": "1", "label": "User name", "role": "textbox", "value": "admin", "operations": ["TYPE_TEXT"]},
                {"index": "2", "label": "Security Level", "role": "combobox", "value": "low", "operations": ["SELECT"]},
                {"index": "3", "label": "Submit", "role": "checkbox", "checked": "true", "operations": ["CLICK"]},
                {"index": "4", "label": "Tabs", "role": "tab", "selected": "true", "operations": ["CLICK"]},
            ],
        },
        "created_at": 0,
    }
    base.update(overrides)
    return base


def test_no_spec_returns_null():
    assert verify(record(), None) is None


def test_none_kind_returns_null():
    assert verify(record(), {"kind": "none"}) is None


def test_text_present_finds_text_in_final_snapshot():
    spec = {"kind": "text_present", "value": "Security level is now low"}
    assert verify(record(), spec) is True


def test_text_present_absent_text_fails():
    spec = {"kind": "text_present", "value": "First name:"}
    assert verify(record(), spec) is False


def test_text_present_is_case_insensitive_substring():
    spec = {"kind": "text_present", "value": "welcome, admin"}
    assert verify(record(), spec) is True


def test_url_matches_prefix_on_final_url():
    spec = {"kind": "url_matches", "value": "https://dvwa.range.local/low"}
    assert verify(record(), spec) is True


def test_url_matches_regex_on_final_url():
    spec = {"kind": "url_matches", "value": r"^https://dvwa\.range\.local/.*\.php$"}
    assert verify(record(), spec) is True


def test_url_matches_fails_when_final_url_does_not_match():
    spec = {"kind": "url_matches", "value": "https://dvwa.range.local/high.php"}
    assert verify(record(), spec) is False


def test_url_matches_invalid_regex_fails():
    spec = {"kind": "url_matches", "value": "([unclosed"}
    assert verify(record(), spec) is False


def test_element_state_matches_value_by_index():
    spec = {"kind": "element_state", "index": "2", "value": "low"}
    assert verify(record(), spec) is True


def test_element_state_checked_by_index():
    spec = {"kind": "element_state", "index": "3", "checked": "true"}
    assert verify(record(), spec) is True


def test_element_state_selected_by_index():
    spec = {"kind": "element_state", "index": "4", "selected": "true"}
    assert verify(record(), spec) is True


def test_element_state_fails_on_wrong_value():
    spec = {"kind": "element_state", "index": "2", "value": "high"}
    assert verify(record(), spec) is False


def test_element_state_fails_on_out_of_range_index():
    spec = {"kind": "element_state", "index": "99", "value": "low"}
    assert verify(record(), spec) is False


def test_unsupported_kind_raises():
    with pytest.raises(ValueError, match="Unsupported verify kind"):
        verify(record(), {"kind": "telepathy"})


# --- T-3 acceptance criteria, wired through the T-2 response contract -----------


def test_false_done_status_done_but_verified_false():
    """The core T-3 guarantee: Jev chose DONE but the final snapshot does not carry
    the promised text — the planner must see verified=False, not trust status.

    Here the agent declared DONE on the /low.php URL yet the page text still shows the
    level as 'high' and the Security Level element still holds 'high': the outcome was
    never actually achieved, so independent verification must say verified=False."""
    false_done = record(status="done")
    false_done["snapshot"]["text"] = "Security level is still high\nPlease retry"
    false_done["snapshot"]["elements"] = [
        {"index": "2", "label": "Security Level", "role": "combobox", "value": "high", "operations": ["SELECT"]},
    ]
    spec = {"kind": "text_present", "value": "Security level is now low"}
    result = compress_run(false_done, spec)
    assert result.status is SubgoalStatus.DONE
    assert result.verified is False


def test_false_done_element_state_also_catches_it():
    """Same false DONE caught by a different kind: the element still reads 'high', so
    element_state verification fails even though status=DONE."""
    false_done = record(status="done")
    false_done["snapshot"]["elements"] = [
        {"index": "2", "label": "Security Level", "role": "combobox", "value": "high", "operations": ["SELECT"]},
    ]
    spec = {"kind": "element_state", "index": "2", "value": "low"}
    result = compress_run(false_done, spec)
    assert result.status is SubgoalStatus.DONE
    assert result.verified is False


def test_done_and_verified_true_when_outcome_holds():
    spec = {"kind": "url_matches", "value": r"/low\.php$"}
    result = compress_run(record(status="done"), spec)
    assert result.status is SubgoalStatus.DONE
    assert result.verified is True


def test_blocked_partial_run_can_still_verify_true():
    """Blocked mid-run after the level was actually changed: status=BLOCKED but the
    final snapshot proves the subgoal outcome — verified=True. The planner gets both
    facts and decides (re-plan vs accept the partial win) instead of guessing."""
    partial = record(status="blocked")
    partial["history"] = [
        {"step": 1, "action": "Security Level", "kind": "select", "operation": "SELECT", "url": "https://dvwa.range.local/vuln.php"},
        # The agent got stuck afterwards (e.g. a popup) and never reached Submit —
        # but the snapshot below shows the dropdown already on 'low'.
    ]
    partial["snapshot"]["text"] = "Security level set to low (submit pending)"
    partial["snapshot"]["elements"] = [
        {"index": "2", "label": "Security Level", "role": "combobox", "value": "low", "operations": ["SELECT"]},
    ]
    spec = {"kind": "element_state", "index": "2", "value": "low"}
    result = compress_run(partial, spec)
    assert result.status is SubgoalStatus.BLOCKED
    assert result.verified is True
    assert result.blocked_reason is not None


def test_compress_run_without_spec_leaves_verified_null():
    result = compress_run(record())
    assert result.verified is None


def test_compress_run_none_kind_leaves_verified_null():
    result = compress_run(record(), {"kind": "none"})
    assert result.verified is None
