"""Offline contracts for per-session browser contexts (T-4).

Fake session hooks — no Chrome, no daemon, no paid APIs. Real-browser persistence is
covered by test_service_session_real.py (the acceptance test that does login then an
authenticated action in a real local Chrome).

Offline we only fake the two injectable hooks (``_session_factory`` / ``_session_release``)
that launch the Chrome and spawn the daemon. Routing helpers stay real (harmless
offline — they only set a string). We assert on ``_session_factory`` call counts, which
is the real "one Chrome per session" signal.
"""

import threading
import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import service as svc
from jev_ultrafast.service import sessions


class FakeSessions:
    """Stand-in for the two injectable browser hooks. Records lifecycle events."""

    def __init__(self):
        self.contexts: dict[str, dict] = {}
        self.created: list[tuple[str, str]] = []  # (session_id, name) in creation order
        self.released: list[str] = []             # session_ids torn down

    def factory(self, ctx):
        self.created.append((ctx.session_id, ctx.name))
        self.contexts[ctx.name] = {"session_id": ctx.session_id, "alive": True, "proc": Mock()}
        ctx.proc = self.contexts[ctx.name]["proc"]

    def release(self, ctx):
        if ctx.name in self.contexts:
            self.contexts[ctx.name]["alive"] = False
        self.released.append(ctx.session_id)


def fake_agent():
    agent = Mock()
    agent.run.return_value = iter([{"status": "done", "page": {"url": "https://dvwa.range.local/x"}}])
    agent.close = Mock()
    agent.snapshot.return_value = {
        "status": "done", "page": {"url": "https://dvwa.range.local/x"}, "history": [], "elapsed_ms": 1,
    }
    agent.state = {"decisions": [], "history": []}
    return agent


@pytest.fixture
def client_factory(monkeypatch):
    """Patch the model + the two injectable session hooks; build() returns a TestClient
    wired to the fake hooks. Pass agent_factory= to override the default fake Agent."""
    from fastapi.testclient import TestClient

    from jev_ultrafast import model
    from jev_ultrafast.service.app import create_app

    fake = FakeSessions()

    def build(agent_factory=None):
        monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
        monkeypatch.setattr(svc.sessions, "_session_factory", fake.factory)
        monkeypatch.setattr(svc.sessions, "_session_release", fake.release)
        agent_factory = agent_factory or (lambda url, goal, **kw: fake_agent())
        monkeypatch.setattr(svc, "Agent", agent_factory)
        return TestClient(create_app())

    return build, fake


def _goal(session_id="cam-1"):
    return {
        "url": "https://dvwa.range.local/login.php",
        "goal": "g",
        "session_id": session_id,
        "scope_allowlist": ["dvwa.range.local"],
        "reuse_session": True,
    }


def test_same_session_id_reuses_one_context(client_factory):
    build, fake = client_factory
    with build() as c:
        r1 = c.post("/run_goal", json=_goal())
        assert r1.status_code == 200
        r2 = c.post("/run_goal", json={**_goal(), "goal": "g2"})
        assert r2.status_code == 200
        # The Chrome (context) is created ONCE for the session, not per run.
        assert [sid for sid, _ in fake.created] == ["cam-1"]
        assert len(fake.created) == 1
        # The reusable context is not torn down between the two runs.
        (_, name) = fake.created[0]
        assert fake.contexts[name]["alive"] is True
        assert fake.released.count("cam-1") == 0


def test_reuse_session_false_is_a_disposable_clean_context(client_factory):
    build, fake = client_factory
    with build() as c:
        c.post("/run_goal", json={**_goal(), "reuse_session": False})
        c.post("/run_goal", json={**_goal(), "reuse_session": False})
        # Two separate disposable contexts (a fresh Chrome + profile each run),
        # each torn down right after its run.
        assert len(fake.created) == 2
        assert len({n for _, n in fake.created}) == 2
        for sid, name in fake.created:
            assert fake.contexts[name]["alive"] is False
        assert fake.released.count("cam-1") == 2


def test_different_session_ids_are_isolated(client_factory):
    build, fake = client_factory
    with build() as c:
        c.post("/run_goal", json=_goal("cam-1"))
        c.post("/run_goal", json=_goal("cam-2"))
    assert [sid for sid, _ in fake.created] == ["cam-1", "cam-2"]
    assert len(fake.created) == 2


def test_concurrent_same_session_serializes(client_factory):
    """Two run_goal calls on the same session must not run the loop in parallel."""
    build, fake = client_factory
    inside = 0
    peak = 0
    intervals = []

    def budget_agent(url, goal, **kw):
        agent = Mock()

        def run():
            nonlocal inside, peak
            inside += 1
            peak = max(peak, inside)
            start = time.monotonic()
            time.sleep(0.4)
            intervals.append((start, time.monotonic()))
            inside -= 1
            yield {"status": "ready", "page": {"url": "https://dvwa.range.local/x"}, "history": [], "elapsed_ms": 1}
            raise ValueError("Stopped at the 1-action demo budget")

        agent.run = run
        agent.close = Mock()
        agent.snapshot.return_value = {
            "status": "blocked", "page": {"url": "https://dvwa.range.local/x"}, "history": [], "elapsed_ms": 1,
        }
        agent.state = {"decisions": [], "history": []}
        return agent

    with build(budget_agent) as c:
        results = {}

        def call(n):
            results[n] = c.post("/run_goal", json={
                "url": "https://dvwa.range.local/x", "goal": f"g{n}",
                "session_id": "cam-conc", "scope_allowlist": ["dvwa.range.local"], "reuse_session": True})

        t1 = threading.Thread(target=call, args=(1,))
        t2 = threading.Thread(target=call, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    assert results[1].status_code == 200 and results[2].status_code == 200
    assert len(intervals) == 2, f"expected 2 run loops, got {len(intervals)}"
    assert peak <= 1, f"run loops ran in parallel (peak={peak}); the browser lock must serialize"
    a, b = intervals[0], intervals[1]
    assert b[0] >= a[1] or a[0] >= b[1], f"run loops overlapped in time: {a} vs {b}"


def test_ttl_expires_and_cleans_contexts(monkeypatch):
    fake = FakeSessions()
    monkeypatch.setattr(svc.sessions, "_session_factory", fake.factory)
    monkeypatch.setattr(svc.sessions, "_session_release", fake.release)
    reg = sessions.SessionRegistry(ttl_s=0.15, sweeper_interval_s=100.0, base_dir="/tmp/jev-t4-test-reg")

    reg.session("cam-ttl", reuse=True)
    assert "cam-ttl" in reg.active_sessions()
    # Let it go idle past the TTL.
    time.sleep(0.3)
    expired = reg.sweep_expired()
    assert expired == ["cam-ttl"], expired
    assert "cam-ttl" not in reg.active_sessions()
    assert fake.released.count("cam-ttl") == 1


def test_release_does_not_drop_a_live_context(client_factory):
    build, fake = client_factory
    with build() as c:
        c.post("/run_goal", json=_goal())
        # Reusable context still usable: created once and still alive.
        (sid, name) = fake.created[0]
        assert sid == "cam-1"
        assert fake.contexts[name]["alive"] is True
        assert fake.released.count("cam-1") == 0


def test_invalid_session_id_is_rejected(monkeypatch):
    fake = FakeSessions()
    monkeypatch.setattr(svc.sessions, "_session_factory", fake.factory)
    monkeypatch.setattr(svc.sessions, "_session_release", fake.release)
    reg = sessions.SessionRegistry(base_dir="/tmp/jev-t4-test-invalid")
    with pytest.raises(ValueError):
        reg.session("bad/session id")
    with pytest.raises(ValueError):
        reg.session("")
    assert fake.created == []
