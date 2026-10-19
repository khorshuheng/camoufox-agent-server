"""Action vocabulary implemented with Playwright locators.

Actions accept either a ``ref`` from a snapshot or a CSS ``selector``. Playwright
dispatches input through Juggler, so Camoufox sees native input events rather
than synthetic DOM calls.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
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


def _frame_root(tab: Tab, frame: Any) -> Any:
    target: Any = tab.page
    frames = frame if isinstance(frame, list) else [frame]
    for frame_selector in frames:
        target = target.frame_locator(str(frame_selector))
    return target


def locator_for(tab: Tab, params: dict[str, Any]) -> tuple[Locator, str]:
    """Resolve a ref/selector, optionally inside one or more iframes.

    ``frame`` is a CSS selector for the iframe, or a list of selectors for
    nested frames. Playwright's frame locator pierces same- and cross-origin
    iframes, so the rest of the action layer is unchanged.
    """
    selector = selector_for(params)
    frame = params.get("frame")
    if not frame:
        return tab.page.locator(selector).first, selector
    return _frame_root(tab, frame).locator(selector).first, selector


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


async def _poll_until(predicate: Any, timeout_ms: int, interval: float = 0.1) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return True
        await asyncio.sleep(interval)
    return False


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

    wait_timeout = _timeout(params, timeout)
    timed_out = None
    if params.get("waitFor"):
        wait_selector = str(params["waitFor"])
        try:
            wait_locator, _ = locator_for(
                tab, {"selector": wait_selector, "frame": params.get("frame")}
            )
            await wait_locator.wait_for(
                state=str(params.get("waitState") or "visible"), timeout=wait_timeout
            )
        except PlaywrightError:
            timed_out = f"selector:{wait_selector}"
    if params.get("waitForText"):
        wait_text = str(params["waitForText"])
        try:
            await tab.page.wait_for_function(
                "(text) => document.body && document.body.innerText.includes(text)",
                arg=wait_text,
                timeout=wait_timeout,
            )
        except PlaywrightError:
            timed_out = f"text:{wait_text}"
    if params.get("waitForUrl"):
        wait_url = str(params["waitForUrl"])
        found = await _poll_until(lambda: wait_url in tab.url, wait_timeout)
        if not found:
            timed_out = f"url:{wait_url}"

    result: dict[str, Any] = {
        "url": tab.url,
        "status": response.status if response else None,
    }
    if timed_out:
        result["waitForTimedOut"] = timed_out
    return result


BLOCKER_JS = r"""
(args) => {
  const top = document.elementFromPoint(args.x, args.y);
  if (!top) return { blocked: false };
  const target = document.querySelector(args.selector);
  if (target && (top === target || target.contains(top) || top.contains(target))) {
    return { blocked: false };
  }
  return {
    blocked: true,
    blocker: {
      tag: top.tagName,
      id: top.id || null,
      class: String(top.className || '').slice(0, 120),
      role: top.getAttribute('role') || null,
      text: (top.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160),
    },
  };
}
"""


async def _blocker_at(tab: Tab, box: dict[str, Any], selector: str) -> dict[str, Any] | None:
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    try:
        probe = await tab.evaluate_fn(
            BLOCKER_JS, {"x": x, "y": y, "selector": selector}, timeout_ms=3000
        )
    except Exception:  # noqa: BLE001 - diagnostics are best effort
        return None
    return probe.get("blocker") if probe and probe.get("blocked") else None


async def click(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    locator, selector = locator_for(tab, params)
    button = str(params.get("button") or "left")
    if button not in ("left", "right", "middle"):
        raise ActionError(f'unsupported button "{button}"', code="bad_request")
    timeout = _timeout(params)
    force = bool(params.get("force"))
    box = None
    try:
        box = await locator.bounding_box(timeout=timeout)
    except PlaywrightError:
        pass
    try:
        await locator.click(
            button=button,
            click_count=int(params.get("clickCount") or 1),
            timeout=timeout,
            force=force,
        )
    except PlaywrightError as error:
        if box and not force and not params.get("frame"):
            blocker = await _blocker_at(tab, box, selector)
            if blocker:
                raise ActionError(
                    f"element {selector} is covered by <{blocker['tag'].lower()}>",
                    code="blocked",
                    details={
                        "selector": selector,
                        "blocker": blocker,
                        "hint": "close the overlay, or retry with force:true",
                    },
                ) from error
        raise _translate_locator_error(error, selector) from error
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
    if params.get("frame"):
        raise ActionError(
            "dropFiles does not support frame yet; use setFiles on the file input",
            code="bad_request",
            details={"hint": "dropFiles dispatches a DataTransfer in the main document"},
        )
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


EXTRACT_JS = r"""
(args) => {
  const deepAll = (root, selector) => {
    const out = [];
    const walk = (node) => {
      for (const el of node.querySelectorAll('*')) {
        if (el.matches(selector)) out.push(el);
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    walk(root);
    return out;
  };
  const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const first = (root, selector) => {
    if (!selector) return root;
    if (root.matches && root.matches(selector)) return root;
    return deepAll(root, selector)[0] || null;
  };
  const root = args.scope ? document.querySelector(args.scope) : document;
  if (!root) return { count: 0, rows: [], error: 'scope-not-found' };
  const rows = deepAll(root, args.selector);
  const read = (el, spec) => {
    if (!el) return null;
    if (spec.attr) return el.getAttribute(spec.attr);
    if (spec.html) return el.innerHTML;
    if (spec.value) return ('value' in el ? el.value : null);
    return textOf(el);
  };
  const pick = (row, rawSpec) => {
    const spec = typeof rawSpec === 'string' ? { selector: rawSpec } : (rawSpec || {});
    if (spec.all) {
      const selector = spec.selector || '*';
      return deepAll(row, selector).map((el) => read(el, spec));
    }
    return read(first(row, spec.selector || null), spec);
  };
  const limit = Math.max(1, Math.min(Number(args.limit) || 100, 500));
  const out = rows.slice(0, limit).map((row) => {
    const item = {};
    for (const [key, spec] of Object.entries(args.fields || {})) item[key] = pick(row, spec);
    if (args.includeText) item.text = textOf(row).slice(0, Number(args.maxText) || 500);
    return item;
  });
  return {
    count: rows.length,
    rows: out,
    truncated: rows.length > limit || undefined,
  };
}
"""


def _field_spec(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return {"selector": raw}
    if isinstance(raw, dict):
        return raw
    raise ActionError(
        "extract field specs must be a selector string or an object", code="bad_request"
    )


async def _extract_with_locator(
    tab: Tab, selector: str, fields: dict[str, Any], params: dict[str, Any], limit: int
) -> dict[str, Any]:
    frame = params.get("frame")
    rows = _frame_root(tab, frame).locator(selector) if frame else tab.page.locator(selector)
    total = await rows.count()
    out = []
    for index in range(min(total, limit)):
        row = rows.nth(index)
        item: dict[str, Any] = {}
        for key, raw_spec in fields.items():
            spec = _field_spec(raw_spec)
            target = row if not spec.get("selector") else row.locator(str(spec["selector"])).first
            try:
                if spec.get("all"):
                    all_target = (
                        row if not spec.get("selector") else row.locator(str(spec["selector"]))
                    )
                    values = await all_target.all_inner_texts()
                    item[key] = [value.strip() for value in values]
                elif spec.get("attr"):
                    item[key] = await target.get_attribute(str(spec["attr"]), timeout=3000)
                elif spec.get("html"):
                    item[key] = await target.inner_html(timeout=3000)
                elif spec.get("value"):
                    item[key] = await target.input_value(timeout=3000)
                else:
                    item[key] = (await target.inner_text(timeout=3000)).strip()
            except PlaywrightError:
                item[key] = None
        if params.get("includeText"):
            try:
                item["text"] = (await row.inner_text(timeout=3000)).strip()[
                    : int(params.get("maxText") or 500)
                ]
            except PlaywrightError:
                item["text"] = None
        out.append(item)
    result = {"count": total, "rows": out}
    if total > limit:
        result["truncated"] = True
    return result


async def extract(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    selector = params.get("selector")
    fields = params.get("fields")
    if not selector:
        raise ActionError('extract requires "selector" for the repeated rows', code="bad_request")
    if not isinstance(fields, dict) or not fields:
        raise ActionError('extract requires a non-empty "fields" object', code="bad_request")
    limit = max(1, min(int(params.get("limit") or 100), 500))
    wait_ms = max(0, min(int(params.get("timeoutMs") or 0), 120_000))
    if wait_ms > 0:
        try:
            await locator_for(tab, {"selector": str(selector), "frame": params.get("frame")})[
                0
            ].wait_for(state="attached", timeout=wait_ms)
        except PlaywrightError:
            if params.get("required"):
                raise ActionError(
                    f"no elements matching {selector} appeared within {wait_ms}ms",
                    code="not_found",
                    details={"selector": str(selector)},
                ) from None
    if params.get("frame"):
        return await _extract_with_locator(tab, str(selector), fields, params, limit)
    result = await tab.evaluate_fn(
        EXTRACT_JS,
        {
            "selector": str(selector),
            "fields": {key: _field_spec(value) for key, value in fields.items()},
            "limit": limit,
            "scope": params.get("scope"),
            "includeText": bool(params.get("includeText")),
            "maxText": int(params.get("maxText") or 500),
        },
        timeout_ms=_timeout(params, 20_000),
    )
    return result or {"count": 0, "rows": []}


FIND_OVERLAY_JS = r"""
(args) => {
  const deepAll = (root, selector) => {
    const out = [];
    const walk = (node) => {
      for (const el of node.querySelectorAll('*')) {
        if (el.matches(selector)) out.push(el);
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    walk(root);
    return out;
  };
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const nameOf = (el) => (
    el.getAttribute('aria-label') || el.getAttribute('title') ||
    el.innerText || el.textContent || ''
  ).replace(/\s+/g, ' ').trim().toLowerCase();
  const overlayish = (el) => {
    let node = el;
    for (let i = 0; i < 4 && node; i += 1, node = node.parentElement) {
      const cls = typeof node.className === 'string' ? node.className : '';
      const testId = node.getAttribute('data-testid') || '';
      const role = node.getAttribute('role') || '';
      const blob = `${cls} ${node.id || ''} ${role} ${testId}`.toLowerCase();
      if (/cookie|consent|modal|overlay|dialog|popup|banner|gdpr/.test(blob)) return true;
    }
    return false;
  };
  const labels = (args.labels || []).map((s) => s.toLowerCase());
  const dialogs = deepAll(document, '[role="dialog"],[aria-modal="true"]');
  const controls = deepAll(
    document,
    'button,a,[role="button"],input[type="button"],input[type="submit"]'
  );
  for (const el of controls) {
    if (!visible(el)) continue;
    const name = nameOf(el);
    const matched = labels.some((label) => name === label || name.includes(label));
    if (!matched) continue;
    if (!overlayish(el) && !dialogs.some((dialog) => dialog.contains(el))) continue;
    el.setAttribute('data-cf-dismiss', '1');
    return { found: true, label: name, tag: el.tagName };
  }
  return { found: false };
}
"""

DEFAULT_DISMISS_LABELS = [
    "close",
    "dismiss",
    "no thanks",
    "not now",
    "maybe later",
    "got it",
    "continue without accepting",
    "skip",
    "×",
]
COOKIE_ACCEPT_LABELS = [
    "accept all",
    "accept cookies",
    "i agree",
    "agree",
    "allow all",
    "accept",
]


async def close_overlays(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    labels = [str(label) for label in params.get("labels") or DEFAULT_DISMISS_LABELS]
    if params.get("acceptCookies"):
        labels.extend(COOKIE_ACCEPT_LABELS)
    max_clicks = max(0, min(int(params.get("max") or 5), 20))
    clicked: list[str] = []
    for _ in range(max_clicks):
        try:
            found = await tab.evaluate_fn(
                FIND_OVERLAY_JS, {"labels": labels}, timeout_ms=_timeout(params, 5_000)
            )
        except Exception:  # noqa: BLE001 - overlay cleanup is best effort
            break
        if not found or not found.get("found"):
            break
        control = tab.page.locator('[data-cf-dismiss="1"]').first
        handled = False
        for attempt in (False, True):
            try:
                await control.click(timeout=2_000, force=attempt)
                handled = True
                break
            except PlaywrightError:
                continue
        if not handled:
            try:
                await control.evaluate("(el) => el.click()")
                handled = True
            except PlaywrightError:
                handled = False
        await tab.evaluate_fn(
            "() => document.querySelectorAll('[data-cf-dismiss]').forEach("
            "(el) => el.removeAttribute('data-cf-dismiss'))",
            timeout_ms=3_000,
        )
        if not handled:
            break
        clicked.append(str(found.get("label") or found.get("tag") or "overlay"))
        await asyncio.sleep(0.2)
    return {"clicked": clicked, "count": len(clicked)}


async def click_at(tab: Tab, params: dict[str, Any]) -> dict[str, Any]:
    try:
        x = float(params["x"])
        y = float(params["y"])
    except (KeyError, TypeError, ValueError) as error:
        raise ActionError('clickAt requires numeric "x" and "y"', code="bad_request") from error
    button = str(params.get("button") or "left")
    if button not in ("left", "right", "middle"):
        raise ActionError(f'unsupported button "{button}"', code="bad_request")
    steps = max(1, min(int(params.get("steps") or 8), 100))
    await tab.page.mouse.move(x, y, steps=steps)
    await tab.page.mouse.click(x, y, button=button, click_count=int(params.get("clickCount") or 1))
    return {"clicked": True, "at": {"x": round(x), "y": round(y)}}


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
        min_count = int(params.get("minCount") or 0)
        frame = params.get("frame")
        if min_count > 0:
            if frame:
                locator = _frame_root(tab, frame).locator(selector)

                async def enough() -> bool:
                    return await locator.count() >= min_count

                found = await _poll_until(enough, timeout)
            else:
                try:
                    await tab.page.wait_for_function(
                        "([sel, n]) => document.querySelectorAll(sel).length >= n",
                        arg=[selector, min_count],
                        timeout=timeout,
                    )
                    found = True
                except PlaywrightTimeoutError:
                    found = False
            if not found:
                raise ActionError(
                    f"fewer than {min_count} elements match {selector} within {timeout}ms",
                    code="not_found",
                    details={"selector": selector, "minCount": min_count},
                )
            return {"found": True, "count": min_count}
        locator, _ = locator_for(tab, {"selector": selector, "frame": frame})
        try:
            await locator.wait_for(state=str(params.get("state") or "visible"), timeout=timeout)
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
    if params.get("forUrl"):
        needle = str(params["forUrl"])
        if not await _poll_until(lambda: needle in tab.url, timeout):
            raise ActionError(
                f"URL never contained {needle!r} within {timeout}ms",
                code="not_found",
                details={"url": tab.url},
            )
        return {"found": True, "url": tab.url}
    if params.get("forFunction"):
        expression = str(params["forFunction"])
        try:
            await tab.page.wait_for_function(expression, timeout=timeout)
        except PlaywrightError as error:
            raise ActionError(
                f"condition {expression!r} was not truthy within {timeout}ms",
                code="not_found",
            ) from error
        return {"found": True}
    if params.get("forNetworkIdle"):
        try:
            await tab.page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeoutError as error:
            raise ActionError(
                f"network did not become idle within {timeout}ms", code="timeout"
            ) from error
        return {"idle": True}
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
    "extract": extract,
    "closeOverlays": close_overlays,
    "clickAt": click_at,
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
    "extract": "Extract compact rows from repeated elements using a selector and field map.",
    "closeOverlays": "Close visible cookie/login/dialog overlays (acceptCookies optional).",
    "clickAt": "Click viewport coordinates (x, y); useful for canvas and iframe widgets.",
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
    "extract",
}


def list_actions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": ACTION_DESCRIPTIONS.get(name, "")} for name in sorted(ACTIONS)
    ]
