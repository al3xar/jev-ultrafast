"""HTTP service around the Jev agent loop (T-1). The loop in agent.py is unchanged.

T-2: re-exports the compressed subgoal contract (compress_run -> SubgoalResult) so the
service and downstream consumers (T-3 verify, T-5 scope/budget) import it from one place.

T-4: re-exports the per-session browser-context registry (sessions.SessionRegistry) so
/run_goal with reuse_session=true keeps one Chrome profile (cookies/CSRF) alive per
session_id across consecutive runs.
"""

from ..agent import Agent  # noqa: F401  (service.Agent stays injectable for offline tests)
from ..contract import SubgoalResult, SubgoalStatus, compress_run  # noqa: F401  (T-2 contract)
from . import sessions  # noqa: F401  (T-4 per-session browser contexts)
