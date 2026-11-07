from __future__ import annotations

from types import SimpleNamespace

from camoufox_agent_server.events import HIGHLIGHT_CHARS, MAX_HIGHLIGHTS, summarize
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


def test_highlights_are_clipped_to_a_readable_length() -> None:
    tracker = "https://www.youtube.com/api/stats/qoe?fmt=398&" + "a" * 5000
    events = [
        {
            "type": "requestfailed",
            "data": {"method": "POST", "url": tracker, "failure": "NS_ERROR_ABORT"},
            "seq": 1,
            "ts": 0.0,
        }
    ]

    digest = summarize(events)

    assert len(digest["highlights"]) == 1
    highlight = digest["highlights"][0]
    assert len(highlight) <= HIGHLIGHT_CHARS
    assert highlight.startswith("[netfail] POST https://www.youtube.com/api/stats/qoe")
    assert highlight.endswith("\u2026")


def test_highlight_count_is_bounded() -> None:
    events = [
        {"type": "navigation", "data": {"url": f"https://example.com/{i}"}, "seq": i, "ts": 0.0}
        for i in range(MAX_HIGHLIGHTS + 25)
    ]

    digest = summarize(events)

    assert len(digest["highlights"]) == MAX_HIGHLIGHTS
    assert digest["total"] == MAX_HIGHLIGHTS + 25
