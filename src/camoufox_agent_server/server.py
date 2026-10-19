"""HTTP + SSE API.

The interface is deliberately small and agent-shaped:

    GET  /health                    browser state, tabs, pending dialogs
    GET  /actions                   action vocabulary
    GET  /tabs                      list tabs
    POST /tabs                      open a tab                 {url}
    DELETE /tabs/{id}               close a tab
    POST /tabs/{id}/focus           raise the headed window
    GET  /tabs/{id}/snapshot        compact tree with refs
    POST /tabs/{id}/act             run one action
    POST /tabs/{id}/batch           run N actions, get N deltas
    GET  /tabs/{id}/dialog          pending JavaScript dialog
    POST /tabs/{id}/dialog          accept/dismiss it
    GET  /tabs/{id}/events          SSE stream since ?cursor=
    GET  /tabs/{id}/events.json     polling variant
    GET  /events                    SSE stream across tabs
    GET  /artifacts/{id}            stowed screenshot bytes
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from .actions import ActionError, list_actions
from .config import ServerOptions
from .events import Event, EventBus, frame
from .executor import Executor
from .manager import Manager, UnknownTab

log = logging.getLogger(__name__)

MAX_BATCH = 500


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class NewTabRequest(BaseModel):
    url: str = "about:blank"


class BatchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    commands: list[dict[str, Any]]
    observe: str = "auto"
    settle_ms: int | None = Field(default=None, alias="settleMs")
    stop_on_error: bool = Field(default=False, alias="stopOnError")
    timeout_ms: int = Field(default=30_000, alias="timeoutMs")


class DialogRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    accept: bool = True
    prompt_text: str | None = Field(default=None, alias="promptText")


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class ArtifactStore:
    def __init__(self, ttl_s: int = 600) -> None:
        self.ttl_s = ttl_s
        self.items: dict[str, dict[str, Any]] = {}

    def put(self, data: bytes, media_type: str) -> str:
        artifact_id = uuid.uuid4().hex
        self.items[artifact_id] = {"data": data, "type": media_type, "ts": time.time()}
        return artifact_id

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        return self.items.get(artifact_id)

    def gc(self) -> None:
        cutoff = time.time() - self.ttl_s
        for artifact_id in [key for key, value in self.items.items() if value["ts"] < cutoff]:
            self.items.pop(artifact_id, None)

    def stow(self, value: Any) -> Any:
        if isinstance(value, dict):
            encoded = value.pop("__artifactData", None)
            if encoded:
                image_format = "jpeg" if value.get("format") == "jpeg" else "png"
                artifact_id = self.put(base64.b64decode(encoded), f"image/{image_format}")
                value["artifact"] = f"/artifacts/{artifact_id}"
            for child in value.values():
                self.stow(child)
        elif isinstance(value, list):
            for child in value:
                self.stow(child)
        return value


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(options: ServerOptions | None = None) -> FastAPI:
    options = options or ServerOptions()
    manager = Manager(options)
    executor = Executor(default_settle_ms=options.settle_ms)
    artifacts = ArtifactStore(options.artifact_ttl_s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await manager.start()
        except Exception as error:  # noqa: BLE001 - /health must still answer
            log.error("browser launch failed: %s", error)
        gc_task = asyncio.create_task(_gc_loop(artifacts))
        try:
            yield
        finally:
            gc_task.cancel()
            await manager.close()

    app = FastAPI(
        title="camoufox-agent-server",
        version="0.1.0",
        description="Headed Camoufox control server for coding agents",
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.state.executor = executor
    app.state.artifacts = artifacts
    app.state.options = options

    # -- errors ------------------------------------------------------------

    @app.exception_handler(UnknownTab)
    async def _unknown_tab(_request: Request, exc: UnknownTab) -> JSONResponse:
        return JSONResponse({"error": f"unknown tab {exc.args[0]!r}"}, status_code=404)

    @app.exception_handler(ActionError)
    async def _action_error(_request: Request, exc: ActionError) -> JSONResponse:
        payload: dict[str, Any] = {"error": str(exc), "code": exc.code}
        if exc.details:
            payload["details"] = exc.details
        return JSONResponse(payload, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": "invalid request", "details": jsonable_encoder(exc.errors())},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled request failure")
        payload: dict[str, Any] = {"error": str(exc) or exc.__class__.__name__}
        if options.debug:
            import traceback

            payload["stack"] = traceback.format_exc()
        return JSONResponse(payload, status_code=500)

    # -- discovery ---------------------------------------------------------

    @app.get("/")
    async def index() -> dict[str, Any]:
        return {
            "name": "camoufox-agent-server",
            "browser": "camoufox",
            "routes": [
                "GET /health",
                "GET /actions",
                "GET /tabs",
                "POST /tabs",
                "DELETE /tabs/{id}",
                "POST /tabs/{id}/focus",
                "GET /tabs/{id}/snapshot",
                "POST /tabs/{id}/act",
                "POST /tabs/{id}/batch",
                "GET|POST /tabs/{id}/dialog",
                "GET /tabs/{id}/events",
                "GET /tabs/{id}/events.json",
                "GET /events",
                "GET /artifacts/{id}",
            ],
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return await manager.health()

    @app.get("/actions")
    async def actions_catalog() -> dict[str, Any]:
        return {
            "actions": list_actions(),
            "usage": {
                "single": 'POST /tabs/default/act {"action":"click","ref":"e5"}',
                "batch": (
                    'POST /tabs/default/batch {"commands":'
                    '[{"action":"navigate","url":"https://example.com"},'
                    '{"action":"click","ref":"e5"}],"observe":"auto"}'
                ),
            },
        }

    # -- tabs --------------------------------------------------------------

    @app.get("/tabs")
    async def list_tabs() -> dict[str, Any]:
        return {"tabs": await manager.list_tabs(), "default": manager.mru_id}

    @app.post("/tabs", status_code=201)
    async def create_tab(body: NewTabRequest | None = None) -> dict[str, Any]:
        tab = await manager.new_tab(body.url if body else "about:blank")
        return {"id": tab.id, "url": tab.url, "title": await tab.title()}

    @app.delete("/tabs/{tab_id}")
    async def delete_tab(tab_id: str) -> dict[str, Any]:
        resolved = await manager.ensure_tab(tab_id)
        await manager.close_tab(resolved.id)
        return {"closed": resolved.id}

    @app.post("/tabs/{tab_id}/focus")
    async def focus_tab(tab_id: str) -> dict[str, Any]:
        tab = await manager.focus_tab(tab_id)
        return {"focused": True, "id": tab.id, "url": tab.url}

    # -- observation and actions ------------------------------------------

    @app.get("/tabs/{tab_id}/snapshot")
    async def snapshot(
        tab_id: str,
        max_nodes: int = Query(default=250, alias="maxNodes", ge=1, le=2000),
        scope: str | None = Query(default=None),
        include_text: bool = Query(default=True, alias="includeText"),
    ) -> dict[str, Any]:
        tab = await manager.ensure_tab(tab_id)
        async with tab.lock:
            return await tab.snapshot(
                {"maxNodes": max_nodes, "scope": scope, "includeText": include_text}
            )

    @app.post("/tabs/{tab_id}/act")
    async def act(tab_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        tab = await manager.ensure_tab(tab_id)
        async with tab.lock:
            out = await executor.run(
                tab,
                [body],
                observe=str(body.get("observe") or "auto"),
                settle_ms=_optional_int(body.get("settleMs")),
                stop_on_error=True,
            )
        result = out["results"][0]
        payload: dict[str, Any] = {"cursor": out["cursor"], **result}
        if "snapshot" in out:
            payload["snapshot"] = out["snapshot"]
        return artifacts.stow(payload)

    @app.post("/tabs/{tab_id}/batch")
    async def batch(tab_id: str, body: BatchRequest) -> dict[str, Any]:
        if len(body.commands) > MAX_BATCH:
            raise ActionError(
                f"too many commands ({len(body.commands)}); max {MAX_BATCH} per batch",
                code="bad_request",
            )
        tab = await manager.ensure_tab(tab_id)
        async with tab.lock:
            out = await executor.run(
                tab,
                body.commands,
                observe=body.observe,
                settle_ms=body.settle_ms,
                stop_on_error=body.stop_on_error,
                timeout_ms=body.timeout_ms,
            )
        return artifacts.stow(out)

    # -- dialogs -----------------------------------------------------------

    @app.get("/tabs/{tab_id}/dialog")
    async def get_dialog(tab_id: str) -> dict[str, Any]:
        tab = await manager.ensure_tab(tab_id)
        return {"id": tab.id, "dialog": tab.pending_dialog}

    @app.post("/tabs/{tab_id}/dialog")
    async def post_dialog(tab_id: str, body: DialogRequest) -> dict[str, Any]:
        tab = await manager.ensure_tab(tab_id)
        async with tab.lock:
            return await tab.handle_dialog(accept=body.accept, prompt_text=body.prompt_text)

    # -- events ------------------------------------------------------------

    @app.get("/tabs/{tab_id}/events.json")
    async def events_json(
        tab_id: str,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        types: str | None = Query(default=None),
    ) -> dict[str, Any]:
        tab = await manager.ensure_tab(tab_id)
        wanted = _types(types)
        events, last_seq, dropped = manager.events.since(cursor)
        events = [event for event in events if _matches(event, tab.id, wanted)]
        page = events[:limit]
        next_cursor = int(page[-1]["seq"]) if page else last_seq
        return {
            "cursor": next_cursor,
            "head": manager.events.seq,
            "dropped": dropped,
            "events": [frame(event) for event in page],
            "more": len(events) > len(page) or None,
        }

    @app.get("/tabs/{tab_id}/events")
    async def events_sse(
        tab_id: str,
        cursor: int = Query(default=0, ge=0),
        types: str | None = Query(default=None),
    ) -> EventSourceResponse:
        tab = await manager.ensure_tab(tab_id)
        return EventSourceResponse(
            _event_stream(manager.events, cursor, tab.id, _types(types)),
            ping=15,
        )

    @app.get("/events")
    async def all_events(
        cursor: int = Query(default=0, ge=0),
        types: str | None = Query(default=None),
    ) -> EventSourceResponse:
        return EventSourceResponse(
            _event_stream(manager.events, cursor, None, _types(types)),
            ping=15,
        )

    # -- artifacts ---------------------------------------------------------

    @app.get("/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str) -> Response:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found or expired")
        return Response(
            content=artifact["data"],
            media_type=artifact["type"],
            headers={"cache-control": "no-store"},
        )

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _types(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    values = {part.strip() for part in raw.split(",") if part.strip()}
    return values or None


def _matches(event: Event, tab_id: str | None, types: set[str] | None) -> bool:
    if tab_id is not None and event["data"].get("tabId") != tab_id:
        return False
    return not (types and event["type"] not in types)


async def _event_stream(
    bus: EventBus,
    cursor: int,
    tab_id: str | None,
    types: set[str] | None,
) -> AsyncIterator[dict[str, str]]:
    # Replay and subscribe without an await in between, so no event can slip
    # through the gap.
    events, last_seq, dropped = bus.since(cursor)
    if dropped:
        yield {
            "event": "dropped",
            "data": json.dumps(
                {
                    "requestedCursor": cursor,
                    "oldestAvailable": int(bus.buffer[0]["seq"]) if bus.buffer else None,
                }
            ),
        }
    for event in events:
        if _matches(event, tab_id, types):
            yield _sse(event)

    queue = bus.subscribe()
    try:
        while True:
            event, meta = await queue.get()
            coalesced = bool(meta.get("coalesced"))
            if int(event["seq"]) <= last_seq and not coalesced:
                continue
            if not _matches(event, tab_id, types):
                continue
            last_seq = max(last_seq, int(event["seq"]))
            yield _sse(event, coalesced=coalesced)
    finally:
        bus.unsubscribe(queue)


def _sse(event: Event, *, coalesced: bool = False) -> dict[str, str]:
    return {
        "event": event["type"],
        "id": str(event["seq"]),
        "data": json.dumps(frame(event, coalesced=coalesced), separators=(",", ":")),
    }


async def _gc_loop(artifacts: ArtifactStore) -> None:
    while True:
        await asyncio.sleep(60)
        artifacts.gc()


app = create_app()
