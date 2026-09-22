"""FastAPI app: run_goal / extract_surface / get_evidence around jev_ultrafast.Agent."""

import contextlib
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from jev_ultrafast import model
from jev_ultrafast import service as _service
from jev_ultrafast.contract import compress_run
from jev_ultrafast.scope import ScopeGuard, clamp_budget

# T-1: in-memory evidence store. Session persistence across runs arrives in T-4.
_RUNS: dict[str, dict] = {}
_LOCK = threading.Lock()

# Placeholder goal for surface extraction; the loop never runs, so it is never sent to a model.
SURFACE_GOAL = "Extract the page surface"


class RunGoalRequest(BaseModel):
    url: str
    goal: str
    session_id: str
    # T-5: scope is mandatory for a run in the range — a run without an
    # allowlist is refused, not defaulted, so a missing field can never mean
    # "navigate anywhere".
    scope_allowlist: list[str] = Field(min_length=1)
    max_actions: int | None = None
    max_decisions: int | None = None
    # T-4: keep this session's browser (cookies/CSRF/profile) alive across runs.
    # When true the run reuses the session's existing Chrome profile; when false it
    # uses a throwaway context that is torn down after the run.
    reuse_session: bool = True
    # T-4b: capture a JPEG per observed step inside the loop and store it in the
    # evidence record. Off by default — structured state only, no vision in the loop.
    screenshots: bool = False


class ExtractSurfaceRequest(BaseModel):
    url: str
    session_id: str


def _elements(state: dict) -> list:
    page = state.get("page") or {}
    return state.get("elements") or model.action_space(page.get("actions", []))[0]


def create_app() -> FastAPI:
    # T-4: per-session browser contexts. The registry is created eagerly so it exists
    # for every request (the lifespan only manages the background sweeper and the
    # end-of-life teardown).
    session_registry = _service.sessions.SessionRegistry()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.session_registry = session_registry
        session_registry.start_sweeper()
        try:
            yield
        finally:
            session_registry.shutdown()

    app = FastAPI(title="Jev Ultrafast service", lifespan=lifespan)
    app.state.session_registry = session_registry

    @app.post("/run_goal")
    def run_goal(body: RunGoalRequest):
        # T-5: scope + budget are enforced before the run starts. An empty or
        # missing scope_allowlist is a 422 at the request layer (above); an
        # unclamped budget is clamped to Jev's hard limits (60/120).
        guard = ScopeGuard(body.scope_allowlist)
        max_actions, max_decisions = clamp_budget(body.max_actions, body.max_decisions)
        run_id = "jev-" + uuid.uuid4().hex
        if (offending := guard.is_out_of_scope(body.url)) is not None:
            # The requested URL itself is out of scope: the run is refused before
            # a browser even opens, reported as BLOCKED / out_of_scope.
            record = {
                "run_id": run_id,
                "session_id": body.session_id,
                "url": body.url,
                "goal": body.goal,
                "status": "blocked",
                "error": None,
                "elapsed_ms": 0,
                "history": [],
                "snapshot": {"url": body.url, "title": None, "text": None, "elements": []},
                "scope_blocked_url": body.url,
                "scope_offending": offending,
                "max_actions": max_actions,
                "max_decisions": max_decisions,
                "created_at": time.time(),
            }
            record["screenshots"] = []
            record["evidence_hash"] = compress_run(record).evidence_hash
            with _LOCK:
                _RUNS[run_id] = record
            return {
                "run_id": run_id,
                "status": "blocked",
                "blocked_reason": "out_of_scope",
                "blocked_url": body.url,
                "session_id": body.session_id,
                "elapsed_ms": 0,
                "max_actions": max_actions,
                "max_decisions": max_decisions,
            }
        registry = app.state.session_registry
        final_state: dict | None = None
        error = None
        scope_blocked_url = None
        screenshots: list = []

        def _execute():
            """Open the agent against this session's browser and run the loop.

            Runs inside ``registry.run`` so the per-session Chrome (same profile
            across runs) is bound to the CDP routing for the whole run and access
            to the session is serialized.
            """
            nonlocal final_state, error, scope_blocked_url
            try:
                # T-4b: per-step JPEG capture only when the run requested it.
                agent = _service.Agent(body.url, body.goal, screenshots=body.screenshots)
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"Browser unavailable: {exc}") from exc
            try:
                # The run loop ends on done/blocked. A budget ValueError or an
                # interrupted dropdown (RuntimeError) mid-loop leaves the agent with
                # the last executed action recorded; the run is stored with its error,
                # never retried.
                # T-5: every observed page URL is checked against the allowlist — a
                # CLICK that navigates out of scope aborts the run at that point.
                for state in agent.run():
                    final_state = state
                    if body.screenshots:
                        # T-4b: one base64 JPEG per observed step, ordered. The agent
                        # only attaches page["screenshot"] when it was built with
                        # screenshots=True, so this stays empty otherwise.
                        shot = (state.get("page") or {}).get("screenshot")
                        if shot:
                            screenshots.append(shot)
                    page_url = (state.get("page") or {}).get("url") or state.get("url") or ""
                    if guard.is_out_of_scope(page_url) is not None:
                        scope_blocked_url = page_url
                        break
            except Exception as exc:
                error = str(exc)
            finally:
                agent.close()
            if final_state is None:
                final_state = agent.snapshot()

        registry.run(body.session_id, reuse=body.reuse_session, fn=_execute)
        page = final_state.get("page") or {}
        record = {
            "run_id": run_id,
            "session_id": body.session_id,
            "url": body.url,
            "goal": body.goal,
            "status": final_state.get("status", "ready"),
            "error": error,
            "elapsed_ms": final_state.get("elapsed_ms", 0),
            "history": final_state.get("history", []),
            "snapshot": {
                "url": page.get("url"),
                "title": page.get("title"),
                "text": page.get("text"),
                "elements": _elements(final_state),
            },
            "max_actions": max_actions,
            "max_decisions": max_decisions,
            "created_at": time.time(),
            # T-4b: per-step JPEG captures (empty unless the run requested them).
            "screenshots": screenshots,
        }
        # T-4b: the T-2 tamper-evident digest over the stored record, coherent with
        # compress_run recomputed on the same record (screenshots are not part of the
        # digest — the hash covers run identity, goal, status, and the action history).
        record["evidence_hash"] = compress_run(record).evidence_hash
        if scope_blocked_url:
            record["scope_blocked_url"] = scope_blocked_url
            record["scope_offending"] = guard.is_out_of_scope(scope_blocked_url)
            record["status"] = "blocked"
        with _LOCK:
            _RUNS[run_id] = record
        response = {
            "run_id": run_id,
            "status": record["status"],
            "session_id": body.session_id,
            "elapsed_ms": record["elapsed_ms"],
            "error": error,
            "max_actions": max_actions,
            "max_decisions": max_decisions,
        }
        if scope_blocked_url:
            response["blocked_reason"] = "out_of_scope"
            response["blocked_url"] = scope_blocked_url
        return response

    @app.post("/extract_surface")
    def extract_surface(body: ExtractSurfaceRequest):
        try:
            agent = _service.Agent(body.url, SURFACE_GOAL)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Browser unavailable: {exc}") from exc
        try:
            # One observation + the indexed element table. The agent loop is never started.
            state = agent.snapshot()
        finally:
            agent.close()
        page = state.get("page") or {}
        return {
            "run_id": None,
            "session_id": body.session_id,
            "status": state.get("status", "ready"),
            "url": page.get("url"),
            "title": page.get("title"),
            "text": page.get("text"),
            "elements": _elements(state),
        }

    @app.get("/get_evidence/{run_id}")
    def get_evidence(run_id: str):
        with _LOCK:
            record = _RUNS.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown run_id")
        return record

    return app
