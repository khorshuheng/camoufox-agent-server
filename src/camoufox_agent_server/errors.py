"""Errors shared by the browser manager, the executor and the HTTP layer.

Keeping the "is the browser gone?" test in one place means every surface can
agree on it: the route handlers map it to HTTP 503 + ``code: "browser_closed"``,
the executor maps it to a per-command error, and the manager uses it to decide
whether a relaunch is worth attempting.
"""

from __future__ import annotations

# Playwright raises different types and messages depending on where the closed
# browser is discovered; match on both the class name and the message.
_CLOSED_TYPES = {
    "TargetClosedError",
    "BrowserClosedError",
    "BrowserClosed",
    "ConnectionClosedError",
}
_CLOSED_HINTS = (
    "has been closed",
    "target closed",
    "target page, context or browser",
    "browser has been closed",
    "browser closed",
    "connection closed",
    "browser is not available",
)

BROWSER_CLOSED_HINT = "POST /browser/restart to relaunch the browser, then retry"


class BrowserUnavailable(RuntimeError):
    """The browser is gone and could not be relaunched."""


def is_closed_error(error: BaseException) -> bool:
    """True when the error means the browser, context or page is gone."""
    if isinstance(error, BrowserUnavailable):
        return True
    if type(error).__name__ in _CLOSED_TYPES:
        return True
    message = str(error).lower()
    return any(hint in message for hint in _CLOSED_HINTS)


def closed_error_hint() -> dict[str, object]:
    """Extra fields for a ``browser_closed`` error payload."""
    return {"retryable": True, "hint": BROWSER_CLOSED_HINT}
