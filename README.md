# camoufox-agent-server

A local HTTP + SSE server that lets a coding agent drive **Camoufox**, the
stealth Firefox fork, over a small agent-shaped API.

It is built for a **headed** workflow on purpose: the browser window stays
visible, so the user can step in for a captcha, a login, a 2FA prompt, or any
challenge an automation loop should not try to defeat. The agent keeps driving
the same persistent profile afterwards.

What you get:

- **Batched commands** — many actions in one request, one result per action.
- **Deltas, not dumps** — each result carries only what that action changed
  (console, network, navigation, errors), plus a fresh snapshot when the page
  moved.
- **Stable refs** — snapshots return a compact tree of actionable elements with
  `ref` handles, instead of raw HTML.
- **Resumable events** — SSE with per-frame sequence numbers and a cursor, so a
  reconnecting client never misses or duplicates an event.
- **Stale-ref recovery** — a failed ref comes back with a fresh snapshot in the
  same response.
- **Camoufox stealth** — native C++ fingerprint patches, fpgen identity,
  human cursor movement, and a persistent profile whose identity is stable.

The stack is deliberately different from the CDP-based
[`browser-agent-server`](../browser-agent-server): Camoufox is Firefox and
speaks **Juggler**, so this server uses the official Python `camoufox` package
and `playwright` async directly. There is no CDP and no Node launcher.

## Requirements

- Linux or macOS (Windows works for the browser, but the server is tested on
  Linux).
- Python 3.10+ and [uv](https://docs.astral.sh/uv/).
- The Camoufox browser build (one-time download, ~1.3 GB).

## Install

```bash
uv sync
uv run camoufox fetch      # or: make browser
```

If you already installed the `camoufox` CLI with `uv tool install camoufox`,
its browser cache under `~/.cache/camoufox` is shared. You only need one copy
of the browser, and the project and tool use the same Camoufox/Playwright
versions.

## Quick start

```bash
uv run pi-camoufox          # headed, http://127.0.0.1:8787
```

Or use the Makefile:

```bash
make install
make browser
make start
```

`make start` launches a **visible** Camoufox window against a dedicated profile
at `~/.camoufox-agent/profile` (created on first run) and serves the API on
`http://127.0.0.1:8787`.

```bash
make start-headless         # no window
make start-virtual          # Linux: headed browser on a private Xvfb display
```

### Use it from an agent

```bash
# 1. Observe: a compact tree with refs
curl -s localhost:8787/tabs/default/snapshot
# {"url": "...", "title": "...",
#  "nodes": [{"ref": "e1", "role": "link", "name": "Docs", "href": "/docs"}]}

# 2. Act in bulk: one round trip, per-step deltas
curl -s -X POST localhost:8787/tabs/default/batch \
  -H 'content-type: application/json' -d '{
    "commands": [
      {"action": "navigate", "url": "https://example.com"},
      {"action": "fill", "ref": "e3", "value": "agent@example.com"},
      {"action": "click", "ref": "e6", "waitForNav": true}
    ],
    "observe": "auto"
  }'
```

Each result looks like:

```json
{
  "i": 2, "action": "click", "ok": true, "ms": 87,
  "result": { "clicked": true, "at": { "x": 506, "y": 92 } },
  "events": {
    "count": 3,
    "byType": { "response": 1, "pageerror": 1, "navigation": 1 },
    "highlights": ["[pageerror] Error: intentional explosion"]
  }
}
```

`observe` controls the trailing snapshot: `"auto"` (only after a page-changing
step), `"always"`, `"never"`, or `"full"` (also include every raw event).

### Linux and no display

Headed is the default. Without `DISPLAY`/`WAYLAND_DISPLAY` the CLI refuses to
start and tells you to use one of:

```bash
uv run pi-camoufox --headless
uv run pi-camoufox --virtual-display    # Xvfb; requires Xvfb + setpriv
```

`--virtual-display` is a **headed** browser on a private display, which is
still useful for rendering-sensitive sites but leaves no window on your
desktop. The user cannot intervene there.

## The captcha / login workflow

This is the reason the server is headed:

1. The agent notices a challenge in a snapshot (captcha, login wall, 2FA,
   "verify you are human", cookie consent that blocks the page).
2. The agent **stops issuing actions** and calls:
   ```bash
   curl -s -X POST localhost:8787/tabs/default/focus
   ```
3. The agent asks the user to solve it in the visible Camoufox window.
4. The user solves it — clicks, types, opens a password manager, whatever.
5. The agent polls `GET /tabs/default/snapshot` (or `/health`) until the
   challenge is gone, then resumes the loop.

Do not reload, navigate, or close the tab while the user is working. The
profile is persistent, so a login survives restarts.

JavaScript dialogs are handled separately: they block the page until accepted
or dismissed. The server holds the dialog open and exposes it:

```bash
curl -s localhost:8787/tabs/default/dialog
# {"id":"t1","dialog":{"kind":"alert","message":"hello",...}}
curl -s -X POST localhost:8787/tabs/default/dialog \
  -H 'content-type: application/json' -d '{"accept": true}'
```

A pending dialog also appears in `GET /health` under `pendingDialogs`, and can
be handled with the `acceptDialog` action. To avoid a permanently frozen page,
dialogs are auto-dismissed after `--dialog-timeout` (default 30 s, `0` = never).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Route index |
| `GET` | `/health` | Browser version, profile, tabs, pending dialogs, cursor (`?challenges=1` also checks each tab) |
| `GET` | `/actions` | Action vocabulary |
| `GET` | `/tabs` | List tabs (most-recently-used marked `active`) |
| `POST` | `/tabs` | Open a tab `{"url": "..."}` |
| `DELETE` | `/tabs/:id` | Close a tab |
| `POST` | `/tabs/:id/focus` | Raise the headed window |
| `GET` | `/tabs/:id/snapshot` | Compact snapshot (`?maxNodes=&scope=&includeText=`) |
| `GET` | `/tabs/:id/challenge` | Heuristic challenge/captcha detection |
| `POST` | `/tabs/:id/act` | Run one action |
| `POST` | `/tabs/:id/batch` | **Run N actions → N deltas** (hot path) |
| `GET` | `/tabs/:id/dialog` | Pending JavaScript dialog, if any |
| `POST` | `/tabs/:id/dialog` | Accept/dismiss it |
| `GET` | `/tabs/:id/events` | SSE stream since `?cursor=` |
| `GET` | `/tabs/:id/events.json` | Poll this tab's events since `?cursor=&limit=&types=` |
| `GET` | `/events` | SSE stream across tabs |
| `GET` | `/artifacts/:id` | Fetch stowed bytes (screenshots) |

Tab ids are stable and human-sized (`t1`, `t2`, ...).
`/tabs/default/...` targets the most recently used tab, creating one if none is
open. New tabs (including popups) are adopted automatically.

### Snapshots

```json
{
  "url": "https://example.com/signup",
  "title": "Sign up",
  "scroll": {"y": 0, "maxY": 1240},
  "counts": {"heading": 1, "textbox": 2, "button": 1},
  "nodes": [
    {"ref": "e3", "role": "textbox", "name": "Email"},
    {"ref": "e6", "role": "button", "name": "Create account"}
  ],
  "truncated": true
}
```

Refs are stored on the element as `data-cf-ref`. They are handles, not CSS:
act on them with `{"ref": "e6"}`. If the page changed and an element is gone,
the action fails with `code: "not_found"` and the response includes a fresh
snapshot so the agent can re-target in the same turn. Open shadow roots are
included in snapshots and in ref-based actions.

### Batch response

```json
{
  "cursor": 42,
  "results": [
    {"i": 0, "action": "navigate", "ok": true, "ms": 112, "result": {...}, "events": {...}}
  ],
  "snapshot": {...}
}
```

A command-level failure is still HTTP 200; it is data, not a transport error.
Malformed requests, unknown tabs and invalid bodies use 4xx.

## Actions

| Action | What it does |
| --- | --- |
| `navigate` | Go to a URL (`waitUntil`: `commit`, `domcontentloaded`, `load`, `networkidle`, `none`); `waitFor`/`waitForText`/`waitForUrl` | 
| `click` | Click a ref/selector (`button`, `clickCount`, `waitForNav`, `force`) |
| `fill` | Focus and replace a field (`value`, `clear`, `pressEnter`, `delay`) |
| `type` | Type without clearing (`text`, `delay`) |
| `press` | Press a key (`Enter`, `Tab`, `Control+a`, ...) |
| `select` | Choose an option by `value`, `label`, or `index` |
| `hover` | Move the pointer over an element |
| `scroll` | Scroll to a ref/selector, or by `dx`/`dy`; returns the new position |
| `check` | Set a checkbox/radio (`checked: true/false`) and verify it |
| `setFiles` | Set files on an `<input type="file">` |
| `dropFiles` | Drop files on a dropzone via dragenter/dragover/drop |
| `screenshot` | Capture PNG/JPEG; returns an artifact handle |
| `wait` | Wait for `ms`, `forSelector` (+`minCount`/`state`), `forText`, `forUrl`, `forFunction`, or `forNetworkIdle` |
| `evaluate` | Evaluate a JS expression in the page |
| `getText` | Read `innerText` from a ref/selector |
| `getHtml` | Read `innerHTML` (or the whole page with no target) |
| `snapshot` | Return a snapshot as the action result |
| `extract` | Extract compact rows from repeated elements with a selector + field map |
| `closeOverlays` | Close cookie/login/dialog overlays (`acceptCookies`, `labels`, `max`) |
| `clickAt` | Click viewport coordinates (`x`, `y`); useful for canvas and iframe widgets |
| `history` | `direction: "back"` or `"forward"` |
| `reload` | Reload the page |
| `acceptDialog` | Accept/dismiss the pending JS dialog |

Actions accept either a `ref` from a snapshot or a CSS `selector`. Playwright
dispatches input through Juggler, so Camoufox sees native input handling.
Add `"frame": "#iframe-selector"` (or a list for nested frames) to target a
control inside an iframe. `clickAt` is the coordinate fallback for canvas and
cross-origin widgets. `dropFiles` is main-document only; use `setFiles` for a
file input inside an iframe.

`extract` is the fast path for list pages:

```json
{"action":"extract","selector":"[data-testid=property-card]",
 "fields":{"title":"[data-testid=title]",
           "price":"[data-testid=price-and-discounted-price]",
           "link":{"selector":"a","attr":"href"}},
 "limit":30}
```

A field is a selector string, or an object with `selector` plus `attr`, `html`,
`value`, or `all`. `includeText` adds the row's collapsed text. `scope`
restricts the row search, `timeoutMs` optionally waits for rows, and `required`
turns an empty result into a `not_found` error.

`closeOverlays` only clicks controls inside dialog/cookie/modal-looking
containers. It does not accept cookies unless `acceptCookies: true`. If a click
is covered, it fails immediately with `code: "blocked"` and a `blocker`
description rather than waiting out the action timeout.

`setFiles` and `dropFiles` accept absolute host paths. `dropFiles` builds real
`File` objects in the page's main world, which is why it needs main-world
evaluation (the default).

Screenshots are stored out-of-band: the JSON contains
`{"artifact": "/artifacts/<id>", "format": "png", "bytes": 12345}` instead of
hundreds of KB of base64.

## Events

Event types: `console`, `pageerror`, `request`, `response`, `requestfailed`,
`navigation`, `load`, `domcontentloaded`, `dialog`, `dialogtimeout`, `download`,
`popup`, `tab`, `tabclosed`, `crash`, `browser`.

Per-command deltas are scoped to the current tab and navigation epoch: tracker
failures from the previous document and messages from browser extensions
(`moz-extension://`, `chrome-extension://`) are filtered out.

### Challenge detection

```bash
curl -s localhost:8787/tabs/default/challenge
# {"id":"t1","url":"...","challenge":{"detected":true,"kind":"cloudflare",...}}
curl -s 'localhost:8787/health?challenges=1'
```

The detector is heuristic (title/body phrases for Cloudflare, captcha and access
screens). When it fires, stop acting, call `/focus`, ask the user to solve it,
and poll the snapshot/health afterwards.

```bash
curl -N 'localhost:8787/tabs/default/events?cursor=0'
```

Frames are `event: <type>` with `id: <seq>`. Reconnect with
`?cursor=<lastSeq>` to resume. If the cursor predates the retained buffer, the
stream emits an `event: dropped` frame instead of silently skipping. Filter by
type with `?types=navigation,pageerror,response`.

High-frequency identical events (console lines, repeated requests) coalesce
within a 400 ms window; the frame carries a `count`.

Prefer polling? `GET /tabs/:id/events.json?cursor=…&limit=…` returns a page of
that tab's events plus the next cursor and a `more` flag.

## Profile and identity

- Default profile: `~/.camoufox-agent/profile` (`--profile`, `CAMOUFOX_PROFILE`).
- It is a real persistent Firefox profile: cookies, logins, local storage,
  history and `about:config` state survive restarts.
- A fingerprint is drawn once and stored in `fingerprint.json` beside the
  profile. Camoufox would otherwise draw a new identity on every launch, which
  makes a persistent profile incoherent. Override the target OS with `--os`
  (default: the host OS); changing it draws a new identity. `--rotate-fingerprint`
  forces a new one.
- The fingerprint is a normal fpgen fingerprint generated by Camoufox itself.

### Keeping the profile consistent

Use the same `--profile` across runs; that is the whole point. A stable profile
keeps cookies, logins, history, and the exact identity (UA, screen, WebGL,
canvas, audio and font metrics stay put across restarts). `/health` reports
`browser.fingerprint.reused: true` from the second launch onward.

A brand-new profile still looks brand-new. Warm it with ordinary browsing
before putting it to work:

```bash
make warm        # or: uv run python scripts/warm_profile.py http://127.0.0.1:8787
```

The helper visits Google, Wikipedia, GitHub, YouTube, Reddit, Hacker News,
Stack Overflow and BBC News, and accepts an obvious cookie-consent screen. It
does not log in anywhere. Pass `--site URL` (repeatable) to choose your own.

One server per profile: the profile contains a `server.pid` lock. A second
server pointed at the same profile refuses to start with an actionable message
instead of corrupting the profile. If a server was killed hard, the next start
sees a dead pid and reclaims the lock; a recycled pid is checked against the
process command line.

Do not change `--os` on an established profile unless you intend to rotate the
identity, and do not run `--rotate-fingerprint` casually: both make the profile
look like a different machine.

## Stealth notes

Camoufox spoofs values in C++, so `navigator`, headers, fonts, WebGL, audio and
canvas agree without page-world JavaScript overrides. Defaults here:

- headed (`--headless` disables),
- human cursor movement (on by default; `--no-humanize` is ~5x faster for
  mouse-driven clicks, which matters in long agent loops),
- host-OS identity (not a random OS mixture),
- a persistent profile and stable fingerprint,
- uBlock Origin, enabled by Camoufox by default.

**Main-world evaluation is enabled by default.** Camoufox's isolated world is a
stealth feature, but it hides page globals from `evaluate` and makes page-owned
objects (notably a `DataTransfer` with files) invisible to the page's own event
listeners. Agents need both. If maximum isolation matters more, pass
`--isolated-eval`; `evaluate` then cannot see page globals and `dropFiles` is
unavailable.

No browser is undetectable. Camoufox presents Firefox; sites that require
Chromium features will behave differently, and anti-bot vendors still look for
inconsistencies.

## CLI

```
pi-camoufox [options]

  --http-port <n>       HTTP port (default 8787)
  --host <addr>         Bind address (default 127.0.0.1)
  --headless            No window
  --virtual-display     Linux: headed on a private Xvfb display
  --profile <dir>       Profile directory (default ~/.camoufox-agent/profile)
  --os <name>           host|linux|macos|windows|random (default host)
  --locale <tags>       Comma-separated locale(s), e.g. en-US,fr-FR
  --geoip               Derive timezone/locale from the proxy exit IP
  --proxy <url>         http://user:pass@host:8080
  --no-humanize         Disable human cursor movement
  --block-images / --block-webrtc / --block-webgl
  --disable-coop        Allow clicking cross-origin iframe widgets (e.g. Turnstile)
  --enable-cache
  --isolated-eval       Run page JS in Camoufox's isolated world
  --executable <path>   Custom Camoufox binary
  --rotate-fingerprint  Draw and store a new identity
  --arg <flag>          Extra browser argument (repeatable)
  --settle-ms <n>       Default settle budget after mutating actions (250)
  --dialog-timeout <ms> Auto-dismiss JS dialogs after this (30000; 0 = never)
  --debug               Include stack traces in error responses
  --version
```

Environment: `HTTP_PORT`, `HOST`, `CAMOUFOX_PROFILE`, `CAMOUFOX_OS`,
`CAMOUFOX_HEADLESS=1`.

## Make targets

`make` / `make help` lists everything.

| Target | Purpose |
| --- | --- |
| `install` / `deps` | `uv sync` |
| `browser` | Download the Camoufox build |
| `doctor` | Show uv, Camoufox, Playwright and browser state |
| `start` / `start-headless` / `start-virtual` | Run the server |
| `warm` | Visit common sites to seed profile cookies/history |
| `demo` | Run the example agent loop |
| `lint` / `format` / `format-check` / `check` | Ruff and syntax checks |
| `test` / `test-headed` | Hermetic browser tests |
| `ci` | install + check + test |
| `clean` / `clean-profile` | Housekeeping |

Override variables, e.g. `make start HTTP_PORT=8790 PROFILE=/tmp/p`.

## Testing

```bash
uv run pytest                 # headless
SMOKE_HEADED=1 uv run pytest  # headed (opens a window)
```

The suite launches a real Camoufox with a temporary profile, starts the server
in-process, and drives it against local HTTP fixtures — no network required. It
covers snapshots and refs, batched fill/select/check/click, console coalescing,
page errors, network events, stale-ref recovery, screenshots and artifacts,
event paging and SSE, dialogs, `setFiles`, `dropFiles`, history/reload, tab
lifecycle, and stop-on-error.

## Layout

```
src/camoufox_agent_server/
  browser.py     Camoufox launch, profile, persisted fingerprint
  manager.py     context and tab lifecycle, MRU tab, health
  session.py     one page: Playwright event wiring, evaluation, dialogs
  observe.py     in-page snapshot JS (refs, roles, names)
  actions.py     Playwright action vocabulary
  executor.py    batch runner, per-command deltas, settling
  events.py      bounded coalescing event bus + summarizer
  server.py      FastAPI routes, SSE, artifacts
  challenge.py   heuristic captcha/challenge detection
  cli.py         pi-camoufox entry point
tests/           pytest suite + local fixtures
examples/        agent loop example
scripts/         warm_profile.py (seed a new profile)
skills/          pi skill for this server
```

## Security

The API grants full control of a real browser profile. It binds to
`127.0.0.1` and ships no authentication. Do not expose it to a network.
`setFiles`/`dropFiles` accept arbitrary local paths, and `evaluate` runs
arbitrary JavaScript in the page.

## Known limitations

- **One browser process per server.** The context lives in-process; do not run
  multiple uvicorn workers.
- **No attach mode.** Playwright owns the browser it launched. To reuse a
  running profile, start a server with that `--profile`.
- **Firefox, not Chromium.** Chromium-only features will differ.
- **Action timeouts cancel the waiter, not necessarily the browser-side work.**
- **Coalescing is lossy at cursor boundaries**, as with any merged stream.
- **`--virtual-display` needs Xvfb and `setpriv`**, and has no GPU.
- **Headless is less useful for this project** by design; use it for tests or
  unattended work, not captcha flows.
