"""Real-browser acceptance test for per-session context persistence (T-4).

This is the test that proves the critical gap is closed: in a real local Chrome,
two consecutive ``run_goal`` calls on the SAME session_id must share one browser
context (cookies/CSRF/profile), so the second run is *already* authenticated and
never has to log in again. A pentest is login -> set level -> attack; if every
subgoal opened a clean Chrome the campaign would never get past the login.

How it stays model-free (repo rule: tests must not hit paid APIs):
- The session/browser layer is REAL — the two injectable hooks are restored to
  ``_real_session_factory`` / ``_real_session_release`` (dedicated headless Chrome
  per session + named browser-harness daemon bound to it), exactly what ``/run_goal``
  uses. This is "estilo scripts/check_guards.py": a real browser, local only.
- The Agent is replaced by a *scripted* agent that drives the real ``Browser`` with
  fixed actions (no model decision). ``model.post_json`` is patched to raise if it
  is ever called, so any accidental paid call fails the test loudly.

The HTTP path is exercised end to end through ``create_app()`` + ``/run_goal`` — the
same ``registry.run(session_id, reuse, fn)`` call the endpoint makes — so this is a
faithful proof that ``reuse_session=true`` keeps the campaign's browser state alive
between subgoals while ``reuse_session=false`` hands out a clean context.
"""

import http.server
import shutil
import socketserver
import threading
from unittest.mock import Mock

import pytest

from jev_ultrafast import model
from jev_ultrafast import service as svc
from jev_ultrafast.browser import Browser
from jev_ultrafast.service.app import create_app

pytestmark = pytest.mark.skipif(
    shutil.which("google-chrome") is None,
    reason="real-browser acceptance test needs a Chrome binary",
)


class _PagesHandler(http.server.BaseHTTPRequestHandler):
    """Same-origin page. Shows an 'authenticated' banner + a report button ONLY
    when the session cookie is present; otherwise 'anonymous' with no button. This
    mirrors the login -> protected-module flow without a real web app."""

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_GET(self):
        cookie = self.headers.get("Cookie", "")
        if "auth=" in cookie:
            who = "authenticated"
            extra = (
                "<button id='report' "
                "onclick=\"document.getElementById('r').textContent='report-issued'\">"
                "Download report</button><p id='r'>none</p>"
            )
        else:
            who = "anonymous"
            extra = "<p id='r'>none</p>"
        body = (
            f"<html><head><title>Range</title></head><body><h1>Range</h1>"
            f"<p id='who'>{who}</p>{extra}</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def http_origin():
    """A same-origin HTTP server on a random 127.0.0.1 port, for the whole module."""
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _PagesHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def real_service(monkeypatch, http_origin):
    """create_app() with REAL browser hooks + a scripted (model-free) Agent.

    ``CAPTURE`` records what each run actually observed so the test can assert on
    the persisted state. The base URL is in scope; the registry is the app's own,
    torn down at context exit (lifespan -> shutdown stops Chrome + daemons).
    """
    from fastapi.testclient import TestClient

    cap = {}

    def page_now():
        return {
            "url": cap.get("url", http_origin + "/"),
            "title": "Range",
            "text": "",
            "scroll": {"y": 0},
            "fingerprint": "f" * 64,
            "actions": [],
        }

    class ScriptedAgent:
        """Drives the real Browser with fixed actions per goal; never calls a model."""

        def __init__(self, url, goal, *, screenshots=False):
            self.goal = goal.strip()
            # Mirrors the real Agent's screenshots kwarg (T-4b); the scripted run
            # never captures, so the evidence record's screenshots list stays empty.
            self.screenshots = screenshots
            self.browser = Browser(url)  # REAL browser bound to the session's Chrome

        def run(self):
            if self.goal == "LOGIN":
                # A successful login issues the session cookie on the origin.
                self.browser.evaluate('document.cookie = "auth=tok123; path=/"; 0')
            else:  # "AUTHED": an action that only exists while authenticated.
                cap["who"] = self.browser.evaluate("document.getElementById('who').textContent")
                page = self.browser.observe(screenshot=False)
                report = next(a for a in page["actions"] if a.get("label") == "Download report")
                self.browser.act(report, page)
                cap["report"] = self.browser.evaluate("document.getElementById('r').textContent")
            cap["url"] = self.browser.evaluate("location.href")
            cap["cookie"] = self.browser.evaluate("document.cookie")
            yield {
                "status": "done",
                "page": page_now(),
                "history": [],
                "elapsed_ms": 1,
            }

        def snapshot(self):
            return {"status": "done", "page": page_now(), "history": [], "elapsed_ms": 1, "elements": []}

        def close(self):
            self.browser.close()

    # Restore the real browser hooks (the autouse conftest fixture no-ops them by
    # default). Applied in the test body, so this wins over the fixture.
    monkeypatch.setattr(svc.sessions, "_session_factory", svc.sessions._real_session_factory)
    monkeypatch.setattr(svc.sessions, "_session_release", svc.sessions._real_session_release)
    monkeypatch.setattr(model, "post_json", Mock(side_effect=AssertionError("paid API called in tests")))
    monkeypatch.setattr(svc, "Agent", ScriptedAgent)
    cap["origin"] = http_origin
    app = create_app()
    with TestClient(app) as client:
        client._cap = cap
        yield client


def _run(client, goal, session_id, reuse=True):
    return client.post(
        "/run_goal",
        json={
            "url": client._cap["origin"] + "/",
            "goal": goal,
            "session_id": session_id,
            "scope_allowlist": ["127.0.0.1"],
            "reuse_session": reuse,
        },
    )


def test_session_persists_across_two_run_goals(real_service):
    """Two run_goal on the same session_id: run 1 logs in, run 2 is already
    authenticated and performs a protected action WITHOUT logging in again."""
    r1 = _run(real_service, "LOGIN", "cam-auth")
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "done"

    # The login run stored the session cookie in the session's Chrome profile.
    assert "auth=tok123" in real_service._cap["cookie"]

    # Run 2: a fresh Browser instance on the SAME session. It must be authenticated
    # (the cookie persisted) and be able to perform the protected action.
    r2 = _run(real_service, "AUTHED", "cam-auth")
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "done"

    cap = real_service._cap
    assert cap["who"] == "authenticated", f"second run lost the session (who={cap['who']!r})"
    assert cap["report"] == "report-issued", "authenticated action failed"
    assert "auth=tok123" in cap["cookie"], "cookie did not persist into the second run"


def test_reuse_session_false_is_a_clean_context(real_service):
    """reuse_session=false must hand out a clean, disposable context: no carried
    cookies, and the context is torn down right after the run (not reused)."""
    # First establish a persistent, authenticated session to prove contrast.
    assert _run(real_service, "LOGIN", "cam-persist").json()["status"] == "done"
    assert _run(real_service, "AUTHED", "cam-persist").json()["status"] == "done"
    assert real_service._cap["who"] == "authenticated"

    # Now a disposable run on a different session_id: it must start clean.
    assert _run(real_service, "AUTHED", "cam-disposable", reuse=False).json()["status"] == "done"
    assert real_service._cap["who"] == "anonymous", (
        "a reuse_session=false run must not inherit another session's cookies"
    )
    # The disposable context is torn down immediately, not kept for reuse.
    registry = real_service.app.state.session_registry
    assert "cam-disposable" not in registry.active_sessions()


def test_different_session_ids_are_isolated(real_service):
    """A second campaign on a different session_id must not see the first one's
    cookies — one context per session_id, no bleed across campaigns."""
    assert _run(real_service, "LOGIN", "cam-A").json()["status"] == "done"
    assert _run(real_service, "AUTHED", "cam-A").json()["status"] == "done"
    assert real_service._cap["who"] == "authenticated"

    assert _run(real_service, "AUTHED", "cam-B").json()["status"] == "done"
    assert real_service._cap["who"] == "anonymous", "cookies bled across session_ids"
