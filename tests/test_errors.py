from __future__ import annotations

import pytest

from camoufox_agent_server.errors import BrowserUnavailable, is_closed_error


class TargetClosedError(Exception):
    pass


@pytest.mark.parametrize(
    "error",
    [
        TargetClosedError("whatever"),
        RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed"),
        RuntimeError("Target page, context or browser has been closed"),
        RuntimeError("Browser has been closed"),
        BrowserUnavailable("browser is not available"),
    ],
)
def test_closed_browser_errors_are_recognized(error: BaseException) -> None:
    assert is_closed_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Timeout 30000ms exceeded"),
        RuntimeError("Element is not attached to the DOM"),
        RuntimeError("net::ERR_NAME_NOT_RESOLVED"),
    ],
)
def test_ordinary_page_errors_are_not_mistaken_for_a_dead_browser(error: BaseException) -> None:
    assert is_closed_error(error) is False
