"""Per-session browser contexts (T-4): one persistent Chrome profile per session_id.

A real pentest is login -> set security level -> attack module. Each Jev run used to
spawn a fresh Chrome and destroy its cookies/CSRF on exit, so the campaign never got
past the login. This module keeps one browser context alive per session_id so
consecutive ``run_goal`` calls with ``reuse_session=true`` resume an authenticated
state.

How persistence is achieved (no change to the agent loop):
- Each reusable session owns a headless Chrome launched with its own
  ``--user-data-dir`` and a loopback ``--remote-debugging-port``. Same profile dir
  means the same cookies/storage survive between runs.
- Each session gets a *named* browser-harness daemon (``BU_NAME``) bound to that
  Chrome via ``BU_CDP_URL``. ``Browser``/``Agent`` talk CDP through the process-global
  daemon name, so before a run we bind the per-session daemon into that global.
- ``browser_harness`` routes CDP through one process-global daemon name, so at most
  ONE run can be bound to a session at a time: a global browser-routing lock plus a
  per-session lock serialize access (un contexto, sin concurrencia).

Releasing a reusable session keeps the context alive (TTL expires idle ones). A
``reuse_session=false`` run uses a throwaway context torn down immediately.
"""

import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL_S = 30 * 60
DEFAULT_SWEEPER_INTERVAL_S = 60
DEFAULT_BASE_DIR = "/tmp/jev-sessions"

# Session ids become directory names, daemon names (BU_NAME) and part of process
# commands; keep to a safe charset so a caller cannot inject path separators or
# break daemon naming.
_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _free_port() -> int:
    """A free loopback port for a session Chrome's remote-debugging endpoint."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class SessionContext:
    session_id: str
    name: str
    user_data_dir: str
    port: int
    reusable: bool
    last_used: float
    proc: subprocess.Popen | None = None


def _launch_chrome(user_data_dir: str, port: int) -> subprocess.Popen:
    """Launch a dedicated headless Chrome for one session."""
    args = [
        "google-chrome",
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--noerrdialogs",
        "--no-first-run",
        "--ozone-platform=headless",
        "--ozone-override-screen-size=1120,780",
        "about:blank",
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_chrome(port: int, timeout: float = 30.0) -> None:
    """Wait until the session Chrome answers CDP on its loopback port."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
                r.read()
            return
        except Exception as exc:  # noqa: BLE001 - any I/O error means "not up yet"
            last_err = exc
            time.sleep(0.2)
    raise RuntimeError(f"Session Chrome CDP endpoint not reachable on 127.0.0.1:{port}: {last_err}")


def _bind_daemon(name: str, port: int) -> None:
    """Spawn (or reuse) the named browser-harness daemon bound to this session's
    Chrome. Does NOT set the process-global routing — ``run`` does that and restores
    it, so this stays safe even when the routing is concurrently managed."""
    from browser_harness import admin

    admin.ensure_daemon(name=name, env={"BU_CDP_URL": f"http://127.0.0.1:{port}"})


def _stop_daemon(name: str) -> None:
    """Stop the named daemon (best-effort); leave the Chrome for profile cleanup."""
    from browser_harness import admin

    try:
        admin.restart_daemon(name)
    except Exception:  # noqa: BLE001 - daemon may already be gone
        pass


def _routing_name() -> str:
    """The current process-global routing daemon name (to restore later)."""
    from browser_harness import admin

    return admin.NAME


def _set_routing(name: str) -> None:
    """Point the process-global browser_harness routing at ``name`` so
    ``Browser``/``Agent`` talk to this session's daemon."""
    from browser_harness import admin, helpers

    admin.NAME = name
    helpers.NAME = name


def _restore_routing(name: str) -> None:
    """Reset the routing to the default daemon name."""
    from browser_harness import admin, helpers

    admin.NAME = name
    helpers.NAME = name


def _stop_chrome(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# --- Browser hooks -----------------------------------------------------------
# The two hooks below are the only points that touch real processes (Chrome + the
# browser-harness daemon). ``tests/conftest.py`` no-ops them by default so that
# service tests which mock ``Agent`` never launch a real Chrome; the real-browser
# acceptance test restores them to ``_real_*``. ``run``/``session``/``release``/
# ``sweep_expired`` resolve these names at call time, so a test-level override wins.

def _real_session_factory(ctx: SessionContext) -> None:
    """Create the browser for a context: launch a dedicated headless Chrome on its
    user-data-dir, wait for its CDP endpoint, and spawn the named daemon bound to it.
    Stores the Chrome Popen on ``ctx.proc``. (Routing is set separately by ``run``.)"""
    ctx.proc = _launch_chrome(ctx.user_data_dir, ctx.port)
    _wait_chrome(ctx.port)
    _bind_daemon(ctx.name, ctx.port)


def _real_session_release(ctx: SessionContext) -> None:
    """Tear a context down: stop its daemon and Chrome, remove the profile dir.
    Used for disposable (reuse_session=false) contexts right after a run, and for
    any context when it expires via TTL or at process shutdown."""
    _stop_daemon(ctx.name)
    _stop_chrome(ctx.proc)
    shutil.rmtree(ctx.user_data_dir, ignore_errors=True)


# The injectable hooks default to the real implementations.
_session_factory = _real_session_factory
_session_release = _real_session_release


# Global serialization of the browser_harness CDP routing (one daemon name is
# process-global, so only one run may be bound to a session at a time).
SESSION_BROWSER_LOCK = threading.Lock()


class SessionRegistry:
    """Owns browser contexts per session_id, with a TTL and per-session locking."""

    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        sweeper_interval_s: float = DEFAULT_SWEEPER_INTERVAL_S,
        base_dir: str | None = None,
    ):
        self._ttl = float(ttl_s)
        self._interval = float(sweeper_interval_s)
        self._base = Path(base_dir or (DEFAULT_BASE_DIR + f"-{os.getpid()}"))
        self._lock = threading.Lock()
        self._contexts: dict[str, SessionContext] = {}
        self._seq = 0
        self._sweeper: threading.Thread | None = None

    # ---- lifecycle -----------------------------------------------------------

    def session(self, session_id: str, reuse: bool = True) -> SessionContext:
        """Acquire (creating if needed) the browser context for a session_id."""
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError(f"Invalid session_id {session_id!r}: must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,63}}")
        now = time.time()
        with self._lock:
            self._expire_locked(now)
            ctx = self._contexts.get(session_id)
            if ctx is not None and now - ctx.last_used <= self._ttl:
                ctx.last_used = now
                return ctx
            if ctx is not None:
                # Idle past the TTL: tear down before recreating.
                _session_release(ctx)
                del self._contexts[session_id]
            ctx = self._create(session_id, reuse=reuse, now=now)
            self._contexts[session_id] = ctx
            return ctx

    def _create(self, session_id: str, reuse: bool, now: float) -> SessionContext:
        self._seq += 1
        name = f"jev-sess-{session_id}-{self._seq}"
        base = self._base / session_id
        base.mkdir(parents=True, exist_ok=True)
        if not reuse:
            # A clean context: a fresh profile dir every time (same session_id, new state).
            user_data_dir = str(base / f"run-{self._seq}")
        else:
            # A persistent context: the SAME profile dir across runs so state carries over.
            user_data_dir = str(base / "profile")
        ctx = SessionContext(
            session_id=session_id,
            name=name,
            user_data_dir=user_data_dir,
            port=_free_port(),
            reusable=reuse,
            last_used=now,
        )
        _session_factory(ctx)
        return ctx

    def release(self, ctx: SessionContext) -> None:
        """Finish a run. A reusable context stays alive (TTL expires idle ones); a
        disposable one (reuse_session=false) is torn down exactly once, here."""
        if not ctx.reusable:
            _session_release(ctx)
            with self._lock:
                self._contexts.pop(ctx.session_id, None)
        else:
            with self._lock:
                ctx.last_used = time.time()

    def run(self, session_id: str, reuse: bool, fn, *args, **kwargs):
        """Serialize one run against a session and bind its browser routing.

        ``fn(*args, **kwargs)`` is the agent run (e.g. a lambda that constructs and
        drains an ``Agent``). The global browser-routing lock guarantees the CDP
        daemon name points at the right session for the whole run and that no two run
        loops interleave (``browser_harness`` has one process-global daemon name).
        """
        ctx = self.session(session_id, reuse=reuse)
        prev = _routing_name()
        try:
            with SESSION_BROWSER_LOCK:
                with self._lock:
                    self._contexts[ctx.session_id] = ctx
                    ctx.last_used = time.time()
                # Point the process-global routing at this session's daemon for the run.
                _set_routing(ctx.name)
                return fn(*args, **kwargs)
        finally:
            # Restore the routing so other endpoints (extract_surface, the shared
            # recon browser) are not left pointed at this session's daemon.
            _restore_routing(prev)
            self.release(ctx)

    def active_sessions(self) -> list[str]:
        now = time.time()
        with self._lock:
            return [s for s, c in self._contexts.items() if now - c.last_used <= self._ttl]

    def sweep_expired(self) -> list[str]:
        """Expire and tear down idle contexts. Returns the expired session ids."""
        now = time.time()
        expired = []
        with self._lock:
            for session_id in list(self._contexts):
                ctx = self._contexts[session_id]
                if now - ctx.last_used > self._ttl:
                    _session_release(ctx)
                    del self._contexts[session_id]
                    expired.append(session_id)
        return expired

    def _expire_locked(self, now: float) -> None:
        for session_id in list(self._contexts):
            ctx = self._contexts[session_id]
            if now - ctx.last_used > self._ttl:
                _session_release(ctx)
                del self._contexts[session_id]

    # ---- background sweeper -------------------------------------------------

    def start_sweeper(self) -> None:
        if self._sweeper is not None:
            return
        self._sweeper = threading.Thread(target=self._sweep_loop, name="jev-session-sweeper", daemon=True)
        self._sweeper.start()

    def _sweep_loop(self) -> None:
        while True:
            time.sleep(self._interval)
            try:
                self.sweep_expired()
            except Exception:  # noqa: BLE001 - keep the sweeper alive
                pass

    def shutdown(self) -> None:
        with self._lock:
            for session_id in list(self._contexts):
                _session_release(self._contexts.pop(session_id))


# The service-level registry is created in app.create_app(); tests build their own.
registry: SessionRegistry | None = None
