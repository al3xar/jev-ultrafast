"""Offline contracts for the Jev /health readiness endpoint (T-14 chart readiness).

The K8s pod readiness probe hits GET /health. It must report the SERVICE
state (up + browser-harness/Chrome availability), never a live browser —
`browser-harness --doctor` returns non-zero while idle (no Chrome running),
which would flap the pod Ready/NotReady. So /health checks the binary
presence + harness importability and returns 200 with status=ready (or
degraded) — it never requires a running Chrome.

No paid APIs, no Chrome launch.
"""

import importlib.util

import pytest
from fastapi.testclient import TestClient

from jev_ultrafast.service.app import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("jev_ultrafast.service.Agent", object)
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health_returns_200_and_ready(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ready", "degraded")
    assert "browser_harness" in body
    assert "chrome" in body


def test_health_reports_harness_availability(client):
    assert client.get("/health").json()["browser_harness"] == (
        importlib.util.find_spec("browser_harness") is not None
    )


def test_health_does_not_require_a_live_browser(client):
    # No Chrome is running here — the endpoint must still answer 200/ready.
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["active_sessions"] == 0


def test_health_reports_active_session_count(client, monkeypatch):
    from jev_ultrafast.service import sessions

    class _Reg:
        def active_sessions(self):
            return ["sess-a", "sess-b"]

    monkeypatch.setattr(sessions.SessionRegistry, "active_sessions",
                        lambda self: ["sess-a", "sess-b"])
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["active_sessions"] == 2
