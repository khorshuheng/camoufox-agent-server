"""CLI: ``pi-camoufox [options]``."""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import os
import sys
from urllib.parse import unquote, urlparse

import uvicorn

from .browser import display_available, resolve_profile
from .config import (
    DEFAULT_DIALOG_TIMEOUT_MS,
    DEFAULT_HTTP_PORT,
    DEFAULT_SETTLE_MS,
    BrowserOptions,
    ServerOptions,
    host_os,
)
from .server import create_app

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-camoufox",
        description=(
            "Headed Camoufox control server for coding agents. "
            "The browser window stays visible so a user can solve captchas and log in."
        ),
    )
    parser.add_argument(
        "--http-port", type=int, default=int(os.environ.get("HTTP_PORT", DEFAULT_HTTP_PORT))
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without a window (user cannot intervene)",
    )
    parser.add_argument(
        "--virtual-display",
        action="store_true",
        help='Linux: headed browser on a private Xvfb display (Camoufox headless="virtual")',
    )
    parser.add_argument("--profile", default=os.environ.get("CAMOUFOX_PROFILE"))
    parser.add_argument(
        "--os",
        default=os.environ.get("CAMOUFOX_OS", "host"),
        choices=["host", "linux", "macos", "windows", "random"],
        help="OS to claim in the fingerprint (default: the host OS)",
    )
    parser.add_argument("--locale", default=None, help="locale(s), e.g. en-US or en-US,fr-FR")
    parser.add_argument(
        "--geoip", action="store_true", help="derive timezone/locale from the proxy exit IP"
    )
    parser.add_argument("--proxy", default=None, help="proxy URL, e.g. http://user:pass@host:8080")
    parser.add_argument(
        "--no-humanize", action="store_true", help="disable Camoufox human cursor movement"
    )
    parser.add_argument("--block-images", action="store_true")
    parser.add_argument("--block-webrtc", action="store_true")
    parser.add_argument("--block-webgl", action="store_true")
    parser.add_argument(
        "--disable-coop",
        action="store_true",
        help="disable COOP so cross-origin iframe widgets (e.g. Turnstile) can be clicked",
    )
    parser.add_argument("--enable-cache", action="store_true")
    parser.add_argument(
        "--isolated-eval",
        action="store_true",
        help=(
            "run page JS in Camoufox's isolated world (maximum stealth; "
            "page globals are invisible and dropFiles is unavailable)"
        ),
    )
    parser.add_argument("--executable", default=None, help="path to a Camoufox binary")
    parser.add_argument(
        "--rotate-fingerprint",
        action="store_true",
        help="draw and store a new identity for this profile",
    )
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="FLAG",
        help="extra browser argument (repeatable)",
    )
    parser.add_argument("--settle-ms", type=int, default=DEFAULT_SETTLE_MS)
    parser.add_argument(
        "--dialog-timeout",
        type=int,
        default=DEFAULT_DIALOG_TIMEOUT_MS,
        metavar="MS",
        help="auto-dismiss a JS dialog after MS (0 = never; default 30000)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="include stack traces in error responses"
    )
    parser.add_argument("--version", action="store_true", help="print the package version")
    return parser


def parse_proxy(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise SystemExit(f"invalid proxy URL: {raw!r}")
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    proxy = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def options_from_args(args: argparse.Namespace) -> ServerOptions:
    if args.headless and args.virtual_display:
        raise SystemExit("--headless and --virtual-display are mutually exclusive")
    headless: bool | str
    if args.virtual_display:
        headless = "virtual"
    else:
        headless = bool(args.headless or os.environ.get("CAMOUFOX_HEADLESS") == "1")

    if args.os == "host":
        target_os = host_os()
    elif args.os == "random":
        target_os = None
    else:
        target_os = args.os

    locale: str | list[str] | None = None
    if args.locale:
        parts = [part.strip() for part in args.locale.split(",") if part.strip()]
        locale = parts[0] if len(parts) == 1 else parts

    browser = BrowserOptions(
        profile=resolve_profile(args.profile),
        headless=headless,
        os=target_os,
        humanize=not args.no_humanize,
        geoip=bool(args.geoip),
        proxy=parse_proxy(args.proxy),
        locale=locale,
        block_images=args.block_images,
        block_webrtc=args.block_webrtc,
        block_webgl=args.block_webgl,
        disable_coop=args.disable_coop,
        enable_cache=args.enable_cache,
        main_world_eval=not args.isolated_eval,
        executable=args.executable,
        rotate_fingerprint=args.rotate_fingerprint,
        extra_args=list(args.arg),
    )
    return ServerOptions(
        browser=browser,
        host=args.host,
        http_port=args.http_port,
        settle_ms=args.settle_ms,
        dialog_timeout_ms=args.dialog_timeout,
        debug=args.debug,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        try:
            print(importlib.metadata.version("camoufox-agent-server"))
        except importlib.metadata.PackageNotFoundError:
            print("0.1.0")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    options = options_from_args(args)
    if options.browser.headless is False and not display_available():
        print(
            "no display detected (DISPLAY/WAYLAND_DISPLAY unset).\n"
            "  * run from a desktop session, or\n"
            "  * pass --headless, or\n"
            "  * pass --virtual-display (Linux).",
            file=sys.stderr,
        )
        return 1
    app = create_app(options)

    print(f"pi-camoufox listening on http://{options.host}:{options.http_port}")
    print(f"profile: {options.browser.profile}")
    mode = "headless" if options.browser.headless is True else options.browser.headless
    print(f"mode: {mode or 'headed'}")
    if options.browser.headless is False:
        print("the browser window is visible; the user can take over for captchas and logins")
    sys.stdout.flush()

    uvicorn.run(
        app,
        host=options.host,
        port=options.http_port,
        loop="asyncio",
        workers=1,
        log_level="debug" if options.debug else "info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
