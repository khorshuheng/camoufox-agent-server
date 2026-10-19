"""One Camoufox page: event collection, evaluation and snapshots."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from playwright.async_api import (
    ConsoleMessage,
    Dialog,
    Download,
    Frame,
    Page,
    Request,
    Response,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from .challenge import detect_challenge
from .events import EventBus
from .observe import SCROLL_POSITION_JS, SNAPSHOT_JS

log = logging.getLogger(__name__)


class Tab:
    def __init__(
        self,
        tab_id: str,
        page: Page,
        events: EventBus,
        *,
        action_timeout_ms: int = 15_000,
        dialog_timeout_ms: int = 30_000,
        collect_network: bool = True,
        collect_console: bool = True,
        main_world_eval: bool = True,
    ) -> None:
        self.id = tab_id
        self.page = page
        self.events = events
        self.action_timeout_ms = action_timeout_ms
        self.dialog_timeout_ms = dialog_timeout_ms
        self.collect_network = collect_network
        self.collect_console = collect_console
        self.main_world_eval = main_world_eval
        self.requests: dict[Request, dict[str, Any]] = {}
        self.navigation_id = 0
        self.attached = False
        self.lock = asyncio.Lock()
        self._dialog: Dialog | None = None
        self._dialog_info: dict[str, Any] | None = None
        self._dialog_timer: asyncio.TimerHandle | None = None
        self._closed = False

    # -- wiring ------------------------------------------------------------

    async def attach(self) -> Tab:
        self.page.set_default_timeout(self.action_timeout_ms)
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_page_error)
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("requestfinished", self._on_request_done)
        self.page.on("requestfailed", self._on_request_failed)
        self.page.on("framenavigated", self._on_frame_navigated)
        self.page.on("load", self._on_load)
        self.page.on("domcontentloaded", self._on_dom_content_loaded)
        self.page.on("dialog", self._on_dialog)
        self.page.on("download", self._on_download)
        self.page.on("popup", self._on_popup)
        self.page.on("crash", self._on_crash)
        self.page.on("close", self._on_close)
        self.attached = True
        return self

    def _emit(self, event_type: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.events.emit(
            event_type,
            {"tabId": self.id, "navigationId": self.navigation_id, **data},
            **kwargs,
        )

    # -- event handlers ----------------------------------------------------

    def _on_console(self, message: ConsoleMessage) -> None:
        if not self.collect_console:
            return
        text = message.text[:2000]
        location = message.location or {}
        url = location.get("url")
        if _is_extension_url(url):
            return
        self._emit(
            "console",
            {
                "level": message.type,
                "text": text,
                "url": url,
                "documentUrl": self.url,
            },
            key=f"console:{message.type}:{text}",
            summary=text[:140],
        )

    def _on_page_error(self, error: PlaywrightError) -> None:
        if not self.collect_console:
            return
        message = str(error)
        if _is_extension_url(message):
            return
        self._emit(
            "pageerror",
            {
                "message": message,
                "stack": getattr(error, "stack", None),
                "documentUrl": self.url,
            },
            key=f"pageerror:{message[:180]}",
        )

    def _on_request(self, request: Request) -> None:
        if not self.collect_network or _is_extension_url(request.url):
            return
        document_url = _request_document_url(request, self.url)
        self.requests[request] = {
            "method": request.method,
            "url": request.url,
            "resourceType": request.resource_type,
            "ts": time.time(),
            "navigationId": self.navigation_id,
            "documentUrl": document_url,
        }
        self._emit(
            "request",
            {
                "method": request.method,
                "url": request.url,
                "resourceType": request.resource_type,
                "frame": bool(request.frame != self.page.main_frame),
                "documentUrl": document_url,
                "navigationId": self.navigation_id,
            },
            key=f"req:{request.method}:{request.url}",
        )

    def _on_response(self, response: Response) -> None:
        if not self.collect_network or _is_extension_url(response.url):
            return
        request = response.request
        info = self.requests.get(request, {})
        if info and info.get("navigationId") != self.navigation_id:
            self.requests.pop(request, None)
            return
        self._emit(
            "response",
            {
                "status": response.status,
                "statusText": response.status_text,
                "method": info.get("method", request.method),
                "url": response.url,
                "mimeType": _safe_header(response, "content-type"),
                "resourceType": info.get("resourceType", request.resource_type),
                "documentUrl": info.get("documentUrl"),
                "navigationId": info.get("navigationId", self.navigation_id),
            },
            key=f"resp:{response.url}:{response.status}",
        )

    def _on_request_done(self, request: Request) -> None:
        self.requests.pop(request, None)

    def _on_request_failed(self, request: Request) -> None:
        if not self.collect_network or _is_extension_url(request.url):
            return
        info = self.requests.pop(request, {})
        if info and info.get("navigationId") != self.navigation_id:
            return
        self._emit(
            "requestfailed",
            {
                "url": request.url,
                "method": info.get("method", request.method),
                "failure": request.failure or "unknown",
                "resourceType": info.get("resourceType", request.resource_type),
                "documentUrl": info.get("documentUrl"),
                "navigationId": info.get("navigationId", self.navigation_id),
            },
            key=f"fail:{request.url}:{request.failure}",
        )

    def _on_frame_navigated(self, frame: Frame) -> None:
        if frame != self.page.main_frame:
            return
        # Bump first: events from the previous document that arrive late are
        # tagged with the old id and dropped from this command's delta.
        self.navigation_id += 1
        self._emit("navigation", {"url": frame.url}, key="navigation")

    def _on_load(self, _page: Page) -> None:
        self._emit("load", {}, key="load")

    def _on_dom_content_loaded(self, _page: Page) -> None:
        self._emit("domcontentloaded", {})

    def _on_download(self, download: Download) -> None:
        self._emit(
            "download",
            {"url": download.url, "filename": download.suggested_filename},
            key=f"download:{download.suggested_filename}",
        )

    def _on_popup(self, page: Page) -> None:
        self._emit("popup", {"url": page.url}, key="popup")

    def _on_crash(self, _page: Page) -> None:
        self._emit("crash", {})

    def _on_close(self, _page: Page) -> None:
        self._closed = True
        self.attached = False
        self._clear_dialog_timer()
        self._emit("tabclosed", {}, key=f"closed:{self.id}")

    # -- dialogs -----------------------------------------------------------

    def _on_dialog(self, dialog: Dialog) -> None:
        self._dialog = dialog
        self._dialog_info = {
            "kind": dialog.type,
            "message": dialog.message,
            "defaultValue": dialog.default_value,
            "ts": time.time(),
        }
        self._emit("dialog", dict(self._dialog_info), key="dialog")
        self._clear_dialog_timer()
        if self.dialog_timeout_ms > 0:
            loop = asyncio.get_running_loop()
            self._dialog_timer = loop.call_later(
                self.dialog_timeout_ms / 1000, self._schedule_auto_dismiss
            )

    def _schedule_auto_dismiss(self) -> None:
        asyncio.create_task(self._auto_dismiss_dialog())

    async def _auto_dismiss_dialog(self) -> None:
        dialog = self._dialog
        if dialog is None:
            return
        info = self._dialog_info or {}
        self._dialog = None
        self._dialog_info = None
        self._clear_dialog_timer()
        try:
            await dialog.dismiss()
        except Exception:  # noqa: BLE001 - the user may have handled it already
            pass
        self._emit("dialogtimeout", {"dismissed": True, "kind": info.get("kind")})

    def _clear_dialog_timer(self) -> None:
        if self._dialog_timer is not None:
            self._dialog_timer.cancel()
            self._dialog_timer = None

    @property
    def pending_dialog(self) -> dict[str, Any] | None:
        return dict(self._dialog_info) if self._dialog_info else None

    async def handle_dialog(
        self, *, accept: bool = True, prompt_text: str | None = None
    ) -> dict[str, Any]:
        dialog = self._dialog
        if dialog is None:
            raise NoDialogError("no JavaScript dialog is open")
        info = self._dialog_info or {}
        self._dialog = None
        self._dialog_info = None
        self._clear_dialog_timer()
        try:
            if accept:
                await dialog.accept(prompt_text)
            else:
                await dialog.dismiss()
        except Exception as error:  # noqa: BLE001 - user may have closed it in the window
            message = str(error).lower()
            if "already" in message or "closed" in message or "not found" in message:
                return {"handled": True, "alreadyClosed": True, "kind": info.get("kind")}
            raise DialogError(f"could not handle dialog: {error}") from error
        return {"handled": True, "accepted": accept, "kind": info.get("kind")}

    async def challenge(self) -> dict[str, Any] | None:
        try:
            info = await self.evaluate_fn(
                """() => ({
                  title: document.title,
                  text: (document.body ? document.body.innerText : '').slice(0, 8000),
                })""",
                timeout_ms=5_000,
            )
        except Exception:  # noqa: BLE001 - challenge detection is best effort
            return None
        return detect_challenge(info.get("title"), info.get("text"))

    # -- evaluation --------------------------------------------------------

    def _js(self, script: str) -> str:
        # Camoufox runs page.evaluate in an isolated world by default. Prefixing
        # with "mw:" opts into the page's own world, which is what an agent
        # expects: page globals are visible and DOM events carry page-owned
        # objects (e.g. a DataTransfer with files).
        return f"mw:{script}" if self.main_world_eval else script

    async def evaluate(self, expression: str, *, timeout_ms: int = 20_000) -> Any:
        return await asyncio.wait_for(
            self.page.evaluate(self._js(expression)), timeout=timeout_ms / 1000
        )

    async def evaluate_fn(
        self, function_js: str, arg: Any = None, *, timeout_ms: int = 20_000
    ) -> Any:
        return await asyncio.wait_for(
            self.page.evaluate(self._js(function_js.strip()), arg), timeout=timeout_ms / 1000
        )

    async def snapshot(self, opts: dict[str, Any] | None = None) -> dict[str, Any]:
        options = dict(opts or {})
        timeout_ms = int(options.pop("timeoutMs", 15_000))
        return await self.evaluate_fn(SNAPSHOT_JS, options, timeout_ms=timeout_ms)

    async def scroll_position(self) -> dict[str, Any]:
        return await self.evaluate_fn(SCROLL_POSITION_JS, timeout_ms=5_000)

    # -- lifecycle ---------------------------------------------------------

    async def title(self) -> str:
        try:
            return await asyncio.wait_for(self.page.title(), timeout=2)
        except Exception:  # noqa: BLE001 - title is cosmetic
            return ""

    @property
    def url(self) -> str:
        try:
            return self.page.url
        except Exception:  # noqa: BLE001
            return "about:blank"

    @property
    def closed(self) -> bool:
        return self._closed or self.page.is_closed()

    @property
    def pending_count(self) -> int:
        return len(self.requests)

    async def focus(self) -> None:
        await self.page.bring_to_front()

    async def close(self) -> None:
        self._clear_dialog_timer()
        if not self.page.is_closed():
            try:
                await self.page.close()
            except Exception:  # noqa: BLE001 - already closing
                pass
        self.attached = False
        self._closed = True

    def on_close(self, callback: Callable[[Tab], None]) -> None:
        self.page.on("close", lambda _page: callback(self))


class NoDialogError(RuntimeError):
    pass


class DialogError(RuntimeError):
    pass


EXTENSION_PREFIXES = (
    "moz-extension://",
    "chrome-extension://",
    "safari-extension://",
    "safari-web-extension://",
    "ms-browser-extension://",
    "edge-extension://",
)


def _is_extension_url(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return any(lowered.startswith(prefix) for prefix in EXTENSION_PREFIXES)


def _request_document_url(request: Request, fallback: str) -> str:
    try:
        return request.frame.url or fallback
    except Exception:  # noqa: BLE001 - detached frames
        return fallback


def _safe_header(response: Response, name: str) -> str | None:
    try:
        return response.headers.get(name)
    except Exception:  # noqa: BLE001
        return None
