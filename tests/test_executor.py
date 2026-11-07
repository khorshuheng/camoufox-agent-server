from __future__ import annotations

from typing import Any

from camoufox_agent_server import executor as executor_module
from camoufox_agent_server.executor import Executor


class TargetClosedError(Exception):
    pass


class _Events:
    def mark(self) -> int:
        return 0

    def since(self, cursor: int) -> tuple[list[Any], int, bool]:
        return [], cursor, False


class FakeTab:
    """Just enough of a Tab for the executor's bookkeeping."""

    id = "t1"
    navigation_id = 1
    events = _Events()
    pending_count = 0


async def _boom(_tab: Any, _params: dict[str, Any]) -> Any:
    raise TargetClosedError("Target page, context or browser has been closed")


async def test_a_closed_browser_becomes_a_retryable_per_command_error(monkeypatch) -> None:
    monkeypatch.setitem(executor_module.ACTIONS, "boom", _boom)
    out = await Executor().run(
        FakeTab(),  # type: ignore[arg-type]
        [{"action": "boom"}],
        observe="never",
        events_mode="none",
    )

    entry = out["results"][0]
    assert entry["ok"] is False
    assert entry["error"]["code"] == "browser_closed"
    assert entry["error"]["details"]["retryable"] is True
    assert "browser/restart" in entry["error"]["details"]["hint"]


async def test_an_ordinary_failure_stays_action_failed(monkeypatch) -> None:
    async def fail(_tab: Any, _params: dict[str, Any]) -> Any:
        raise RuntimeError("Element is not attached to the DOM")

    monkeypatch.setitem(executor_module.ACTIONS, "fail", fail)
    out = await Executor().run(
        FakeTab(),  # type: ignore[arg-type]
        [{"action": "fail"}],
        observe="never",
        events_mode="none",
    )

    assert out["results"][0]["error"]["code"] == "action_failed"
