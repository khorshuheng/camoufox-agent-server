---
name: camoufox-browser
description: Drive a real, headed Camoufox browser through the local pi-camoufox HTTP server. Use when asked to browse a site, fill or submit forms, inspect a page, upload files, take screenshots, or handle a captcha/login that needs the user to step in.
compatibility: Needs the pi-camoufox server running (default http://127.0.0.1:8787) and curl or Python httpx.
---

# Camoufox browser

`pi-camoufox` is a local server that owns one persistent, **headed** Camoufox
(Firefox) browser. You drive it over HTTP/SSE. The user can see and use the same
window, which is the point: when a captcha or login appears, ask the user to
solve it instead of fighting it.

Start it if it is not running:

```bash
uv run pi-camoufox                 # headed, port 8787
# or: make start
```

Check readiness first:

```bash
curl -s http://127.0.0.1:8787/health
```

`ok: true` and a non-empty `tabs` means you can act. The response also reports
`browser.headless`, the profile, pending JavaScript dialogs and the event
cursor.

## The loop

1. **Observe** — `GET /tabs/default/snapshot`
2. **Act** — `POST /tabs/default/act` or `/batch` using `ref` handles
3. **Read the deltas** — each result has `events` and, with `observe: auto`, a
   fresh `snapshot`
4. Repeat.

```bash
curl -s localhost:8787/tabs/default/snapshot
```

```json
{
  "url": "https://example.com/signup",
  "title": "Sign up",
  "nodes": [
    {"ref": "e3", "role": "textbox", "name": "Email"},
    {"ref": "e6", "role": "button", "name": "Create account"}
  ]
}
```

Act on the ref, not on guessed CSS:

```bash
curl -s -X POST localhost:8787/tabs/default/batch \
  -H 'content-type: application/json' -d '{
    "commands": [
      {"action": "fill", "ref": "e3", "value": "agent@example.com"},
      {"action": "click", "ref": "e6", "waitForNav": true}
    ],
    "observe": "auto"
  }'
```

Prefer a batch for multi-step work: one round trip, one delta per step. Use
`selector` only when a snapshot has no usable ref, or when you need a locator
that is not exposed as an interesting node.

If an action fails with `code: "not_found"` or `"not_visible"`, the response
already contains a fresh snapshot. Re-target from it; do not blindly retry the
old ref.

## Captcha, login, 2FA: hand off to the user

The browser is headed. When the page shows a challenge the agent should not
try to bypass:

1. **Stop sending actions.**
2. Focus the window:
   ```bash
   curl -s -X POST localhost:8787/tabs/default/focus
   ```
3. Tell the user exactly what to do (solve the captcha, sign in, approve 2FA).
4. Wait. Do **not** reload, navigate, or close the tab while they work.
5. Poll the snapshot or `/health` occasionally:
   ```bash
   curl -s localhost:8787/tabs/default/snapshot
   ```
6. When the challenge is gone, resume the loop. The profile persists, so the
   login survives server restarts.

If the challenge is a JavaScript dialog (`alert`, `confirm`, `prompt`) it blocks
the page. Inspect and handle it explicitly:

```bash
curl -s localhost:8787/tabs/default/dialog
curl -s -X POST localhost:8787/tabs/default/dialog \
  -H 'content-type: application/json' -d '{"accept": true}'
```

Dialogs auto-dismiss after `--dialog-timeout` (30 s by default), so act promptly
if the answer matters. `acceptDialog` is the same operation as an action.

## Useful actions

`navigate`, `click`, `fill`, `type`, `press`, `select`, `check`, `hover`,
`scroll`, `setFiles`, `dropFiles`, `screenshot`, `wait`, `evaluate`, `getText`,
`getHtml`, `snapshot`, `history`, `reload`, `acceptDialog`.

Examples:

```json
{"action": "navigate", "url": "https://example.com", "waitUntil": "load"}
{"action": "fill", "ref": "e3", "value": "hello", "pressEnter": true}
{"action": "select", "ref": "e8", "label": "Singapore"}
{"action": "check", "ref": "e9", "checked": true}
{"action": "wait", "forText": "Welcome", "timeoutMs": 10000}
{"action": "getText", "ref": "e12"}
{"action": "evaluate", "expression": "document.title"}
{"action": "history", "direction": "back"}
```

Uploads take absolute host paths:

```json
{"action": "setFiles", "selector": "#file-input", "files": ["/tmp/receipt.pdf"]}
{"action": "dropFiles", "selector": "#dropzone", "files": ["/tmp/receipt.pdf"]}
```

The server is normally started with main-world evaluation (the default), which
`dropFiles` needs. With `--isolated-eval` it is unavailable.

Screenshots are stowed out-of-band:

```json
{"format": "png", "bytes": 20481, "artifact": "/artifacts/<id>"}
```

Fetch `http://127.0.0.1:8787/artifacts/<id>` to get the bytes.

## Events

Read events from the same cursor the results report:

```bash
curl -s 'localhost:8787/tabs/default/events.json?cursor=0&types=navigation,pageerror,response'
curl -N 'localhost:8787/tabs/default/events?cursor=0'
```

Types: `console`, `pageerror`, `request`, `response`, `requestfailed`,
`navigation`, `load`, `domcontentloaded`, `dialog`, `dialogtimeout`, `download`,
`popup`, `tab`, `tabclosed`, `crash`, `browser`.

Identical console lines and repeated requests coalesce; the frame carries a
`count`. If a resume cursor is older than the buffer, an `event: dropped` frame
tells you there is a gap.

## Tabs

- `GET /tabs` — list; the MRU tab is marked `active`.
- `POST /tabs {"url": "..."}` — open a new tab.
- `DELETE /tabs/:id` — close it.
- `/tabs/default/...` — the most recently used tab, created if none exists.
- Popups and new windows are adopted automatically.

## Pitfalls

- **Do not treat a captcha as a selector problem.** Retrying clicks or reloading
  usually makes the challenge worse. Hand off.
- **Refs go stale** when the DOM changes. The failure response includes a new
  snapshot; use it.
- **Camoufox is Firefox.** Sites that require Chromium-only features may not
  work. Say so instead of silently retrying.
- **`evaluate` runs in the page's main world by default**, so page globals are
  visible. Do not run destructive JS unless the task needs it.
- **One browser, one profile.** `setFiles`/`dropFiles` and `evaluate` are
  powerful; keep the server bound to localhost.
- **HTTP 200 with `ok: false`** is a command-level failure, not a transport
  failure. Read `error.code` and `error.message`.
