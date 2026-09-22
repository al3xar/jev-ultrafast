"""Shared test fixtures.

The Jev service routes every run_goal through a per-session browser registry (T-4),
whose real hooks launch a dedicated headless Chrome and spawn a browser-harness
daemon per session. Every existing service test mocks ``Agent`` and must stay fully
offline (no Chrome, no daemon, no paid APIs), so this autouse fixture no-ops the
registry's two injectable browser hooks by default.

Tests that exercise the session registry itself (tests/test_session_registry.py)
override these hooks with their own fakes, which are applied after this fixture and
therefore win. The real-browser acceptance test (test_service_session_real.py)
restores the hooks to their real implementations.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_browser_session_hooks(monkeypatch):
    """Keep the T-4 session layer inert unless a test opts into its real/fake hooks."""
    from jev_ultrafast.service import sessions

    monkeypatch.setattr(sessions, "_session_factory", lambda ctx: None, raising=False)
    monkeypatch.setattr(sessions, "_session_release", lambda ctx: None, raising=False)
