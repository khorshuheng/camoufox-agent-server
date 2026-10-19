from __future__ import annotations

from types import SimpleNamespace

from camoufox_agent_server.executor import _for_tab
from camoufox_agent_server.session import _is_extension_url


def test_for_tab_drops_other_tabs_and_old_navigation_epochs() -> None:
    tab = SimpleNamespace(id="t1", navigation_id=3)
    events = [
        {"type": "console", "data": {"tabId": "t1", "navigationId": 3}},
        {"type": "console", "data": {"tabId": "t1", "navigationId": 2}},
        {"type": "console", "data": {"tabId": "t2", "navigationId": 3}},
        {"type": "navigation", "data": {"tabId": "t1"}},
    ]
    kept = _for_tab(events, tab)
    assert len(kept) == 2
    assert kept[0]["data"]["navigationId"] == 3
    assert kept[1]["type"] == "navigation"


def test_extension_urls_are_recognized() -> None:
    assert _is_extension_url("moz-extension://abc/content.js")
    assert _is_extension_url("chrome-extension://abc/content.js")
    assert _is_extension_url("safari-web-extension://abc/content.js")
    assert not _is_extension_url("https://example.com/app.js")
    assert not _is_extension_url("blob:https://example.com/abc")
    assert not _is_extension_url(None)
