#!/usr/bin/env python3
"""A tiny agent loop against pi-camoufox.

    uv run python examples/agent_loop.py [http://127.0.0.1:8787]

It opens a local data: URL (no network), observes the page, then fills and
submits a form in one batch and prints the deltas.
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
DEMO = (
    "data:text/html,"
    "<title>Agent demo</title>"
    "<h1>Sign up</h1>"
    "<label>Email <input id=email></label>"
    "<button onclick=\"document.title='Submitted'\">Create account</button>"
)


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60) as client:
        health = client.get("/health")
        health.raise_for_status()
        info = health.json()
        if not info.get("ok"):
            print(f"browser is not ready: {info.get('error')}")
            return 1
        print(f"browser: {info['browser']['name']} {info['browser']['version']}")
        print(f"headed:  {not info['browser']['headless']}")
        print(f"tabs:    {[tab['id'] for tab in info['tabs']]}")

        client.post(
            "/tabs/default/act", json={"action": "navigate", "url": DEMO}
        ).raise_for_status()
        snapshot = client.get("/tabs/default/snapshot").json()
        print("\nsnapshot:")
        for node in snapshot.get("nodes", []):
            print(f"  {node['ref']:>4} {node['role']:<10} {node.get('name', '')}")

        email = next(
            (node for node in snapshot["nodes"] if node.get("role") == "textbox"),
            None,
        )
        button = next(
            (node for node in snapshot["nodes"] if node.get("role") == "button"),
            None,
        )
        if email is None or button is None:
            print("demo page did not expose the expected controls")
            return 1

        response = client.post(
            "/tabs/default/batch",
            json={
                "commands": [
                    {"action": "fill", "ref": email["ref"], "value": "agent@example.com"},
                    {"action": "click", "ref": button["ref"]},
                ],
                "observe": "auto",
            },
        )
        response.raise_for_status()
        out = response.json()
        print(f"\nbatch cursor: {out['cursor']}")
        for result in out["results"]:
            status = "ok" if result["ok"] else f"error: {result.get('error')}"
            print(f"  {result['i']} {result['action']:<10} {result['ms']:>4}ms {status}")
            if result.get("events"):
                print(f"      events: {result['events']['byType']}")
        if out.get("snapshot"):
            print(f"\nfinal title: {out['snapshot']['title']}")
        print(f"\nfull response:\n{json.dumps(out, indent=2)[:2000]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
