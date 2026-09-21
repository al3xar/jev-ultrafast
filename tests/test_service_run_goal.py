"""Offline contracts for the Jev HTTP service. The Agent is faked; no paid APIs are called."""

import uuid
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from jev_ultrafast import model
from jev_ultrafast import service as svc
from jev_ultrafast.service.app import create_app


def page(url="https://range.test/login"):
    p = {
        "url": url,
        "title": "Login",
        "text": "User name\nPassword",
        "scroll": {"y": 0},
        "fingerprint": "f" * 64,
        "actions": [
            {"id": "e1", "kind": "fill", "label": "User name", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "fill", "label": "Password", "role": "textbox", "value": "", "node": 11},
            {"id": "e3", "kind": "click", "label": "Login", "role": "button", "value": "", "node": 20},
        ],
    }
    return p


def fake_agent(status_after_run="done", decisions=2):
    """A fake Agent: a context manager whose run() yields states and ends with the requested status."""
    states = []
    for step in range(1, decisions + 1):
        states.append({
            "status": "predicted" if step < decisions else status_after_run,
            "page": page(),
            "goal": "Log in as admin",
            "history": [
                {
                    "step": i,
                    "action": "User name" if i == 1 else "Password",
                    "kind": "fill",
                    "choice": "e1",
                    "operation": "TYPE_TEXT",
                    "target": "1",
                    "text": "admin" if i == 1 else "password",
                    "latency_ms": 10,
                    "elapsed_ms": 10 * i,
                    "url": page()["url"],
                    "page_changed": True,
                }
                for i in range(1, step + 1)
            ],
            "elapsed_ms": 10 * step,
        })
    agent = Mock()
    agent.run.return_value = iter(states)
    agent.close = Mock()
    agent.snapshot.return_value = {**states[-1], "elements": [{"index": "1", "label": "User name"}]}
    agent.state = {"status": status_after_run, "decisions": [{"x": 1}] * decisions, "history": states[-1]["history"]}
    return agent


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    agent = fake_agent()
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: agent)
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_run_goal_returns_run_id_and_done_status(client):
    response = client.post(
        "/run_goal",
        json={"url": "https://range.test/login", "goal": "Log in as admin", "session_id": "campaign-1",
              "scope_allowlist": ["range.test"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["run_id"].startswith("jev-")
    assert uuid.UUID(body["run_id"].removeprefix("jev-"))  # a generated uuid
    assert body["session_id"] == "campaign-1"
    assert body["elapsed_ms"] == 20


def test_run_goal_stores_history_and_final_snapshot(client):
    body = client.post(
        "/run_goal",
        json={"url": "https://range.test/login", "goal": "Log in as admin", "session_id": "campaign-1",
              "scope_allowlist": ["range.test"]},
    ).json()
    run_id = body["run_id"]
    evidence = client.get(f"/get_evidence/{run_id}")
    assert evidence.status_code == 200
    data = evidence.json()
    assert data["run_id"] == run_id
    assert data["status"] == "done"
    assert [h["action"] for h in data["history"]] == ["User name", "Password"]
    assert data["snapshot"]["url"] == "https://range.test/login"
    assert data["session_id"] == "campaign-1"


def test_run_goal_blocked_status_is_reported(client, monkeypatch):
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: fake_agent(status_after_run="blocked"))
    body = client.post(
        "/run_goal",
        json={"url": "https://range.test/", "goal": "Open file upload", "session_id": "campaign-2",
              "scope_allowlist": ["range.test"]},
    ).json()
    assert body["status"] == "blocked"
    assert body["run_id"].startswith("jev-")


def test_run_goal_unknown_run_id_is_404(client):
    assert client.get(f"/get_evidence/{uuid.uuid4()}").status_code == 404


def test_run_goal_requires_url_and_goal(client):
    assert client.post("/run_goal", json={"goal": "x"}).status_code == 422
    assert client.post("/run_goal", json={"url": "https://range.test/"}).status_code == 422


def test_agent_factory_receives_url_and_goal(client, monkeypatch):
    seen = {}

    def spy(url, goal, **kw):
        seen["url"], seen["goal"] = url, goal
        return fake_agent()

    monkeypatch.setattr(svc, "Agent", spy)
    client.post("/run_goal", json={"url": "https://range.test/x", "goal": "Goal text", "session_id": "c",
                                   "scope_allowlist": ["range.test"]})
    assert seen == {"url": "https://range.test/x", "goal": "Goal text"}


def test_run_goal_stores_state_and_error_when_loop_raises_mid_run(client, monkeypatch):
    agent = fake_agent(status_after_run="ready")
    states = iter([{"status": "ready", "page": page(), "history": [], "elapsed_ms": 10}])

    def broken_run():
        yield from states
        raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")

    agent.run = Mock(return_value=broken_run())
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: agent)
    body = client.post(
        "/run_goal",
        json={"url": "https://range.test/d", "goal": "Change the dropdown", "session_id": "campaign-3",
              "scope_allowlist": ["range.test"]},
    ).json()
    assert body["run_id"].startswith("jev-")
    assert body["status"] == "ready"
    assert "interrupted" in body["error"]
    assert client.get(f"/get_evidence/{body['run_id']}").status_code == 200
    agent.close.assert_called_once()
