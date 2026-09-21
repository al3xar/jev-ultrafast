"""FastAPI app: run_goal / extract_surface / get_evidence around jev_ultrafast.Agent."""

import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from jev_ultrafast import model
from jev_ultrafast import service as _service

# T-1: in-memory evidence store. Session persistence across runs arrives in T-4.
_RUNS: dict[str, dict] = {}
_LOCK = threading.Lock()

# Placeholder goal for surface extraction; the loop never runs, so it is never sent to a model.
SURFACE_GOAL = "Extract the page surface"


class RunGoalRequest(BaseModel):
    url: str
    goal: str
    session_id: str


class ExtractSurfaceRequest(BaseModel):
    url: str
    session_id: str


def _elements(state: dict) -> list:
    page = state.get("page") or {}
    return state.get("elements") or model.action_space(page.get("actions", []))[0]


def create_app() -> FastAPI:
    app = FastAPI(title="Jev Ultrafast service")

    @app.post("/run_goal")
    def run_goal(body: RunGoalRequest):
        run_id = "jev-" + uuid.uuid4().hex
        try:
            agent = _service.Agent(body.url, body.goal)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Browser unavailable: {exc}") from exc
        final_state, error = None, None
        try:
            # The run loop ends on done/blocked. A budget ValueError or an interrupted
            # dropdown (RuntimeError) mid-loop leaves the agent with the last executed
            # action recorded; the run is stored with its error, never retried.
            for state in agent.run():
                final_state = state
        except Exception as exc:
            error = str(exc)
        finally:
            agent.close()
        if final_state is None:
            final_state = agent.snapshot()
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
            "created_at": time.time(),
        }
        with _LOCK:
            _RUNS[run_id] = record
        return {
            "run_id": run_id,
            "status": record["status"],
            "session_id": body.session_id,
            "elapsed_ms": record["elapsed_ms"],
            "error": error,
        }

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
