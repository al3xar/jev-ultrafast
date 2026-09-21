"""Offline contract: /extract_surface is one indexed snapshot, never a loop run."""

from unittest.mock import Mock

from fastapi.testclient import TestClient

from jev_ultrafast import model
from jev_ultrafast import service as svc
from jev_ultrafast.service.app import create_app


def surface_page():
    return {
        "url": "https://range.test/vulnerabilities/sqli/",
        "title": "SQL Injection",
        "text": "User ID",
        "scroll": {"y": 0},
        "fingerprint": "a" * 64,
        "actions": [
            {"id": "e1", "kind": "fill", "label": "User ID", "role": "textbox", "value": "", "node": 5},
            {"id": "e2", "kind": "click", "label": "Submit", "role": "button", "value": "", "node": 6},
            {"id": "e3", "kind": "select", "label": "Security Level", "role": "combobox", "value": "", "node": 7},
        ],
    }


def fake_agent():
    agent = Mock()
    agent.run = Mock(side_effect=AssertionError("extract_surface must not run the agent loop"))
    elements = [{"index": "1", "label": "User ID"}]
    agent.snapshot.return_value = {"status": "ready", "page": surface_page(), "elements": elements}
    agent.close = Mock()
    return agent


def test_extract_surface_returns_one_indexed_snapshot_without_loop(monkeypatch):
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    agent = fake_agent()
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: agent)
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/extract_surface",
            json={"url": "https://range.test/vulnerabilities/sqli/", "session_id": "campaign-1"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "campaign-1"
    assert body["url"] == "https://range.test/vulnerabilities/sqli/"
    assert body["elements"] == [{"index": "1", "label": "User ID"}]
    assert body["run_id"] is None
    agent.run.assert_not_called()
    agent.close.assert_called_once()
