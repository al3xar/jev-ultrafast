"""Offline tests: scope enforcement and budget clamps on /run_goal (T-5).

No paid APIs are called; the Agent is faked exactly as in the T-1 service
tests. The guard must catch: (1) a requested URL already out of scope,
(2) a mid-run navigation (CLICK) to an external host, (3) subdomain coverage,
and (4) budget clamps to 60 actions / 120 decisions.
"""

from unittest.mock import Mock

from fastapi.testclient import TestClient

from jev_ultrafast import model
from jev_ultrafast import service as svc
from jev_ultrafast.service.app import create_app


def page(url):
    return {
        "url": url,
        "title": "Page",
        "text": "text",
        "scroll": {"y": 0},
        "fingerprint": "f" * 64,
        "actions": [{"id": "e1", "kind": "click", "label": "Link", "role": "link", "value": "", "node": 1}],
    }


def fake_agent(navigate_to=None):
    """A fake agent: yields one in-scope state; optionally a second state whose
    page URL is `navigate_to` (simulating a CLICK that leaves the scope)."""
    states = [{"status": "ready", "page": page("https://dvwa.range.local/login.php"), "history": [], "elapsed_ms": 5}]
    if navigate_to:
        states.append({"status": "predicted", "page": page(navigate_to), "history": [], "elapsed_ms": 10})
    states.append({"status": "done", "page": page("https://dvwa.range.local/low.php"), "history": [], "elapsed_ms": 15})
    agent = Mock()
    agent.run.return_value = iter(states)
    agent.close = Mock()
    agent.snapshot.return_value = states[-1]
    return agent


def client_for(agent, monkeypatch):
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: agent)
    app = create_app()
    return TestClient(app)


def post(client, url="https://dvwa.range.local/login.php", **extra):
    body = {
        "url": url,
        "goal": "Set the security level to low",
        "session_id": "campaign-1",
        "scope_allowlist": ["dvwa.range.local"],
    }
    body.update(extra)
    return client.post("/run_goal", json=body)


def test_requested_url_out_of_scope_is_blocked_before_any_browser(monkeypatch):
    agent = fake_agent()
    client = client_for(agent, monkeypatch)
    response = post(client, url="https://evil.net/phish")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "out_of_scope"
    assert body["blocked_url"] == "https://evil.net/phish"
    agent.run.assert_not_called()  # the agent loop never started
    assert client.get(f"/get_evidence/{body['run_id']}").status_code == 200


def test_mid_run_navigation_out_of_scope_aborts_the_run(monkeypatch):
    agent = fake_agent(navigate_to="https://evil.net/leak")
    client = client_for(agent, monkeypatch)
    response = post(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "out_of_scope"
    assert body["blocked_url"] == "https://evil.net/leak"
    evidence = client.get(f"/get_evidence/{body['run_id']}").json()
    assert evidence["status"] == "blocked"
    assert evidence["scope_blocked_url"] == "https://evil.net/leak"


def test_subdomain_navigation_is_within_scope(monkeypatch):
    agent = fake_agent(navigate_to="https://admin.dvwa.range.local/panel")
    client = client_for(agent, monkeypatch)
    response = post(client, url="https://admin.dvwa.range.local/panel")
    body = response.json()
    assert body["status"] == "done"
    assert "blocked_reason" not in body


def test_lookalike_host_is_out_of_scope(monkeypatch):
    for evil in ("https://dvwa.range.local.evil.net/", "https://evil-dvwa.range.local/", "https://range.local/"):
        agent = fake_agent()
        client = client_for(agent, monkeypatch)
        body = post(client, url=evil).json()
        assert body["status"] == "blocked", evil
        assert body["blocked_reason"] == "out_of_scope", evil
        agent.run.assert_not_called()


def test_non_http_scheme_is_out_of_scope(monkeypatch):
    agent = fake_agent()
    client = client_for(agent, monkeypatch)
    body = post(client, url="javascript:alert(1)").json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "out_of_scope"
    agent.run.assert_not_called()


def test_max_actions_is_clamped_to_60(monkeypatch):
    agent = fake_agent()
    client = client_for(agent, monkeypatch)
    body = post(client, max_actions=200, max_decisions=300).json()
    assert body["max_actions"] == 60
    assert body["max_decisions"] == 120
    evidence = client.get(f"/get_evidence/{body['run_id']}").json()
    assert evidence["max_actions"] == 60
    assert evidence["max_decisions"] == 120


def test_smaller_budgets_are_kept(monkeypatch):
    agent = fake_agent()
    client = client_for(agent, monkeypatch)
    body = post(client, max_actions=40, max_decisions=80).json()
    assert body["max_actions"] == 40
    assert body["max_decisions"] == 80


def test_empty_allowlist_is_rejected_422(monkeypatch):
    agent = fake_agent()
    client = client_for(agent, monkeypatch)
    assert post(client, scope_allowlist=[]).status_code == 422
    agent.run.assert_not_called()


def test_compress_run_maps_scope_abort_to_blocked_out_of_scope():
    # Contract level (T-2 output): scope abort -> BLOCKED/out_of_scope, and the
    # rejected URL is reported as the final url.
    from jev_ultrafast.contract import SubgoalStatus, compress_run

    record = {
        "run_id": "jev-" + "c" * 32,
        "session_id": "campaign-1",
        "url": "https://dvwa.range.local/login.php",
        "goal": "Set the security level to low",
        "status": "done",  # whatever the agent believed at the abort point
        "error": None,
        "elapsed_ms": 15,
        "history": [],
        "snapshot": {"url": "https://evil.net/leak", "title": "Leak", "text": "", "elements": []},
        "scope_blocked_url": "https://evil.net/leak",
        "created_at": 0,
    }
    result = compress_run(record)
    assert result.status is SubgoalStatus.BLOCKED
    assert result.blocked_reason == "out_of_scope"
    assert result.extracted.final_url == "https://evil.net/leak"
    # A non-scope run of the same shape would have been DONE:
    record.pop("scope_blocked_url")
    record["status"] = "done"
    assert compress_run(record).status is SubgoalStatus.DONE
