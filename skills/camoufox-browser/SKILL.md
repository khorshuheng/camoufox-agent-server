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

Start it if it is not running. It needs ~10 s to become ready, so start it in
the background and poll instead of blocking or guessing:

```bash
cd /path/to/camoufox-agent-server
(nohup uv run pi-camoufox > /tmp/pi-camoufox.log 2>&1 &)   # or foreground: make start
for _ in $(seq 1 40); do curl -sf localhost:8787/health | grep -q '"ok":true' && break; sleep 1; done
```

If it refuses to start with `profile ... is already in use by pi-camoufox (pid N)`,
an earlier server still holds the profile. Restart with `--replace` instead of
hunting for the pid:

```bash
uv run pi-camoufox --replace
```

Check readiness with:

```bash
curl -s http://127.0.0.1:8787/health
```

`ok: true` and a non-empty `tabs` means you can act. The response also reports
`browser.headless`, `browser.connected`, the profile, pending JavaScript
dialogs and the event cursor. `/health` is also the recovery entry point: it
probes the browser and relaunches a dead one, reporting `recovered: true` when
it had to.

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

### List pages: extract, don't snapshot

For search results, tables and feeds, `extract` is much faster and cheaper than
a snapshot:
```bash
curl -s -X POST localhost:8787/tabs/default/batch   -H 'content-type: application/json' -d '{
    "commands": [
      {"action":"navigate","url":"https://example.com/search?q=...","waitUntil":"domcontentloaded"},
      {"action":"wait","forSelector":".result-card","minCount":5,"timeoutMs":15000},
      {"action":"extract","selector":".result-card","limit":20,
       "fields":{"title":".title","price":".price",
                 "link":{"selector":"a","attr":"href"},
                 "tags":{"selector":".tag","all":true}}}
    ],
    "observe":"never"
  }'
```

A field is a selector string or an object with `selector` plus `attr`, `html`,
`value`, or `all`. Add `includeText: true` for the row's collapsed text and
`scope` to restrict the search. `required: true` turns an empty result into a
`not_found` error.

### SPA data: read the page's own state

When a single-page app ships its data in a global (YouTube, many dashboards,
any server-rendered-then-hydrated list), reading that global beats scraping the
rendered DOM: one round trip, no refs, no lazy-loading surprises.

```json
{"action":"evaluate","expression":"Object.keys(window).filter(k => k.startsWith('ytInitial'))"}
{"action":"evaluate","expression":"window.ytInitialData.contents.twoColumnSearchResultsRenderer.primaryContents.sectionListRenderer.contents[0].itemSectionRenderer.contents.slice(0,10).map(c => c.videoRenderer && ({id: c.videoRenderer.videoId, title: c.videoRenderer.title.runs[0].text}))"}
{"action":"evaluate","expression":"window.ytInitialPlayerResponse?.videoDetails"}
```

`evaluate` runs in the page's main world by default, so page globals are
visible. With `--isolated-eval` they are not — fall back to `extract` then.
Return only the fields you need; a raw `window.ytInitialData` is megabytes.

### Overlays and blocked clicks

Run `closeOverlays` before interacting with a page that may show a cookie or
login modal. Run it *after* the first navigation — on a blank tab it is a
no-op:

```json
{"action":"closeOverlays"}
{"action":"closeOverlays","acceptCookies":true}
```

It only clicks controls inside dialog/cookie/modal-looking containers, so it
will not randomly accept or dismiss page content. If an action fails with
`code:"blocked"`, the response includes the covering element and a hint. Close
the overlay, or use `{"force": true}` on `click` when you know the target is
correct. For canvas or cross-origin widget iframes, use
`{"action":"clickAt","x":100,"y":60}`.

### Waits

Prefer event-driven waits over fixed sleeps:

```json
{"action":"wait","forSelector":".result-card","minCount":5,"timeoutMs":15000}
{"action":"wait","forText":"Showing results","timeoutMs":10000}
{"action":"wait","forUrl":"/search","timeoutMs":10000}
{"action":"wait","forFunction":"() => document.readyState === 'complete'"}
{"action":"navigate","url":"...","waitUntil":"domcontentloaded","waitFor":".result-card"}
```

`navigate` accepts `waitFor`, `waitForText` and `waitForUrl`, so a navigation
plus its first meaningful element can be one command. If a wait misses, it
returns `waitForTimedOut` instead of discarding a successful navigation.

If an action fails with `code: "not_found"` or `"not_visible"`, the response
already contains a fresh snapshot. Re-target from it; do not blindly retry the
old ref. A `blocked` failure means an overlay is covering the target.

Iframes are supported per action with `"frame": "#iframe-selector"` (a list for
nested frames). Open shadow roots are included in snapshots and work with refs.

## When the browser dies

The user can close the window, and a crashed browser takes its context with it.
Every failure carries a `code`; the one that matters here is:

```json
{"error":"BrowserContext.new_page: Target page, context or browser has been closed",
 "code":"browser_closed","retryable":true,
 "hint":"POST /browser/restart to relaunch the browser, then retry"}
```

Rules:

1. Do **not** retry selectors, reload, or re-target refs — the page is gone.
2. Recover with either `POST /health` (probes and relaunches, no-op if healthy)
   or an explicit `POST /browser/restart`.
3. Re-observe from scratch. Tabs, refs and in-page state are gone; the profile
   (cookies, logins, fingerprint) survives, so you are still signed in.
4. Inside a batch, a mid-flight browser death is reported as a per-command
   `error.code: "browser_closed"`. With the default `stopOnError: false` the
   remaining commands fail the same way; with `stopOnError: true` they are
   skipped. Either way, treat every command after the first failure as not
   applied: restart and resend the whole batch.
5. A request that only needed a tab may recover on its own — the server
   relaunches automatically. Expect tabs to be reset to `about:blank` and
   re-navigate rather than assuming the old page is still live.

A stale server that holds the profile lock while its browser is dead is the
same failure one level up: restart it with `uv run pi-camoufox --replace`.

## Reading responses

`/act` returns one command's outcome; `/batch` returns the same shape per
command:

```json
{"cursor":123,"results":[{"i":0,"action":"navigate","ok":true,"ms":412,
  "result":{},"events":{"count":12,"byType":{},"highlights":[]}}],
 "snapshot":{"url":"...","nodes":[]}}
```

So `.results[]` is the list, never the top level. Keep the transcript small:

```bash
curl -s -X POST localhost:8787/tabs/default/batch -H 'content-type: application/json' \
  -d '{"commands":[...],"observe":"never","events":"none"}' \
  | jq -c '{ok: ([.results[]?.ok] | all), r: [.results[]? | {i, action, ok, ms, error}]}'
```

`events` accepts `none`, `summary` (default) or `full`; `observe` accepts
`auto`, `always`, `never`, `full`. Highlights are clipped to ~200 characters,
because tracker URLs and stack traces are otherwise large enough to matter.
Use `"events":"none"` for extract-and-continue loops, and `"observe":"never"`
unless you need the new tree.

If a call fails, the body is `{error, code, retryable?, hint?}` — never parse it
as a batch result.

## Speed

For bulk reading (search pages, listings, tables) start the server with
`--fast`: it enables the HTTP cache, lightens humanization and shortens the
per-action settle window. Add `--block-images` only when you do not need
screenshots.

```bash
(nohup uv run pi-camoufox --fast > /tmp/pi-camoufox.log 2>&1 &)
```

Prefer one batch over several `act` calls, `extract` over `snapshot`, and a
`wait` with `forSelector`/`forText` over a fixed sleep.

## Captcha, login, 2FA: hand off to the user

Check for a challenge after navigation if the page looks wrong:

```bash
curl -s localhost:8787/tabs/default/challenge
```

`challenge.detected` is heuristic but covers the common Cloudflare, captcha and
access-denied shells. `/health?challenges=1` checks every tab.

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

## Profile consistency

The server uses a persistent profile by default (`~/.camoufox-agent/profile`):
cookies, logins, history and the fingerprint survive restarts. Keep using the
same `--profile`; a new profile looks like a new machine and will be challenged
more often.

If a profile is fresh, warm it before real work:

```bash
make warm
# or: uv run python scripts/warm_profile.py http://127.0.0.1:8787
```

`/health` reports `browser.fingerprint.reused: true` after the first launch.
Only one server can use a profile at a time; a second one refuses to start
(pid lock in the profile). Do not run `--rotate-fingerprint` or change `--os`
unless a new identity is intended.

## Prices and listings

On booking/OTA pages, the headline price is often the cheapest restricted rate.
After extracting the list, open the top 2–3 property pages and compare:

- total for the stay vs per-night
- taxes and charges included or not
- free cancellation deadline vs non-refundable/prepay
- room size and bed type
- whether the listing is marked “Featured” (paid placement)

## Tabs

- `GET /tabs` — list; the MRU tab is marked `active`.
- `POST /tabs {"url": "..."}` — open a new tab.
- `DELETE /tabs/:id` — close it.
- `/tabs/default/...` — the most recently used tab, created if none exists.
- Popups and new windows are adopted automatically.

## Pitfalls

- **Do not treat a captcha as a selector problem.** Retrying clicks or reloading
  usually makes the challenge worse. Hand off.
- **Do not treat `browser_closed` as a selector problem either.** Restart the
  browser; a dead context never recovers by itself.
- **Refs go stale** when the DOM changes. The failure response includes a new
  snapshot; use it.
- **Camoufox is Firefox.** Sites that require Chromium-only features may not
  work. Say so instead of silently retrying.
- **`evaluate` runs in the page's main world by default**, so page globals are
  visible. Do not run destructive JS unless the task needs it.
- **One browser, one profile.** `setFiles`/`dropFiles` and `evaluate` are
  powerful; keep the server bound to localhost.
- **HTTP 200 with `ok: false`** is a command-level failure, not a transport
  failure. Read `error.code`, then `error.message`.
- **An error body is not a batch result.** `POST` failures return
  `{error, code, retryable?, hint?}` at the top level; there is no `.results`.
- **`--replace` beats pid archaeology.** A server that holds the profile while
  its browser is dead blocks startup and looks alive to `/health` only if it is
  from an older build — restart it with `--replace`.
