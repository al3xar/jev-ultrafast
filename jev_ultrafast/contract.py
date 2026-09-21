"""The compressed subgoal response contract (plan section 2.2): state -> planner payload.

Jev's native state has no run_id, extracted, or planner-facing status; it exposes
status in {ready, predicted, done, blocked}, history, page, and elapsed_ms. compress_run
synthesizes the run_id, maps the status to DONE | FAILED | BLOCKED | BUDGET_EXCEEDED,
derives extracted (reflected text / final URL / forms seen) from the final snapshot and
history, and writes a short natural-language summary for the big planner model.

The full trace and screenshots never travel in this payload: they stay in the evidence
store and are fetched separately via get_evidence / web_get_evidence.
"""

import hashlib
import json
import re
import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

SUMMARY_MAX_CHARS = 500
REFLECTED_TEXT_MAX_CHARS = 500


class SubgoalStatus(str, Enum):
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class Extracted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reflected_text: str | None = None
    final_url: str | None = None
    forms_seen: int = 0


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions_used: int = 0
    decisions_used: int = 0
    elapsed_ms: int = 0


class SubgoalResult(BaseModel):
    """The compressed response the planner receives (plan section 2.2).

    `trace` is deliberately NOT a field here: it lives in the evidence store and is
    requested separately (get_evidence / web_get_evidence) so the big model is never
    drowned in DOM. `verified` is pre-reserved as null; the verify block (T-3) fills it.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: SubgoalStatus
    verified: bool | None = None
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_CHARS)
    extracted: Extracted
    budget: Budget
    evidence_hash: str
    attack_tactic: str | None = None
    blocked_reason: str | None = None


def _is_budget_error(error: str | None) -> bool:
    """Jev raises ValueError on budget exhaustion (agent.py): 'Stopped at the N-action demo
    budget' (MAX_STEPS actions) or 'Reached the demo's model-call budget' (MAX_STEPS*2 decisions)."""
    if not error:
        return False
    low = error.lower()
    return "budget" in low


def _map_status(status: str, error: str | None, history: list) -> tuple[SubgoalStatus, str | None]:
    if status == "done":
        return SubgoalStatus.DONE, None
    if _is_budget_error(error):
        reason = f"budget exhausted: {error}"
        return SubgoalStatus.BUDGET_EXCEEDED, reason
    if status == "blocked":
        last = history[-1] if history else {}
        reason = "no supported operation can make progress"
        if last.get("action"):
            reason += f" (last action: {last['action']})"
        return SubgoalStatus.BLOCKED, reason
    if error:
        return SubgoalStatus.FAILED, error
    # Ready/predicted with no error: the run did not terminate (e.g. iteration stopped
    # externally). Report it as FAILED rather than DONE.
    return SubgoalStatus.FAILED, f"run ended without terminal status (status={status or 'unknown'})"


def _forms_seen(state: dict) -> int:
    snapshot = state.get("snapshot") or {}
    elements = snapshot.get("elements") or []
    if elements:
        return sum(
            1 for e in elements if e.get("kind") == "fill" or e.get("role") in {"textbox", "combobox", "searchbox"}
        )
    page = state.get("page") or {}
    return sum(1 for a in page.get("actions", []) if a.get("kind") == "fill")


def _reflected_text(snapshot: dict | None) -> str | None:
    text = (snapshot or {}).get("text")
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) > REFLECTED_TEXT_MAX_CHARS:
        return text[: REFLECTED_TEXT_MAX_CHARS - 1] + "…"
    return text


def _summary(status: SubgoalStatus, goal: str, history: list, extracted: Extracted, reason: str | None) -> str:
    steps = [f"{h.get('operation', h.get('kind', '?'))} {h.get('action', '?')}" for h in history]
    detail = f" after {len(steps)} actions: {', '.join(steps[-3:])}" if steps else ""
    url = f" (final url {extracted.final_url})" if extracted.final_url else ""
    base = f"{status.value} on '{goal}'{detail}{url}."
    if reason:
        base += f" {reason}"
    if len(base) > SUMMARY_MAX_CHARS:
        base = base[: SUMMARY_MAX_CHARS - 1] + "…"
    return base


def _evidence_hash(state: dict, status: SubgoalStatus) -> str:
    """Tamper-evident digest of what the run actually did: run identity, goal, status, and
    the ordered action history. Computed offline; the NyxStrike evidence chain (T-9) is the
    authoritative chain and links to this hash."""
    digest = {
        "run_id": state.get("run_id"),
        "session_id": state.get("session_id"),
        "goal": state.get("goal"),
        "status": status.value,
        "actions": [
            {"step": h.get("step"), "operation": h.get("operation"), "target": h.get("target"), "url": h.get("url")}
            for h in state.get("history", [])
        ],
    }
    blob = json.dumps(digest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def compress_run(state: dict) -> SubgoalResult:
    """Translate a Jev run state/record into the compressed subgoal response contract.

    `state` is the record stored by the service's /run_goal (T-1): run_id, session_id,
    url, goal, Jev status, error, elapsed_ms, history, decisions, snapshot, created_at.
    A bare Agent snapshot (status/page/history/decisions/elapsed_ms, no run_id) is also
    accepted: the run_id is synthesized in that case.
    """
    run_id = state.get("run_id") or "jev-" + uuid.uuid4().hex
    jev_status = state.get("status") or ""
    error = state.get("error")
    history = state.get("history") or []
    decisions = state.get("decisions") or []
    snapshot = state.get("snapshot") or {}
    goal = state.get("goal") or state.get("url") or "run"

    # T-5: the scope guard is authoritative — a run aborted for out-of-scope
    # navigation is BLOCKED with reason "out_of_scope" regardless of whatever
    # status/error the agent happened to carry at the abort point.
    scope_blocked_url = state.get("scope_blocked_url")
    if scope_blocked_url:
        status, reason = SubgoalStatus.BLOCKED, "out_of_scope"
    else:
        status, reason = _map_status(jev_status, error, history)
    extracted = Extracted(
        reflected_text=_reflected_text(snapshot),
        final_url=scope_blocked_url or snapshot.get("url") or (history[-1].get("url") if history else None),
        forms_seen=_forms_seen(state),
    )
    return SubgoalResult(
        run_id=run_id,
        status=status,
        summary=_summary(status, goal, history, extracted, reason),
        extracted=extracted,
        budget=Budget(
            actions_used=len(history),
            decisions_used=len(decisions),
            elapsed_ms=int(state.get("elapsed_ms") or 0),
        ),
        evidence_hash=_evidence_hash(state, status),
        blocked_reason=reason,
    )


def verify(state: dict, verify_spec: dict | None) -> bool | None:
    """Evaluate the subgoal's verify condition against the final snapshot (T-3).

    Runs INDEPENDENTLY of the run status: a run can be DONE and still fail its verify
    (the planner decides with `verified`, not with `status`). Returns True/False, or
    None when no verification is requested (kind `none` or absent spec).
    """
    if not verify_spec:
        return None
    kind = verify_spec.get("kind")
    if kind == "none":
        return None
    snapshot = state.get("snapshot") or {}
    if kind == "text_present":
        text = " ".join((snapshot.get("text") or "").split()).lower()
        return (verify_spec.get("value") or "").lower() in text
    if kind == "url_matches":
        url = snapshot.get("url") or (state.get("history") or [{}])[-1].get("url") or ""
        pattern = verify_spec.get("value") or ""
        try:
            return re.search(pattern, url) is not None
        except re.error:
            # A malformed regex is a spec error, not a satisfied condition.
            return False
    if kind == "element_state":
        elements = snapshot.get("elements") or []
        by_index = {str(e.get("index")): e for e in elements}
        element = by_index.get(str(verify_spec.get("index", "")))
        if element is None:
            return False
        for attr in ("value", "checked", "selected"):
            if attr in verify_spec:
                expected = verify_spec[attr]
                actual = element.get(attr)
                if expected is None:
                    if actual is not None:
                        return False
                elif str(actual) != str(expected):
                    return False
        return True
    raise ValueError(f"Unsupported verify kind: {kind!r}")
