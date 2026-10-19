#!/usr/bin/env python3
"""Warm a persistent Camoufox profile with ordinary browsing.

    make warm                       # against http://127.0.0.1:8787
    uv run python scripts/warm_profile.py http://127.0.0.1:8787
    uv run python scripts/warm_profile.py --site https://example.com

This does not log in anywhere. It visits a few high-traffic sites so a new
profile has cookies, history and a normal-looking navigation trail before an
agent uses it. It also accepts a visible cookie consent if the page clearly is
a consent screen.
"""

from __future__ import annotations

import argparse
import sys

import httpx

DEFAULT_SITES = [
    "https://www.google.com/",
    "https://en.wikipedia.org/wiki/Main_Page",
    "https://github.com/trending",
    "https://www.youtube.com/",
    "https://www.reddit.com/",
    "https://news.ycombinator.com/",
    "https://stackoverflow.com/",
    "https://www.bbc.com/news",
]

CONSENT_LABELS = {"accept all", "i agree", "accept", "reject all"}
CONSENT_HINTS = ("cookie", "consent", "before you continue")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--site",
        action="append",
        default=[],
        metavar="URL",
        help="extra site to visit (repeatable; replaces the default list if given)",
    )
    parser.add_argument("--wait-ms", type=int, default=2500)
    parser.add_argument("--timeout", type=float, default=90)
    return parser.parse_args()


def maybe_accept_consent(client: httpx.Client, page_text: str) -> str | None:
    if not any(hint in page_text.lower() for hint in CONSENT_HINTS):
        return None
    snapshot = client.get("/tabs/default/snapshot", params={"maxNodes": 400}).json()
    for node in snapshot.get("nodes", []):
        name = (node.get("name") or "").strip().lower()
        if node.get("role") == "button" and name in CONSENT_LABELS:
            result = client.post(
                "/tabs/default/act",
                json={"action": "click", "ref": node["ref"], "timeoutMs": 5000},
            ).json()
            if result.get("ok"):
                return node.get("name")
    return None


def main() -> int:
    args = parse_args()
    sites = args.site or DEFAULT_SITES
    with httpx.Client(base_url=args.base, timeout=args.timeout) as client:
        health = client.get("/health").json()
        if not health.get("ok"):
            print(f"browser is not ready: {health.get('error')}", file=sys.stderr)
            return 1
        print(f"warming profile: {health['browser']['profile']}")
        if health["browser"].get("fingerprint", {}).get("reused") is False:
            print("new identity: this run also created and stored the fingerprint")

        for url in sites:
            try:
                client.post(
                    "/tabs/default/batch",
                    json={
                        "commands": [
                            {
                                "action": "navigate",
                                "url": url,
                                "waitUntil": "domcontentloaded",
                                "timeoutMs": 60_000,
                            },
                            {"action": "wait", "ms": args.wait_ms},
                        ],
                        "observe": "never",
                        "timeoutMs": 60_000,
                    },
                ).raise_for_status()
                info = client.post(
                    "/tabs/default/act",
                    json={
                        "action": "evaluate",
                        "expression": (
                            "({title:document.title,text:document.body.innerText.slice(0,4000)})"
                        ),
                        "timeoutMs": 20_000,
                    },
                ).json()
                value = info.get("result", {}).get("value") or {}
                title = value.get("title") or "(no title)"
                accepted = maybe_accept_consent(client, value.get("text") or "")
                suffix = f" [accepted consent: {accepted}]" if accepted else ""
                print(f"  {url} -> {title}{suffix}")
            except httpx.HTTPError as error:
                print(f"  {url} -> failed: {error}", file=sys.stderr)

        print("done; the profile keeps its cookies and history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
