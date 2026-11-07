from __future__ import annotations

import asyncio
import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from camoufox_agent_server.config import BrowserOptions, ServerOptions
from camoufox_agent_server.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def web_server() -> str:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


@pytest.fixture(scope="session")
def app(tmp_path_factory: pytest.TempPathFactory) -> Any:
    profile = tmp_path_factory.mktemp("camoufox") / "profile"
    headed = os.environ.get("SMOKE_HEADED") == "1"
    options = ServerOptions(
        browser=BrowserOptions(profile=profile, headless=not headed, os="linux"),
        settle_ms=60,
        dialog_timeout_ms=2_000,
    )
    return create_app(options)


@pytest.fixture(scope="session")
async def server_url(app: Any) -> str:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        loop="asyncio",
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(2400):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("server did not start within 120s")
        sockets = server.servers[0].sockets
        port = sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=30)
        except asyncio.TimeoutError:
            task.cancel()


@pytest.fixture
async def client(server_url: str) -> httpx.AsyncClient:
    async with httpx.AsyncClient(base_url=server_url, timeout=60) as http:
        yield http
