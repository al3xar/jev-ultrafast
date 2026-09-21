"""Scope enforcement and budget clamps for Jev runs (T-5).

Ethical/legal guard of the range: Jev decides navigation in natural language,
so nothing in the loop itself stops a CLICK on an external link from taking
the agent out of audit scope. Every run must carry a non-empty allowlist of
hosts (``scope_allowlist``); any navigation to a host outside it aborts the
run with ``status=BLOCKED, blocked_reason="out_of_scope"``.

Hosts are matched case-insensitively; a listed host covers the domain itself
and every subdomain of it (``dvwa.range.local`` admits
``admin.dvwa.range.local``). Lookalike domains are NOT admitted
(``dvwa.range.local.evil.net``, ``evil-dvwa.range.local``). Non-HTTP(s)
schemes (``javascript:``, ``data:``, ``file:``) are always out of scope.

Budget clamps respect Jev's hard limits: ``max_actions <= 60`` and
``max_decisions <= 120``. Exhaustion is reported as ``BUDGET_EXCEEDED`` —
a status distinct from ``BLOCKED`` in the subgoal contract (T-2).
"""

from urllib.parse import urlsplit

MAX_ACTIONS = 60
MAX_DECISIONS = 120

_SCHEMES = {"http", "https"}


def _host(entry: str) -> str:
    """Normalize an allowlist entry or a URL to a bare lowercase hostname."""
    text = (entry or "").strip().lower()
    if not text:
        raise ValueError("Allowlist entries must be non-empty host names or URLs")
    # Entries may be bare hostnames ("dvwa.range.local") or URLs
    # ("https://app.range.local/x"). Only URLs are parsed.
    if "://" in text:
        parts = urlsplit(text)
        host = parts.hostname or ""
        if not host:
            raise ValueError(f"Cannot derive a host from allowlist entry: {entry!r}")
        return host.rstrip(".")
    return text.rstrip(".")


class ScopeError(ValueError):
    """A navigation target left the scope allowlist."""

    def __init__(self, url: str, host: str):
        super().__init__(f"Navigation to {url!r} is out of scope (host {host!r} not in scope_allowlist)")
        self.url = url
        self.host = host


class ScopeGuard:
    """Allowlist guard: hosts in scope cover their domain and all subdomains."""

    def __init__(self, allowlist: list[str] | None):
        if not allowlist:
            raise ValueError("scope_allowlist is required and must not be empty")
        self._hosts: frozenset[str] = frozenset(_host(e) for e in allowlist)
        if not self._hosts:
            raise ValueError("scope_allowlist is required and must not be empty")

    def _host_of_url(self, url: str) -> str | None:
        text = (url or "").strip()
        if not text or "://" not in text:
            return None
        scheme = urlsplit(text).scheme.lower()
        if scheme not in _SCHEMES:
            return None
        return (urlsplit(text).hostname or "").lower().rstrip(".")

    def allowed(self, url: str) -> bool:
        host = self._host_of_url(url)
        if host is None:
            return False
        return any(host == h or host.endswith("." + h) for h in self._hosts)

    def is_out_of_scope(self, url: str) -> str | None:
        """The offending host if the URL is out of scope, else None."""
        host = self._host_of_url(url)
        if host is None:
            return (url or "").strip() or "unparseable url"
        return None if self.allowed(url) else host

    def ensure_in_scope(self, url: str) -> None:
        offending = self.is_out_of_scope(url)
        if offending is not None:
            raise ScopeError((url or "").strip(), offending)


def clamp_budget(max_actions: int | None, max_decisions: int | None) -> tuple[int, int]:
    """Clamp the requested run budget to Jev's hard limits (60 actions / 120 decisions)."""
    actions = MAX_ACTIONS if max_actions is None else int(max_actions)
    decisions = MAX_DECISIONS if max_decisions is None else int(max_decisions)
    if actions <= 0 or decisions <= 0:
        raise ValueError(f"Budgets must be positive (got max_actions={actions}, max_decisions={decisions})")
    return min(actions, MAX_ACTIONS), min(decisions, MAX_DECISIONS)
