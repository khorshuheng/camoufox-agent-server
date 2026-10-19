"""Camoufox lifecycle: profile, stable fingerprint, persistent headed context."""

from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from camoufox import AsyncCamoufox
from camoufox.fingerprints import generate_fingerprint

from .config import ServerOptions, host_os

log = logging.getLogger(__name__)

FINGERPRINT_FILE = "fingerprint.json"
FINGERPRINT_VERSION = 1


class BrowserLaunchError(RuntimeError):
    pass


@dataclass
class BrowserHandle:
    camoufox: AsyncCamoufox
    context: Any
    fingerprint_meta: dict[str, Any]
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.camoufox.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 - the browser may already have exited
            log.debug("browser teardown raised", exc_info=True)


def resolve_profile(profile: str | Path | None) -> Path:
    if profile:
        return Path(profile).expanduser().resolve()
    env = os.environ.get("CAMOUFOX_PROFILE")
    if env:
        return Path(env).expanduser().resolve()
    from .config import DEFAULT_PROFILE

    return DEFAULT_PROFILE


def _fingerprint_path(profile: Path) -> Path:
    return profile / FINGERPRINT_FILE


def load_or_create_fingerprint(
    profile: Path,
    os_name: str | None,
    *,
    rotate: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a stable fpgen fingerprint for this profile.

    Camoufox draws a fresh identity on every launch unless one is supplied, so
    a persistent profile would otherwise keep its cookies but change its
    fingerprint. We draw once and store the exact dict Camoufox generated, then
    reuse it on later launches.
    """
    profile.mkdir(parents=True, exist_ok=True)
    path = _fingerprint_path(profile)
    existing: dict[str, Any] | None = None
    if path.exists() and not rotate:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("ignoring unreadable fingerprint file at %s", path)

    if existing and existing.get("fingerprint"):
        stored_os = existing.get("os")
        if os_name in (None, stored_os):
            return existing["fingerprint"], {
                "file": str(path),
                "os": stored_os,
                "created": existing.get("created"),
                "reused": True,
            }
        log.info("OS changed from %s to %s; drawing a new identity", stored_os, os_name)

    fingerprint = generate_fingerprint(os=os_name)
    document = {
        "version": FINGERPRINT_VERSION,
        "created": datetime.now(timezone.utc).isoformat(),
        "os": os_name,
        "fingerprint": fingerprint,
    }
    _atomic_write_json(path, document)
    return fingerprint, {
        "file": str(path),
        "os": os_name,
        "created": document["created"],
        "reused": False,
    }


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fingerprint-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def display_available() -> bool:
    if platform.system() != "Linux":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


async def launch(options: ServerOptions) -> BrowserHandle:
    """Launch the persistent Camoufox context.

    Headed is the default on purpose: the user needs a real window to solve
    captchas and log in. Failure messages therefore point at the two ways to
    run without a display instead of failing with Playwright's bare error.
    """
    browser = options.browser
    browser.profile.mkdir(parents=True, exist_ok=True)

    if browser.headless is False and not display_available():
        raise BrowserLaunchError(
            "No display detected (DISPLAY/WAYLAND_DISPLAY are unset).\n"
            "  * run on a desktop session, or\n"
            "  * pass --headless for a normal headless browser, or\n"
            "  * pass --virtual-display for a headed browser on a private Xvfb display."
        )

    fingerprint, meta = load_or_create_fingerprint(
        browser.profile, browser.target_os(), rotate=browser.rotate_fingerprint
    )
    kwargs = options.camoufox_kwargs(fingerprint)
    log.info(
        "launching Camoufox (profile=%s, headless=%s, os=%s, fingerprint=%s)",
        browser.profile,
        browser.headless,
        meta.get("os") or host_os(),
        "reused" if meta["reused"] else "new",
    )
    manager = AsyncCamoufox(**kwargs)
    try:
        context = await manager.__aenter__()
    except Exception as error:  # noqa: BLE001 - add actionable context to any launch failure
        message = str(error)
        lowered = message.lower()
        hint = ""
        if (
            "executable doesn't exist" in lowered
            or "not installed" in lowered
            or "no such file" in lowered
        ):
            hint = "\nRun `uv run camoufox fetch` (or `make browser`) to install the browser."
        raise BrowserLaunchError(f"Camoufox failed to launch: {message}{hint}") from error

    return BrowserHandle(camoufox=manager, context=context, fingerprint_meta=meta)
