"""Owns the Camoufox context and the set of tabs (pages)."""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
from typing import Any

from playwright.async_api import Page

from .browser import BrowserHandle, BrowserLaunchError, launch
from .config import ServerOptions
from .errors import BrowserUnavailable, is_closed_error
from .events import EventBus
from .session import Tab

log = logging.getLogger(__name__)


class UnknownTab(KeyError):
    pass


class Manager:
    def __init__(self, options: ServerOptions) -> None:
        self.options = options
        self.events = EventBus(
            capacity=options.event_capacity, coalesce_window_ms=options.coalesce_ms
        )
        self.tabs: dict[str, Tab] = {}
        self.pages: dict[Page, str] = {}
        self.mru_id: str | None = None
        self.handle: BrowserHandle | None = None
        self.version = "unknown"
        self.last_error: str | None = None
        self._next_id = 1
        self._start_lock = asyncio.Lock()
        self._adopt_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self.handle is not None and not self.handle.closed:
            return
        async with self._start_lock:
            if self.handle is not None and not self.handle.closed:
                return
            if self.handle is not None:
                # The handle outlived its browser (the user closed the window,
                # or the process died). Drop it before relaunching, otherwise
                # every later action fails against a dead context.
                await self._retire_browser("the browser is closed")
            await self._launch_locked()

    async def _launch_locked(self, reason: str | None = None) -> BrowserHandle:
        """Launch the browser and adopt its pages. Holds ``_start_lock``."""
        try:
            self.handle = await launch(self.options)
        except BrowserLaunchError as error:
            self.last_error = str(error)
            raise
        context = self.handle.context
        context.set_default_timeout(self.options.action_timeout_ms)
        context.on("page", self._on_context_page)
        context.on("close", self._on_context_close)
        self.version = self._browser_version()
        for page in list(context.pages):
            await self._adopt_page(page)
        if not self.tabs:
            await self._adopt_page(await context.new_page())
        self.last_error = None
        payload: dict[str, Any] = {
            "status": "ready",
            "version": self.version,
            "headless": self.options.browser.headless,
        }
        if reason:
            payload["reason"] = reason
        self.events.emit("browser", payload)
        return self.handle

    def _browser_version(self) -> str:
        if self.handle is None:
            return "unknown"
        try:
            browser = self.handle.context.browser
            return browser.version if browser is not None else "camoufox"
        except Exception:  # noqa: BLE001
            return "camoufox"

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self.handle is not None:
            try:
                await asyncio.wait_for(self.handle.close(), timeout=15)
            except Exception:  # noqa: BLE001 - shutdown best effort
                log.debug("browser close raised", exc_info=True)
            self.handle = None
        self.tabs.clear()
        self.pages.clear()
        self.mru_id = None

    async def _retire_browser(self, reason: str) -> None:
        """Forget the current browser and everything attached to it."""
        handle = self.handle
        self.handle = None
        self.tabs.clear()
        self.pages.clear()
        self.mru_id = None
        if handle is not None:
            try:
                await asyncio.wait_for(handle.close(), timeout=15)
            except Exception:  # noqa: BLE001 - the browser may already be gone
                log.debug("retiring a dead browser raised", exc_info=True)
        self.events.emit("browser", {"status": "disconnected", "reason": reason})
        log.warning("browser retired: %s", reason)

    async def _probe_alive(self, timeout: float = 3.0) -> bool:
        """Liveness probe: one cheap protocol round trip, not a page load.

        ``context.browser`` is null for a persistent context, so connectivity
        has to be tested by asking the context something it can only answer
        over the wire.
        """
        handle = self.handle
        if handle is None or handle.closed:
            return False
        try:
            await asyncio.wait_for(handle.context.cookies(), timeout=timeout)
        except Exception:  # noqa: BLE001 - any failure means it is not usable
            return False
        return True

    async def restart(self, reason: str = "restart requested") -> BrowserHandle:
        """Relaunch the browser on the same profile, dropping every tab."""
        async with self._start_lock:
            await self._retire_browser(reason)
            return await self._launch_locked(reason)

    async def ensure_alive(self) -> None:
        """Guarantee a usable browser, relaunching a dead one once."""
        if self.handle is None or self.handle.closed:
            await self.start()
            return
        if not await self._probe_alive():
            log.warning("browser context is unresponsive; relaunching")
            await self.restart("the browser stopped responding")

    # -- page adoption -----------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_context_page(self, page: Page) -> None:
        self._spawn(self._adopt_page(page))

    def _on_context_close(self, *_args: Any) -> None:
        """Mark the handle unusable when the context closes behind our back."""
        handle = self.handle
        if handle is None or handle.closed:
            return
        handle.closed = True
        self.events.emit(
            "browser", {"status": "disconnected", "reason": "the browser window closed"}
        )

    async def _adopt_page(self, page: Page) -> Tab | None:
        if page in self.pages:
            return self.tabs.get(self.pages[page])
        async with self._adopt_lock:
            if page in self.pages:
                return self.tabs.get(self.pages[page])
            if page.is_closed():
                return None
            tab_id = f"t{self._next_id}"
            self._next_id += 1
            tab = Tab(
                tab_id,
                page,
                self.events,
                action_timeout_ms=self.options.action_timeout_ms,
                dialog_timeout_ms=self.options.dialog_timeout_ms,
                main_world_eval=self.options.browser.main_world_eval,
            )
            try:
                await tab.attach()
            except Exception as error:  # noqa: BLE001 - page can vanish mid-attach
                log.warning("could not attach to new page: %s", error)
                return None
            tab.on_close(self._drop_tab)
            self.tabs[tab_id] = tab
            self.pages[page] = tab_id
            self.mru_id = tab_id
            self.events.emit(
                "tab",
                {"tabId": tab_id, "url": tab.url, "title": await tab.title()},
                key=f"tab:{tab_id}",
            )
            return tab

    def _drop_tab(self, tab: Tab) -> None:
        self.tabs.pop(tab.id, None)
        self.pages.pop(tab.page, None)
        if self.mru_id == tab.id:
            self.mru_id = next(reversed(self.tabs), None)

    # -- tab API -----------------------------------------------------------

    def touch(self, tab: Tab) -> Tab:
        self.mru_id = tab.id
        return tab

    async def new_tab(self, url: str = "about:blank") -> Tab:
        await self.start()
        try:
            page = await self._open_page()
        except Exception as error:  # noqa: BLE001 - recover from a closed browser
            if not is_closed_error(error):
                raise
            log.warning("new_page failed because the browser is gone; relaunching")
            await self.restart("the browser context was gone")
            page = await self._open_page()
        tab = await self._adopt_page(page)
        if tab is None:
            raise BrowserLaunchError("the new page closed before it could be attached")
        if url and url != "about:blank":
            await tab.page.goto(url, wait_until="load", timeout=30_000)
        return self.touch(tab)

    async def _open_page(self) -> Page:
        if self.handle is None or self.handle.closed:
            raise BrowserUnavailable("browser is not available")
        return await self.handle.context.new_page()

    async def ensure_tab(self, tab_id: str | None = None) -> Tab:
        await self.start()
        if tab_id in (None, "", "default"):
            if self.mru_id and self.mru_id in self.tabs:
                tab = self.tabs[self.mru_id]
                if not tab.closed:
                    return tab
            for candidate in reversed(list(self.tabs.values())):
                if not candidate.closed:
                    return self.touch(candidate)
            return await self.new_tab()
        tab = self.tabs.get(tab_id)
        if tab is None or tab.closed:
            raise UnknownTab(tab_id)
        return self.touch(tab)

    async def close_tab(self, tab_id: str) -> None:
        tab = await self.ensure_tab(tab_id)
        await tab.close()
        self._drop_tab(tab)

    async def focus_tab(self, tab_id: str) -> Tab:
        tab = await self.ensure_tab(tab_id)
        await tab.focus()
        return self.touch(tab)

    async def list_tabs(self) -> list[dict[str, Any]]:
        if not self.tabs:
            return []
        titles = await asyncio.gather(*(tab.title() for tab in self.tabs.values()))
        out = []
        for (tab_id, tab), title in zip(self.tabs.items(), titles, strict=True):
            out.append(
                {
                    "id": tab_id,
                    "url": tab.url,
                    "title": title,
                    "active": tab_id == self.mru_id,
                    "pendingRequests": tab.pending_count,
                    "dialog": tab.pending_dialog,
                }
            )
        return out

    async def health(self) -> dict[str, Any]:
        # /health is where an agent lands after a mysterious failure, so it heals
        # as well as reports: a closed window must not need a manual process
        # restart to become usable again.
        previous = self.handle
        try:
            await self.ensure_alive()
        except BrowserLaunchError as error:
            self.last_error = str(error)
            return {
                "ok": False,
                "code": "browser_launch_failed",
                "error": str(error),
                "retryable": True,
                "browser": None,
                "tabs": [],
                "cursor": self.events.seq,
            }
        recovered = previous is not None and self.handle is not previous
        tabs = await self.list_tabs()
        dialogs = [{"tabId": tab["id"], **tab["dialog"]} for tab in tabs if tab.get("dialog")]
        body: dict[str, Any] = {
            "ok": True,
            "browser": {
                "name": "camoufox",
                "version": self.version,
                "packageVersion": _package_version("camoufox"),
                "headless": self.options.browser.headless,
                "profile": str(self.options.browser.profile),
                "humanize": self.options.browser.humanize,
                "connected": True,
                "fingerprint": self.handle.fingerprint_meta if self.handle else None,
            },
            "tabs": tabs,
            "defaultTab": self.mru_id,
            "pendingDialogs": dialogs,
            "cursor": self.events.seq,
        }
        if recovered:
            body["recovered"] = True
        return body


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
