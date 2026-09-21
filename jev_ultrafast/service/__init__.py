"""HTTP service around the Jev agent loop (T-1). The loop in agent.py is unchanged."""

from ..agent import Agent  # noqa: F401  (service.Agent stays injectable for offline tests)
