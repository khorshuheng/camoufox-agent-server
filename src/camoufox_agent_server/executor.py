"""Batch executor: N commands in, N per-command deltas out.

This is the hot path of the agent loop: one HTTP round trip yields the actions
and the page changes they caused, without paying a snapshot per command.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .actions import ACTIONS, READ_ONLY, ActionError
from .errors import closed_error_hint, is_closed_error
from .events import Event, frame, summarize
from .session import Tab

CHANGE_ACTIONS = {
    "navigate",
    "click",
    "fill",
    "type",
    "select",
    "press",
    "check",
    "history",
    "reload",
    "scroll",
    "wait",
    "closeOverlays",
    "clickAt",
}
OBSERVE_MODES = {"auto", "always", "never", "full"}
EVENT_MODES = {"none", "summary", "full"}


class Executor:
    def __init__(self, *, default_settle_ms: int = 250, max_settle_ms: int = 5000) -> None:
        self.default_settle_ms = default_settle_ms
        self.max_settle_ms = max_settle_ms

    async def run(
        self,
        tab: Tab,
        commands: list[dict[str, Any]],
        *,
        observe: str = "auto",
        events_mode: str = "summary",
        settle_ms: int | None = None,
        stop_on_error: bool = False,
        timeout_ms: int = 30_000,
    ) -> dict[str, Any]:
        if observe not in OBSERVE_MODES:
            observe = "auto"
        if events_mode not in EVENT_MODES:
            events_mode = "summary"
        results: list[dict[str, Any]] = []
        cursor = tab.events.mark()

        for index, command in enumerate(commands):
            name = command.get("action") or command.get("type")
            started = time.perf_counter()
            if not name:
                results.append(
                    {
                        "i": index,
                        "action": None,
                        "ok": False,
                        "error": {"message": 'command requires "action"'},
                    }
                )
                if stop_on_error:
                    break
                continue
            function = ACTIONS.get(name)
            if function is None:
                results.append(
                    {
                        "i": index,
                        "action": name,
                        "ok": False,
                        "error": {
                            "message": f'unknown action "{name}"',
                            "known": sorted(ACTIONS),
                        },
                    }
                )
                if stop_on_error:
                    break
                continue

            params = dict(command.get("params") or {})
            for key, value in command.items():
                if key not in ("action", "type", "params"):
                    params[key] = value

            result: Any = None
            error: ActionError | None = None
            try:
                result = await asyncio.wait_for(
                    function(tab, params), timeout=max(1, timeout_ms) / 1000
                )
            except asyncio.TimeoutError:
                error = ActionError(
                    f'action "{name}" timed out after {timeout_ms}ms',
                    code="timeout",
                )
            except ActionError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - surface any browser failure to the agent
                message = str(exc) or exc.__class__.__name__
                if is_closed_error(exc):
                    # The browser died mid-batch. Name it, so the agent restarts
                    # instead of re-targeting refs against a dead page.
                    error = ActionError(message, code="browser_closed", details=closed_error_hint())
                else:
                    error = ActionError(message, code="action_failed")

            if settle_ms is None:
                budget = self.default_settle_ms
            else:
                budget = settle_ms
            if name not in READ_ONLY and budget > 0:
                await self._settle(tab, budget)

            events, cursor, _dropped = tab.events.since(cursor)
            events = _for_tab(events, tab)

            entry: dict[str, Any] = {
                "i": index,
                "action": name,
                "ok": error is None,
                "ms": round((time.perf_counter() - started) * 1000),
            }
            if error is not None:
                entry["error"] = _error_payload(error)
                if error.code in ("not_found", "not_visible"):
                    try:
                        entry["snapshot"] = await tab.snapshot(
                            {"maxNodes": 250, "includeText": True}
                        )
                        entry["hint"] = (
                            "the element reference is stale; "
                            "re-target with a ref from this snapshot"
                        )
                    except Exception:  # noqa: BLE001 - best effort
                        pass
            elif result is not None:
                entry["result"] = compact_result(result)
            compacted = compact_events(events, observe, events_mode)
            if compacted:
                entry["events"] = compacted
            results.append(entry)
            if error is not None and stop_on_error:
                break

        out: dict[str, Any] = {
            "cursor": tab.events.mark(),
            "results": results,
        }
        snapshot = await self._maybe_snapshot(tab, observe, results)
        if snapshot is not None:
            out["snapshot"] = snapshot
        return out

    async def _settle(self, tab: Tab, budget_ms: int) -> None:
        """Wait until in-flight requests drain, up to the budget.

        Exits as soon as the page is quiet; the request table is the source of
        truth, so this costs no page round trips.
        """
        deadline = time.monotonic() + min(budget_ms, self.max_settle_ms) / 1000
        saw_activity = False
        while time.monotonic() < deadline:
            if tab.pending_count > 0:
                saw_activity = True
            if saw_activity and tab.pending_count == 0:
                return
            if not saw_activity and time.monotonic() + 0.06 >= deadline:
                return
            await asyncio.sleep(0.02)

    async def _maybe_snapshot(
        self, tab: Tab, observe: str, results: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if observe == "never":
            return None
        if observe == "auto":
            changed = any(
                entry.get("ok")
                and (entry.get("events") is not None or entry.get("action") in CHANGE_ACTIONS)
                for entry in results
            )
            if not changed:
                return None
        try:
            return await tab.snapshot({"maxNodes": 250, "includeText": True})
        except Exception as error:  # noqa: BLE001 - snapshot is best effort
            return {"error": str(error)}


def _for_tab(events: list[Event], tab: Tab) -> list[Event]:
    """Keep only this tab's events from the current navigation epoch.

    A navigation can leave the previous document's trackers finishing after the
    new page has committed. Those events belong to the old epoch and would
    otherwise show up as failures caused by the navigation.
    """
    current = tab.navigation_id
    out = []
    for event in events:
        data = event["data"]
        if data.get("tabId") != tab.id:
            continue
        navigation_id = data.get("navigationId")
        if navigation_id is not None and navigation_id != current:
            continue
        out.append(event)
    return out


def _error_payload(error: ActionError) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": str(error)}
    if error.code:
        payload["code"] = error.code
    if error.details:
        payload["details"] = error.details
    return payload


def compact_events(
    events: list[Event], observe: str, events_mode: str = "summary"
) -> dict[str, Any] | None:
    if not events or events_mode == "none":
        return None
    digest = summarize(events)
    payload: dict[str, Any] = {"count": len(events), "byType": digest["byType"]}
    if digest["highlights"]:
        payload["highlights"] = digest["highlights"]
    if observe == "full" or events_mode == "full":
        payload["all"] = [frame(event) for event in events]
    return payload


def compact_result(result: Any) -> Any:
    if isinstance(result, dict) and result.get("data") and result.get("format"):
        return {
            "format": result["format"],
            "bytes": result.get("bytes"),
            "__artifactData": result["data"],
        }
    return result
