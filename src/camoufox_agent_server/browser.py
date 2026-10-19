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
LOCK_FILE = "server.pid"
FINGERPRINT_VERSION = 1


class BrowserLaunchError(RuntimeError):
    pass


@dataclass
class BrowserHandle:
    camoufox: AsyncCamoufox
    context: Any
    fingerprint_meta: dict[str, Any]
    lock_file: Path | None = None
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.camoufox.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 - the browser may already have exited
            log.debug("browser teardown raised", exc_info=True)
        finally:
            if self.lock_file is not None:
                release_profile_lock(self.lock_file)


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


def lock_file_for(profile: Path) -> Path:
    return profile / LOCK_FILE


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _looks_like_our_server(pid: int) -> bool:
    """Avoid treating a recycled PID as another pi-camoufox instance."""
    if platform.system() == "Linux":
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        command = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        return any(
            token in command
            for token in ("pi-camoufox", "camoufox_agent_server", "camoufox-agent-server")
        )
    if platform.system() == "Darwin":
        import subprocess

        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return any(
            token in result.stdout
            for token in ("pi-camoufox", "camoufox_agent_server", "camoufox-agent-server")
        )
    return True


def locked_by(profile: Path) -> int | None:
    """Return the live pi-camoufox pid holding this profile, if any."""
    lock = lock_file_for(profile)
    if not lock.exists():
        return None
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid and is_process_alive(pid) and (pid == os.getpid() or _looks_like_our_server(pid)):
        return pid
    return None


def acquire_profile_lock(profile: Path) -> Path:
    """Reserve a profile for this process.

    Two servers on one profile would fight over Firefox's own lock and can leave
    cookies and the fingerprint out of sync. The pid file makes that failure
    explicit, and a stale file from a crash is reclaimed automatically.
    """
    profile.mkdir(parents=True, exist_ok=True)
    holder = locked_by(profile)
    if holder is not None:
        raise BrowserLaunchError(
            f"profile {profile} is already in use by pi-camoufox (pid {holder}).\n"
            "  * stop that server first, or\n"
            "  * use a different profile with --profile."
        )
    lock = lock_file_for(profile)
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return lock


def release_profile_lock(lock: Path) -> None:
    try:
        if lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink(missing_ok=True)
    except OSError:
        pass


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

    lock = acquire_profile_lock(browser.profile)
    try:
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
    except Exception:
        release_profile_lock(lock)
        raise

    return BrowserHandle(camoufox=manager, context=context, fingerprint_meta=meta, lock_file=lock)
