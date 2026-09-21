"""Offline tests for the scope guard and budget clamps (T-5).

The scope guard is the ethical/legal enforcement of the range: Jev decides
navigation in natural language, so nothing else stops a CLICK on an external
link from taking the agent out of audit scope. Every run must carry a
non-empty scope_allowlist and any navigation to a host outside it aborts the
run with status=BLOCKED, blocked_reason="out_of_scope". Budget clamps respect
Jev's hard limits: max_actions <= 60, max_decisions <= 120, exhaustion is
reported as BUDGET_EXCEEDED (a status distinct from BLOCKED in the contract).

No paid APIs are called; pure URL/pattern math plus the contract fixtures.
"""

import pytest

from jev_ultrafast.contract import SubgoalStatus, compress_run
from jev_ultrafast.scope import ScopeError, ScopeGuard, clamp_budget

ALLOW = ["dvwa.range.local"]


def guard():
    return ScopeGuard(ALLOW)


# ---------------------------------------------------------------- the guard


def test_guard_requires_nonempty_allowlist():
    with pytest.raises(ValueError):
        ScopeGuard([])


def test_guard_normalizes_hosts():
    g = ScopeGuard(["  DVWA.Range.Local  ", "https://app.range.local/x", "sub.api.example.org"])
    assert g.allowed("https://dvwa.range.local/vuln.php")
    assert g.allowed("http://app.range.local")
    assert g.allowed("https://sub.api.example.org/anything")
    assert not g.allowed("https://other.example.org/")


def test_guard_subdomains_of_listed_domain_are_in_scope():
    g = guard()
    assert g.allowed("https://dvwa.range.local/login.php")
    assert g.allowed("https://admin.dvwa.range.local/panel")
    assert g.allowed("http://a.b.dvwa.range.local/deep/path?x=1#frag")


def test_guard_lookalike_domains_are_out_of_scope():
    g = guard()
    assert not g.allowed("https://notdvwa.range.local/")
    assert not g.allowed("https://dvwa.range.local.evil.net/")
    assert not g.allowed("https://evil-dvwa.range.local/")
    assert not g.allowed("https://range.local/")


def test_guard_non_http_schemes_out_of_scope():
    g = guard()
    assert not g.allowed("javascript:alert(1)")
    assert not g.allowed("file:///etc/passwd")
    assert not g.allowed("data:text/html;base64,AAAA")
    assert not g.allowed("not a url at all")


def test_guard_detects_external_navigation_and_link_clicks():
    g = guard()
    assert g.is_out_of_scope("https://dvwa.range.local/index.php") is None
    assert "evil.net" in g.is_out_of_scope("https://evil.net/phish")
    assert "evil.net" in g.is_out_of_scope("https://evil.net/a/b?c=d")


def test_guard_raises_scope_error_with_url():
    with pytest.raises(ScopeError) as excinfo:
        guard().ensure_in_scope("https://evil.net/")
    assert "evil.net" in str(excinfo.value)


def test_guard_ensure_in_scope_passes_for_allowed_host():
    guard().ensure_in_scope("https://admin.dvwa.range.local/")  # must not raise


# ------------------------------------------------------------- budget clamps


def test_clamp_budget_caps_at_jev_hard_limits():
    assert clamp_budget(200, 300) == (60, 120)
    assert clamp_budget(61, 121) == (60, 120)


def test_clamp_budget_keeps_smaller_values():
    assert clamp_budget(40, 80) == (40, 80)
    assert clamp_budget(10, 120) == (10, 120)


def test_clamp_budget_none_falls_back_to_hard_limits():
    assert clamp_budget(None, None) == (60, 120)


def test_clamp_budget_rejects_nonsense():
    with pytest.raises(ValueError):
        clamp_budget(0, 10)
    with pytest.raises(ValueError):
        clamp_budget(10, -5)


# ------------------------------------------------- enforcement in the output


def _record(status="blocked", error=None, url="https://dvwa.range.local/vuln.php"):
    return {
        "run_id": "jev-" + "a" * 32,
        "session_id": "campaign-1",
        "url": url,
        "goal": "Set the security level to low",
        "status": status,
        "error": error,
        "elapsed_ms": 900,
        "history": [
            {
                "step": 1,
                "action": "Security Level",
                "kind": "select",
                "operation": "SELECT",
                "target": "5",
                "url": url,
            }
        ],
        "decisions": [{"x": 1}],
        "snapshot": {
            "url": url,
            "title": "DVWA",
            "text": "Security level is now low",
            "elements": [],
        },
        "created_at": 0,
    }


def test_out_of_scope_run_is_blocked_with_reason():
    record = _record(status="blocked")
    record["scope_blocked_url"] = "https://evil.net/phish"
    result = compress_run(record)
    assert result.status is SubgoalStatus.BLOCKED
    assert result.blocked_reason == "out_of_scope"
    assert result.extracted.final_url == "https://evil.net/phish"


def test_budget_exhausted_is_budget_exceeded_not_blocked():
    # agent.py raises 'Stopped at the 60-action demo budget' when max_actions runs out.
    record = _record(status="blocked", error="Stopped at the 60-action demo budget")
    result = compress_run(record)
    assert result.status is SubgoalStatus.BUDGET_EXCEEDED
    assert result.status is not SubgoalStatus.BLOCKED
    assert "budget" in (result.blocked_reason or "").lower()


def test_budget_exceeded_distinct_from_blocked_in_output():
    blocked = compress_run(_record(status="blocked"))
    budget = compress_run(_record(status="blocked", error="Reached the demo's model-call budget"))
    assert blocked.status is SubgoalStatus.BLOCKED
    assert budget.status is SubgoalStatus.BUDGET_EXCEEDED
    assert blocked.model_dump()["status"] != budget.model_dump()["status"]
