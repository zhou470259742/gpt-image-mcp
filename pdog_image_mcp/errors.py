"""Shared error type and stderr logging for the Pdog image engine."""

from __future__ import annotations

import sys
from typing import Any, Optional


class PdogError(Exception):
    """Structured failure raised by the engine.

    `kind` classifies the failure (cost_guard / invalid_parameter / http /
    network / upstream / missing_dependency / config), `http_status` carries
    the upstream HTTP status when known, and `hint` suggests a next step.

    `failover_safe` says whether retrying the same request on another route is
    safe. A connection refused or an HTTP rejection means nothing was produced,
    so a fallback costs nothing extra. A timeout after the request was sent may
    already have been billed upstream, so it must not be silently resubmitted.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "error",
        http_status: Optional[int] = None,
        hint: Optional[str] = None,
        failover_safe: bool = True,
        route: Optional[str] = None,
    ) -> None:
        self.message = message
        self.kind = kind
        self.http_status = http_status
        self.hint = hint
        self.failover_safe = failover_safe
        self.route = route
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.route:
            payload["route"] = self.route
        if self.hint:
            payload["hint"] = self.hint
        return payload


def log(message: str) -> None:
    """Engine logs always go to stderr: stdout is the MCP stdio protocol channel."""
    print(f"[pdog] {message}", file=sys.stderr, flush=True)
