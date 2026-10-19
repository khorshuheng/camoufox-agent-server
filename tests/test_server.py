from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx


async def act(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post("/tabs/default/act", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def batch(
    client: httpx.AsyncClient, commands: list[dict[str, Any]], **options: Any
) -> dict[str, Any]:
    response = await client.post("/tabs/default/batch", json={"commands": commands, **options})
    assert response.status_code == 200, response.text
    return response.json()


async def snapshot(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/tabs/default/snapshot", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def find(
    snap: dict[str, Any], *, role: str | None = None, name: str | None = None
) -> dict[str, Any] | None:
    for node in snap.get("nodes", []):
        if role and node.get("role") != role:
            continue
        if name and name.lower() not in (node.get("name") or "").lower():
            continue
        return node
    return None


async def wait_for(
    predicate: Any,
    *,
    timeout: float = 5,
    interval: float = 0.05,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval)
    return last


def web(web_server: str, name: str) -> str:
    return f"{web_server}/{name}"


# ---------------------------------------------------------------------------


async def test_health_reports_camoufox(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True, body
    assert body["browser"]["name"] == "camoufox"
    assert body["browser"]["version"]
    assert body["browser"]["headless"] in (True, False, "virtual")
    profile = Path(body["browser"]["profile"])
    assert await asyncio.to_thread(profile.is_dir)
    assert await asyncio.to_thread(Path(profile, "fingerprint.json").is_file)
    assert body["tabs"], "a tab should be available on startup"
    assert body["defaultTab"] == body["tabs"][0]["id"]


async def test_navigate_and_snapshot(client: httpx.AsyncClient, web_server: str) -> None:
    nav = await act(client, action="navigate", url=web(web_server, "form.html"))
    assert nav["ok"] is True, nav
    assert nav["result"]["status"] == 200
    snap = await snapshot(client)
    assert snap["title"] == "Fixture: Form"
    assert find(snap, role="heading", name="Sign up")
    assert find(snap, role="textbox", name="Email")
    assert find(snap, role="combobox", name="Country")
    assert find(snap, role="checkbox", name="Accept terms")
    assert find(snap, role="button", name="Create account")


async def test_batch_deltas(client: httpx.AsyncClient, web_server: str) -> None:
    out = await batch(
        client,
        [
            {"action": "navigate", "url": web(web_server, "form.html")},
            {"action": "fill", "selector": "#email", "value": "agent@example.com"},
            {"action": "select", "selector": "#country", "value": "sg"},
            {"action": "check", "selector": "#tos", "checked": True},
            {"action": "click", "selector": "#submit"},
        ],
        observe="auto",
    )
    assert [result["ok"] for result in out["results"]] == [True] * 5, out
    assert out["results"][1]["result"]["value"] == "agent@example.com"
    assert out["results"][2]["result"]["value"] == "sg"
    assert out["results"][3]["result"]["checked"] is True
    assert "snapshot" in out
    title = await act(client, action="evaluate", expression="document.title")
    assert title["result"]["value"] == "Submitted"


async def test_console_coalescing_and_pageerror(client: httpx.AsyncClient, web_server: str) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))
    boom = await act(client, action="click", selector="#boom")
    assert boom["ok"] is True

    async def logged() -> list[dict[str, Any]] | None:
        response = await client.get("/tabs/default/events.json", params={"types": "console"})
        body = response.json()
        events = [e for e in body["events"] if "repeated log line" in (e.get("text") or "")]
        return events or None

    events = await wait_for(logged)
    assert events, "console output should be captured over Juggler"
    assert any(event.get("count", 1) > 1 for event in events), "identical lines should coalesce"

    async def errored() -> list[dict[str, Any]] | None:
        response = await client.get("/tabs/default/events.json", params={"types": "pageerror"})
        events = response.json()["events"]
        return events or None

    errors = await wait_for(errored)
    assert errors
    assert any("intentional explosion" in (e.get("message") or "") for e in errors)


async def test_network_events(client: httpx.AsyncClient, web_server: str) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))

    async def seen() -> list[dict[str, Any]] | None:
        response = await client.get(
            "/tabs/default/events.json", params={"types": "request,response"}
        )
        events = response.json()["events"]
        return [e for e in events if e.get("url", "").endswith("/form.html")] or None

    events = await wait_for(seen)
    assert events
    assert any(event["type"] == "response" and event.get("status") == 200 for event in events)


async def test_stale_ref_returns_fresh_snapshot(client: httpx.AsyncClient, web_server: str) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))
    snap = await snapshot(client)
    email = find(snap, role="textbox", name="Email")
    assert email
    await act(
        client,
        action="evaluate",
        expression="document.querySelector('#email').remove(); true",
    )
    result = await act(client, action="click", ref=email["ref"], timeoutMs=1200)
    assert result["ok"] is False
    assert result["error"]["code"] in ("not_found", "not_visible")
    assert result.get("snapshot"), "a stale ref should come back with a fresh snapshot"
    assert "stale" in result.get("hint", "")


async def test_screenshot_artifact(client: httpx.AsyncClient, web_server: str) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))
    shot = await act(client, action="screenshot")
    assert shot["ok"] is True, shot
    artifact = shot["result"]["artifact"]
    data = await client.get(artifact)
    assert data.status_code == 200
    assert data.headers["content-type"].startswith("image/png")
    assert data.content.startswith(b"\x89PNG\r\n\x1a\n")


async def test_events_json_and_sse(client: httpx.AsyncClient, web_server: str) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))

    paged = await client.get("/tabs/default/events.json", params={"cursor": 0, "limit": 2})
    body = paged.json()
    assert body["cursor"] >= 0
    assert "events" in body
    assert len(body["events"]) <= 2

    async with client.stream("GET", "/tabs/default/events", params={"cursor": 0}) as stream:
        assert stream.status_code == 200
        event_type = None
        payload = None
        async for line in stream.aiter_lines():
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip())
                break
    assert event_type
    assert payload and payload["type"] == event_type


async def test_set_files_and_drop_files(
    client: httpx.AsyncClient, web_server: str, tmp_path: Path
) -> None:
    receipt = tmp_path / "receipt.txt"
    receipt.write_text("hello upload", encoding="utf-8")
    dropped = tmp_path / "dropped.txt"
    dropped.write_text("hello dropzone", encoding="utf-8")

    await act(client, action="navigate", url=web(web_server, "upload.html"))
    set_result = await act(client, action="setFiles", selector="#file-input", files=[str(receipt)])
    assert set_result["ok"] is True, set_result
    report = await act(client, action="getText", selector="#files-report")
    assert report["result"]["text"] == "receipt.txt"

    reset_result = await act(
        client, action="setFiles", selector="#file-input-reset", files=[str(receipt)]
    )
    assert reset_result["ok"] is True
    reset_report = await act(client, action="getText", selector="#reset-report")
    assert reset_report["result"]["text"] == "receipt.txt"

    drop_result = await act(client, action="dropFiles", selector="#dropzone", files=[str(dropped)])
    assert drop_result["ok"] is True, drop_result
    drop_report = await act(client, action="getText", selector="#drop-report")
    assert drop_report["result"]["text"] == "dropped.txt"


async def test_dialog_is_held_for_the_user(client: httpx.AsyncClient, web_server: str) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))
    await act(client, action="evaluate", expression="setTimeout(() => alert('hello'), 0); true")

    async def pending() -> dict[str, Any] | None:
        response = await client.get("/tabs/default/dialog")
        return response.json().get("dialog")

    dialog = await wait_for(pending, timeout=4)
    assert dialog and dialog["kind"] == "alert" and dialog["message"] == "hello"

    health = (await client.get("/health")).json()
    assert health["pendingDialogs"], "health should surface a held dialog"

    handled = await client.post("/tabs/default/dialog", json={"accept": True})
    assert handled.status_code == 200
    assert handled.json()["handled"] is True

    still_open = (await client.get("/tabs/default/dialog")).json()["dialog"]
    assert still_open is None
    after = await act(client, action="evaluate", expression="1 + 1")
    assert after["result"]["value"] == 2


async def test_history_reload_focus_text_html(client: httpx.AsyncClient, web_server: str) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))
    await act(client, action="navigate", url=web(web_server, "upload.html"))
    back = await act(client, action="history", direction="back")
    assert back["ok"] is True and back["result"]["url"].endswith("form.html")
    forward = await act(client, action="history", direction="forward")
    assert forward["ok"] is True and forward["result"]["url"].endswith("upload.html")
    reloaded = await act(client, action="reload")
    assert reloaded["ok"] is True

    await act(client, action="navigate", url=web(web_server, "form.html"))
    text = await act(client, action="getText", selector="h1")
    assert text["result"]["text"] == "Sign up"
    html = await act(client, action="getHtml", selector="h1")
    assert "Sign up" in html["result"]["html"]
    whole = await act(client, action="getHtml")
    assert "Fixture: Form" in whole["result"]["html"]

    focus = await client.post("/tabs/default/focus")
    assert focus.status_code == 200 and focus.json()["focused"] is True


async def test_evaluate_runs_in_the_page_world(client: httpx.AsyncClient) -> None:
    await act(
        client,
        action="navigate",
        url="data:text/html,<title>Globals</title><script>window.__pi_global = 42;</script>",
    )
    out = await act(client, action="evaluate", expression="window.__pi_global")
    assert out["ok"] is True, out
    assert out["result"]["value"] == 42


async def test_actions_catalog(client: httpx.AsyncClient) -> None:
    response = await client.get("/actions")
    assert response.status_code == 200
    names = {entry["name"] for entry in response.json()["actions"]}
    assert {"navigate", "click", "fill", "dropFiles", "snapshot"} <= names


async def test_new_tab_and_close(client: httpx.AsyncClient, web_server: str) -> None:
    created = await client.post("/tabs", json={"url": web(web_server, "upload.html")})
    assert created.status_code == 201
    body = created.json()
    tab_id = body["id"]
    assert body["url"].endswith("upload.html")

    listed = (await client.get("/tabs")).json()
    assert any(tab["id"] == tab_id for tab in listed["tabs"])

    snap = await client.get(f"/tabs/{tab_id}/snapshot")
    assert snap.status_code == 200
    assert find(snap.json(), role="heading", name="Upload")

    deleted = await client.delete(f"/tabs/{tab_id}")
    assert deleted.status_code == 200 and deleted.json()["closed"] == tab_id
    assert all(tab["id"] != tab_id for tab in (await client.get("/tabs")).json()["tabs"])


async def test_stop_on_error(client: httpx.AsyncClient, web_server: str) -> None:
    commands = [
        {"action": "notARealAction"},
        {"action": "navigate", "url": web(web_server, "form.html")},
    ]
    stopped = await batch(client, commands, observe="never", stopOnError=True)
    assert len(stopped["results"]) == 1
    assert stopped["results"][0]["ok"] is False
    assert "notARealAction" in stopped["results"][0]["error"]["message"]

    continued = await batch(client, commands, observe="never", stopOnError=False)
    assert len(continued["results"]) == 2
    assert continued["results"][1]["ok"] is True


async def test_snapshot_action_and_evaluate_failure(
    client: httpx.AsyncClient, web_server: str
) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))
    out = await batch(
        client,
        [
            {"action": "snapshot", "maxNodes": 5},
            {"action": "evaluate", "expression": "throw new Error('nope')"},
        ],
        observe="never",
    )
    assert out["results"][0]["ok"] is True
    assert out["results"][0]["result"]["nodes"]
    assert out["results"][1]["ok"] is False
    assert out["results"][1]["error"]["code"] == "evaluate_failed"


async def test_artifact_bytes_are_not_inlined(client: httpx.AsyncClient, web_server: str) -> None:
    await act(client, action="navigate", url=web(web_server, "form.html"))
    shot = await act(client, action="screenshot", format="jpeg", quality=40)
    assert shot["ok"] is True
    assert "__artifactData" not in json.dumps(shot)
    assert shot["result"]["format"] == "jpeg"
    artifact = await client.get(shot["result"]["artifact"])
    assert artifact.headers["content-type"] == "image/jpeg"
    assert artifact.content.startswith(b"\xff\xd8")


async def test_invalid_batch_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/tabs/default/batch", json={"commands": "nope"})
    assert response.status_code == 422
    assert "error" in response.json()


async def test_unknown_tab_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/tabs/t999/snapshot")
    assert response.status_code == 404
    assert "unknown tab" in response.json()["error"]
