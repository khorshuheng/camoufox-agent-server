"""Heuristic detection of bot-challenge pages.

The agent should hand these to the user instead of treating a challenge as a
selector problem. Detection is deliberately text/title based: it catches the
common challenge shells without requiring site-specific selectors.
"""

from __future__ import annotations

from typing import Any

PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "cloudflare",
        (
            "just a moment",
            "checking your browser",
            "verify you are human",
            "enable javascript and cookies to continue",
            "performance and security by cloudflare",
            "cloudflare ray id",
        ),
    ),
    (
        "captcha",
        (
            "verify you are not a bot",
            "verify you are a human",
            "are you a robot",
            "press & hold",
            "slide to verify",
            "unusual traffic",
            "complete the captcha",
            "captcha",
        ),
    ),
    (
        "access",
        (
            "access denied",
            "attention required",
            "request blocked",
            "ddos protection",
            "you have been blocked",
        ),
    ),
)


def detect_challenge(title: str | None, text: str | None) -> dict[str, Any] | None:
    """Return a challenge descriptor, or None for an ordinary page."""
    title = (title or "").strip()
    body = (text or "").strip()
    haystack = f"{title}\n{body}".lower()
    if not haystack:
        return None
    for kind, phrases in PATTERNS:
        hits = [phrase for phrase in phrases if phrase in haystack]
        if hits:
            return {
                "detected": True,
                "kind": kind,
                "evidence": hits[:5],
                "title": title[:200],
            }
    return None
