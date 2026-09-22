"""T-4b: /run_goal must store per-step screenshots and the T-2 evidence_hash in the
record returned by /get_evidence/{run_id}. Offline: the Agent is faked, model.post_json
is patched to AssertionError (no paid APIs), and the session browser hooks are no-op'd
by the shared conftest."""

import base64
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from jev_ultrafast import model
from jev_ultrafast import service as svc
from jev_ultrafast.contract import compress_run
from jev_ultrafast.service.app import create_app

_JPEG = b"\xff\xd8\xff\xe0fakejpeg"


def page(url="https://range.test/login", shot=None):
    p = {
        "url": url,
        "title": "Login",
        "text": "User name\nPassword",
        "scroll": {"y": 0},
        "fingerprint": "f" * 64,
        "actions": [
            {"id": "e1", "kind": "fill", "label": "User name", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Login", "role": "button", "value": "", "node": 20},
        ],
    }
    if shot is not None:
        p["screenshot"] = shot
    return p


def fake_agent_with_shots(status_after_run="done", n_steps=3):
    """Fake Agent whose run() yields n states, each carrying a page screenshot (base64)."""
    states = []
    for step in range(1, n_steps + 1):
        shot = base64.b64encode(_JPEG + step.to_bytes(1, "big")).decode()
        states.append({
            "status": "predicted" if step < n_steps else status_after_run,
            "page": page(shot=shot),
            "goal": "Log in as admin",
            "history": [
                {
                    "step": i,
                    "action": "User name",
                    "kind": "fill",
                    "choice": "e1",
                    "operation": "TYPE_TEXT",
                    "target": "1",
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
    return agent


def post_run_goal(client, **extra):
    body = {
        "url": "https://range.test/login",
        "goal": "Log in as admin",
        "session_id": "campaign-1",
        "scope_allowlist": ["range.test"],
    }
    body.update(extra)
    response = client.post("/run_goal", json=body)
    assert response.status_code == 200
    return response.json()


def get_evidence(client, run_id):
    response = client.get(f"/get_evidence/{run_id}")
    assert response.status_code == 200
    return response.json()


def test_screenshots_true_agent_constructed_with_screenshots_true(monkeypatch):
    seen = {}

    def spy(url, goal, **kw):
        seen.update(kw)
        return fake_agent_with_shots()

    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    monkeypatch.setattr(svc, "Agent", spy)
    app = create_app()
    with TestClient(app) as client:
        post_run_goal(client, screenshots=True)
    assert seen.get("screenshots") is True


def test_screenshots_false_agent_constructed_with_screenshots_false(monkeypatch):
    seen = {}

    def spy(url, goal, **kw):
        seen.update(kw)
        return fake_agent_with_shots()

    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    monkeypatch.setattr(svc, "Agent", spy)
    app = create_app()
    with TestClient(app) as client:
        post_run_goal(client)  # screenshots omitted -> default False
    assert seen.get("screenshots") is False


def test_screenshots_true_record_has_one_base64_item_per_step(monkeypatch):
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: fake_agent_with_shots(n_steps=3))
    app = create_app()
    with TestClient(app) as client:
        run_id = post_run_goal(client, screenshots=True)["run_id"]
        record = get_evidence(client, run_id)
    assert isinstance(record["screenshots"], list)
    assert len(record["screenshots"]) == 3  # one item per observed step, not just the final one
    for i, item in enumerate(record["screenshots"], start=1):
        assert isinstance(item, str)
        assert base64.b64decode(item) == _JPEG + i.to_bytes(1, "big")  # ordered, step i


def test_screenshots_false_record_is_empty_list_even_if_page_carried_shots(monkeypatch):
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: fake_agent_with_shots(n_steps=3))
    app = create_app()
    with TestClient(app) as client:
        run_id = post_run_goal(client, screenshots=False)["run_id"]
        record = get_evidence(client, run_id)
    assert record["screenshots"] == []  # no captures collected when not requested


def test_out_of_scope_pre_browser_record_has_empty_screenshots():
    """The run refused before the browser opens still stores an evidence record."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/run_goal",
            json={
                "url": "https://evil.test/login",
                "goal": "x",
                "session_id": "campaign-1",
                "scope_allowlist": ["range.test"],
                "screenshots": True,
            },
        )
        assert response.status_code == 200
        record = get_evidence(client, response.json()["run_id"])
    assert record["status"] == "blocked"
    assert record["screenshots"] == []


def test_evidence_hash_present_and_consistent_with_compress_run(monkeypatch):
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: fake_agent_with_shots(n_steps=2))
    app = create_app()
    with TestClient(app) as client:
        run_id = post_run_goal(client, screenshots=True)["run_id"]
        record = get_evidence(client, run_id)
    assert record["evidence_hash"].startswith("sha256:")
    assert len(record["evidence_hash"].removeprefix("sha256:")) == 64
    # Coherent with the T-2 contract: recomputing the digest over the same stored
    # record yields the identical hash (deterministic, offline).
    assert record["evidence_hash"] == compress_run(record).evidence_hash


def test_evidence_hash_present_without_screenshots(monkeypatch):
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: fake_agent_with_shots(n_steps=2))
    app = create_app()
    with TestClient(app) as client:
        run_id = post_run_goal(client)["run_id"]
        record = get_evidence(client, run_id)
    assert record["evidence_hash"].startswith("sha256:")
    assert record["evidence_hash"] == compress_run(record).evidence_hash


def test_session_and_scope_semantics_unchanged_with_screenshots(monkeypatch):
    """reuse_session still routes through the registry and scope guard still aborts
    out-of-scope navigation mid-run (T-4/T-5 semantics intact)."""
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))

    states = [
        {"status": "predicted", "page": page("https://range.test/login", shot=base64.b64encode(_JPEG).decode()),
         "history": [{"step": 1, "operation": "TYPE_TEXT", "target": "1", "action": "User name", "url": "https://range.test/login"}],
         "elapsed_ms": 10},
        {"status": "predicted", "page": page("https://evil.test/x"),
         "history": [{"step": 2, "operation": "CLICK", "target": "2", "action": "Login", "url": "https://evil.test/x"}],
         "elapsed_ms": 20},
    ]
    agent = Mock()
    agent.run.return_value = iter(states)
    agent.close = Mock()
    agent.snapshot.return_value = {**states[-1], "elements": []}
    monkeypatch.setattr(svc, "Agent", lambda url, goal, **kw: agent)
    app = create_app()
    with TestClient(app) as client:
        body = post_run_goal(client, screenshots=True, reuse_session=True,
                             url="https://range.test/login")
        record = get_evidence(client, body["run_id"])
    assert record["status"] == "blocked"
    assert record["scope_blocked_url"] == "https://evil.test/x"
    assert body["session_id"] == "campaign-1"
    # Only the in-scope steps were observed before the abort.
    assert len(record["screenshots"]) == 1
    agent.close.assert_called_once()
