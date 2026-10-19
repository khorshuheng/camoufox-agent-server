"""Action vocabulary implemented with Playwright locators.

Actions accept either a ``ref`` from a snapshot or a CSS ``selector``. Playwright
dispatches input through Juggler, so Camoufox sees native input events rather
than synthetic DOM calls.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .observe import REF_ATTR
from .session import DialogError, NoDialogError, Tab

DEFAULT_TIMEOUT = 15_000
MAX_WAIT_MS = 60_000


class ActionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "action_failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


ActionFn = Callable[[Tab, dict[str, Any]], Awaitable[Any]]


# ---------------------------------------------------------------------------
# Selectors and error translation
# ---------------------------------------------------------------------------


def selector_for(params: dict[str, Any]) -> str:
    ref = params.get("ref")
    selector = params.get("selector")
    if ref:
        return f'[{REF_ATTR}="{ref}"]'
    if selector:
        return str(selector)
    raise ActionError(
        'action requires either "ref" (from a snapshot) or "selector" (CSS)',
        code="bad_request",
    )


def locator_for(tab: Tab, params: dict[str, Any]) -> tuple[Locator, str]:
    selector = selector_for(params)
    return tab.page.locator(selector).first, selector


def _translate_locator_error(error: Exception, selector: str) -> ActionError:
    message = str(error)
    lowered = message.lower()
    if "strict mode violation" in lowered:
        return ActionError(
            f"selector {selector} matches more than one element",
            code="ambiguous",
            details={
                "selector": selector,
                "hint": "use a snapshot ref or a more specific selector",
            },
        )
    if "not visible" in lowered or "element is not visible" in lowered:
        return ActionError(
            f"element {selector} is present but not visible",
            code="not_visible",
            details={"selector": selector},
        )
    if "intercepts pointer events" in lowered:
        return ActionError(
            f"element {selector} is covered by another element",
            code="blocked",
            details={"selector": selector, "hint": "close the overlay or wait for it to disappear"},
        )
    if "not an <input" in lowered or "file input" in lowered or "input element" in lowered:
        return ActionError(
            f"{selector} is not an <input type=file>",
            code="wrong_type",
            details={"selector": selector, "hint": "for dropzones use dropFiles"},
        )
    if isinstance(error, PlaywrightTimeoutError) or "timeout" in lowered:
        return ActionError(
            f"element {selector} not found or actionable within the timeout",
            code="not_found",
            details={"selector": selector},
        )
    return ActionError(
        f"action failed for {selector}: {message}",
        code="action_failed",
        details={"selector": selector},
    )


async def _locator_action(coro: Awaitable[Any], selector: str) -> Any:
    try:
        return await coro
    except ActionError:
        raise
    except PlaywrightError as error:
        raise _translate_locator_error(error, selector) from error


def _timeout(params: dict[str, Any], default: int = DEFAULT_TIMEOUT) -> int:
    value = params.get("timeoutMs", default)
    try:
        return max(1, min(int(value), 180_000))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def navigate(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    url = params.get("url")
    if not url:
        raise ActionError('navigate requires "url"', code="bad_request")
    wait_until = str(params.get("waitUntil") or "load")
    if wait_until == "none":
        wait_until = "commit"
    if wait_until not in ("commit", "domcontentloaded", "load", "networkidle"):
        raise ActionError(
            f'unsupported waitUntil "{wait_until}" (commit|domcontentloaded|load|networkidle|none)',
            code="bad_request",
        )
    timeout = _timeout(params, 30_000)
    try:
        response = await tab.page.goto(str(url), wait_until=wait_until, timeout=timeout)
    except PlaywrightTimeoutError as error:
        raise ActionError(
            f"navigation to {url} timed out after {timeout}ms",
            code="timeout",
            details={"url": str(url), "waitUntil": wait_until},
        ) from error
    except PlaywrightError as error:
        raise ActionError(
            f"navigation to {url} failed: {error}",
            code="navigation_failed",
            details={"url": str(url)},
        ) from error
    return {"url": tab.url, "status": response.status if response else None}


async def click(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    button = str(params.get("button") or "left")
    if button not in ("left", "right", "middle"):
        raise ActionError(f'unsupported button "{button}"', code="bad_request")
    timeout = _timeout(params)
    box = None
    try:
        box = await locator.bounding_box(timeout=timeout)
    except PlaywrightError:
        pass
    await _locator_action(
        locator.click(
            button=button, click_count=int(params.get("clickCount") or 1), timeout=timeout
        ),
        selector,
    )
    if params.get("waitForNav"):
        try:
            await tab.page.wait_for_load_state("load", timeout=timeout)
        except PlaywrightTimeoutError:
            pass
    at = None
    if box:
        at = {"x": round(box["x"] + box["width"] / 2), "y": round(box["y"] + box["height"] / 2)}
    return {"clicked": True, "at": at}


async def fill(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    text = "" if params.get("value") is None else str(params.get("value"))
    delay = max(0, int(params.get("delay") or 0))
    clear = params.get("clear", True)
    timeout = _timeout(params)
    await _locator_action(locator.click(timeout=timeout), selector)
    try:
        if clear and delay == 0:
            await locator.fill(text, timeout=timeout)
        else:
            if clear:
                await locator.fill("", timeout=timeout)
            await locator.press_sequentially(text, delay=delay)
        if params.get("pressEnter"):
            await locator.press("Enter", timeout=timeout)
        try:
            value = await locator.input_value(timeout=2_000)
        except PlaywrightError:
            # contenteditable has no value property
            value = await tab.evaluate_fn(
                """(args) => {
                  const el = document.querySelector(args.selector);
                  return el ? ('value' in el ? el.value : el.textContent) : null;
                }""",
                {"selector": selector},
            )
    except PlaywrightError as error:
        raise _translate_locator_error(error, selector) from error
    return {"value": value}


async def type_text(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    text = "" if params.get("text") is None else str(params.get("text"))
    delay = max(0, int(params.get("delay") or 0))
    timeout = _timeout(params)
    await _locator_action(locator.click(timeout=timeout), selector)
    await _locator_action(locator.press_sequentially(text, delay=delay), selector)
    return {"typed": True}


async def press(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    key = params.get("key")
    if not key:
        raise ActionError('press requires "key" (e.g. Enter, Tab, Control+a)', code="bad_request")
    try:
        await tab.page.keyboard.press(str(key))
    except PlaywrightError as error:
        raise ActionError(f"could not press {key}: {error}", code="bad_request") from error
    return {"key": str(key)}


async def select(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    timeout = _timeout(params)
    kwargs: dict[str, Any] = {"timeout": timeout}
    if params.get("value") is not None:
        kwargs["value"] = str(params["value"])
    elif params.get("label") is not None:
        kwargs["label"] = str(params["label"])
    elif params.get("index") is not None:
        kwargs["index"] = int(params["index"])
    else:
        raise ActionError('select requires "value", "label" or "index"', code="bad_request")
    try:
        await locator.select_option(**kwargs)
        value = await locator.input_value(timeout=2_000)
    except PlaywrightError as error:
        raise _translate_locator_error(error, selector) from error
    return {"value": value}


async def hover(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    await _locator_action(locator.hover(timeout=_timeout(params)), selector)
    return {"hovered": True}


async def scroll(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    if params.get("ref") or params.get("selector"):
        locator, selector = locator_for(tab, params)
        await _locator_action(
            locator.scroll_into_view_if_needed(timeout=_timeout(params)), selector
        )
    else:
        try:
            await tab.page.mouse.wheel(float(params.get("dx") or 0), float(params.get("dy") or 0))
        except PlaywrightError as error:
            raise ActionError(f"scroll failed: {error}", code="scroll_failed") from error
    return await tab.scroll_position()


async def check(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    target = bool(params.get("checked", True))
    timeout = _timeout(params)
    try:
        before = await locator.is_checked()
        if before != target:
            if target:
                await locator.check(timeout=timeout)
            else:
                await locator.uncheck(timeout=timeout)
        after = await locator.is_checked()
    except PlaywrightError as error:
        raise _translate_locator_error(error, selector) from error
    return {"checked": after, "changed": before != target, "requested": target}


async def set_files(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    files = params.get("files")
    file_list = (
        [str(f) for f in files] if isinstance(files, list) else [str(files)] if files else []
    )
    if not file_list:
        raise ActionError('setFiles requires a non-empty "files" array', code="bad_request")
    missing = await asyncio.to_thread(_missing_files, file_list)
    if missing:
        raise ActionError(
            f"file(s) not found: {', '.join(missing)}",
            code="bad_request",
            details={"files": missing},
        )
    try:
        await locator.set_input_files(file_list, timeout=_timeout(params))
    except PlaywrightError as error:
        raise _translate_locator_error(error, selector) from error
    return {"files": file_list}


def _missing_files(files: list[str]) -> list[str]:
    return [name for name in files if not Path(name).expanduser().is_file()]


def _file_payloads(files: list[str]) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for name in files:
        path = Path(name).expanduser()
        if not path.is_file():
            raise ActionError(f'file not found: "{name}"', code="bad_request")
        mime, _ = mimetypes.guess_type(path.name)
        payload.append(
            {
                "name": path.name,
                "type": mime or "application/octet-stream",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
    return payload


DROP_SEQUENCE_JS = r"""
async (args) => {
  const el = document.querySelector(args.selector);
  if (!el) return { ok: false, reason: 'not-found' };
  const transfer = new DataTransfer();
  for (const file of args.files) {
    const binary = atob(file.data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const type = file.type || 'application/octet-stream';
    transfer.items.add(new File([bytes], file.name, { type }));
  }
  for (const type of ['dragenter', 'dragover', 'drop']) {
    el.dispatchEvent(new DragEvent(type, {
      bubbles: true,
      cancelable: true,
      dataTransfer: transfer,
    }));
    await new Promise((resolve) => setTimeout(resolve, 40));
  }
  return { ok: true, files: Array.from(transfer.files).map((file) => file.name) };
}
"""


async def drop_files(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    """Attach files to a dropzone as a real drag sequence.

    Firefox has no CDP ``Input.dispatchDragEvent``, so the whole sequence runs
    in the page's main world: build a DataTransfer with real File objects, then
    dispatch dragenter/dragover/drop on the target.
    """
    locator, selector = locator_for(tab, params)
    files = params.get("files")
    file_list = (
        [str(f) for f in files] if isinstance(files, list) else [str(files)] if files else []
    )
    if not file_list:
        raise ActionError('dropFiles requires a non-empty "files" array', code="bad_request")
    if not tab.main_world_eval:
        raise ActionError(
            "dropFiles needs main-world evaluation; restart without --isolated-eval",
            code="main_world_required",
        )
    await _locator_action(locator.wait_for(state="visible", timeout=_timeout(params)), selector)
    await _locator_action(locator.scroll_into_view_if_needed(timeout=_timeout(params)), selector)
    payload = await asyncio.to_thread(_file_payloads, file_list)
    result = await tab.evaluate_fn(
        DROP_SEQUENCE_JS,
        {"selector": selector, "files": payload},
        timeout_ms=_timeout(params),
    )
    if not result or not result.get("ok"):
        raise ActionError(
            f"dropFiles could not find {selector} in the main world",
            code="drop_failed",
            details={"selector": selector},
        )
    return {"files": file_list, "target": selector}


async def screenshot(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    image_format = str(params.get("format") or "png").lower()
    if image_format == "jpg":
        image_format = "jpeg"
    if image_format not in ("png", "jpeg"):
        raise ActionError('screenshot format must be "png" or "jpeg"', code="bad_request")
    quality = params.get("quality")
    timeout = _timeout(params, 30_000)
    if params.get("ref") or params.get("selector"):
        locator, selector = locator_for(tab, params)
        try:
            data = await locator.screenshot(type=image_format, timeout=timeout)
        except PlaywrightError as error:
            raise _translate_locator_error(error, selector) from error
    else:
        clip = params.get("clip")
        options: dict[str, Any] = {
            "type": image_format,
            "full_page": bool(params.get("fullPage")),
            "timeout": timeout,
        }
        if quality is not None and image_format == "jpeg":
            options["quality"] = int(quality)
        if clip:
            options["clip"] = clip
        try:
            data = await tab.page.screenshot(**options)
        except PlaywrightError as error:
            raise ActionError(f"screenshot failed: {error}", code="screenshot_failed") from error
    return {
        "format": image_format,
        "data": base64.b64encode(data).decode("ascii"),
        "bytes": len(data),
    }


async def wait(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    timeout = _timeout(params)
    if params.get("forSelector"):
        selector = str(params["forSelector"])
        locator = tab.page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
        except PlaywrightError as error:
            raise _translate_locator_error(error, selector) from error
        return {"found": True}
    if params.get("forText"):
        text = str(params["forText"])
        try:
            await tab.page.wait_for_function(
                "(text) => document.body && document.body.innerText.includes(text)",
                arg=text,
                timeout=timeout,
            )
        except PlaywrightTimeoutError as error:
            raise ActionError(
                f"text {text!r} not found within {timeout}ms", code="not_found"
            ) from error
        return {"found": True}
    milliseconds = max(0, min(int(params.get("ms") or 500), MAX_WAIT_MS))
    await asyncio.sleep(milliseconds / 1000)
    return {"waited": milliseconds}


async def evaluate(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    expression = params.get("expression")
    if not expression:
        raise ActionError('evaluate requires "expression"', code="bad_request")
    try:
        value = await tab.evaluate(str(expression), timeout_ms=_timeout(params))
    except PlaywrightError as error:
        raise ActionError(f"evaluate failed: {error}", code="evaluate_failed") from error
    return {"value": value}


async def get_text(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    try:
        text = await locator.inner_text(timeout=_timeout(params))
    except PlaywrightError as error:
        raise _translate_locator_error(error, selector) from error
    return {"text": text}


async def get_html(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    if params.get("ref") or params.get("selector"):
        locator, selector = locator_for(tab, params)
        try:
            html = await locator.inner_html(timeout=_timeout(params))
        except PlaywrightError as error:
            raise _translate_locator_error(error, selector) from error
        return {"html": html}
    return {"html": await tab.page.content()}


async def snapshot(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    return await tab.snapshot(
        {
            "maxNodes": params.get("maxNodes", 250),
            "includeText": params.get("includeText", True),
            "scope": params.get("scope"),
            "timeoutMs": _timeout(params),
        }
    )


async def history(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    direction = str(params.get("direction") or "back")
    if direction not in ("back", "forward"):
        raise ActionError('history direction must be "back" or "forward"', code="bad_request")
    timeout = _timeout(params, 30_000)
    try:
        response = (
            await tab.page.go_back(wait_until="load", timeout=timeout)
            if direction == "back"
            else await tab.page.go_forward(wait_until="load", timeout=timeout)
        )
    except PlaywrightTimeoutError as error:
        raise ActionError(f"history {direction} timed out", code="timeout") from error
    if response is None:
        raise ActionError(f"no {direction} history entry", code="no_history")
    return {"url": tab.url}


async def reload(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    timeout = _timeout(params, 30_000)
    try:
        await tab.page.reload(wait_until="load", timeout=timeout)
    except PlaywrightTimeoutError as error:
        raise ActionError(f"reload timed out after {timeout}ms", code="timeout") from error
    return {"url": tab.url}


async def accept_dialog(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return await tab.handle_dialog(
            accept=bool(params.get("accept", True)),
            prompt_text=params.get("promptText"),
        )
    except NoDialogError as error:
        raise ActionError(str(error), code="no_dialog") from error
    except DialogError as error:
        raise ActionError(str(error), code="dialog_failed") from error


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ACTIONS: dict[str, ActionFn] = {
    "navigate": navigate,
    "click": click,
    "fill": fill,
    "type": type_text,
    "press": press,
    "select": select,
    "hover": hover,
    "scroll": scroll,
    "check": check,
    "setFiles": set_files,
    "dropFiles": drop_files,
    "screenshot": screenshot,
    "wait": wait,
    "evaluate": evaluate,
    "getText": get_text,
    "getHtml": get_html,
    "snapshot": snapshot,
    "history": history,
    "reload": reload,
    "acceptDialog": accept_dialog,
}

ACTION_DESCRIPTIONS: dict[str, str] = {
    "navigate": "Go to a URL (waitUntil: commit|domcontentloaded|load|networkidle|none).",
    "click": "Click a ref/selector; optional waitForNav, button, clickCount.",
    "fill": "Focus and replace a field's value (clear, pressEnter, delay).",
    "type": "Type into the focused field without clearing it.",
    "press": "Press a keyboard key (Enter, Tab, Control+a, ...).",
    "select": "Choose an <option> by value, label or index.",
    "hover": "Move the pointer over an element.",
    "scroll": "Scroll to a ref/selector or by dx/dy; returns the new position.",
    "check": "Set a checkbox/radio to checked=true|false and report the result.",
    "setFiles": "Set files on an <input type=file>.",
    "dropFiles": "Drop files on a dropzone via a real drag sequence.",
    "screenshot": "Capture PNG/JPEG bytes; the response carries an artifact handle.",
    "wait": "Wait for ms, forSelector or forText.",
    "evaluate": "Evaluate a JS expression in the page and return its value.",
    "getText": "Read innerText from a ref/selector.",
    "getHtml": "Read innerHTML (or the whole page when no target is given).",
    "snapshot": "Return a compact snapshot as the action result.",
    "history": "Navigate back or forward.",
    "reload": "Reload the current page.",
    "acceptDialog": "Accept or dismiss the pending JavaScript dialog.",
}

READ_ONLY = {
    "snapshot",
    "evaluate",
    "getText",
    "getHtml",
    "screenshot",
    "wait",
}


def list_actions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": ACTION_DESCRIPTIONS.get(name, "")} for name in sorted(ACTIONS)
    ]
