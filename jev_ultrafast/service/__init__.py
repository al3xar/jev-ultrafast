"""HTTP service around the Jev agent loop (T-1). The loop in agent.py is unchanged.

T-2: re-exports the compressed subgoal contract (compress_run -> SubgoalResult) so the
service and downstream consumers (T-3 verify, T-5 scope/budget) import it from one place.
"""

from ..agent import Agent  # noqa: F401  (service.Agent stays injectable for offline tests)
from ..contract import SubgoalResult, SubgoalStatus, compress_run  # noqa: F401  (T-2 contract)
from ..scope import MAX_ACTIONS, MAX_DECISIONS, ScopeError, ScopeGuard, clamp_budget  # noqa: F401  (T-5 scope/budget)
