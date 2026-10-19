"""Process-wide configuration for the server and browser."""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

HeadlessMode = bool | Literal["virtual"]

DEFAULT_PROFILE = Path.home() / ".camoufox-agent" / "profile"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8787
DEFAULT_SETTLE_MS = 250
DEFAULT_DIALOG_TIMEOUT_MS = 30_000
DEFAULT_ACTION_TIMEOUT_MS = 15_000
DEFAULT_EVENT_CAPACITY = 4_000
DEFAULT_COALESCE_MS = 400
DEFAULT_ARTIFACT_TTL_S = 600


def host_os() -> str:
    """Camoufox's OS name for the machine we are running on."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


@dataclass
class BrowserOptions:
    """Everything that affects the Camoufox process and its identity."""

    profile: Path = DEFAULT_PROFILE
    headless: HeadlessMode = False
    os: str | None = field(default_factory=host_os)
    humanize: bool | float = True
    geoip: bool = False
    proxy: dict[str, str] | None = None
    locale: str | list[str] | None = None
    block_images: bool = False
    block_webrtc: bool = False
    block_webgl: bool = False
    disable_coop: bool = False
    enable_cache: bool = False
    main_world_eval: bool = True
    executable: str | None = None
    rotate_fingerprint: bool = False
    extra_args: list[str] = field(default_factory=list)

    def target_os(self) -> str | None:
        # None lets Camoufox pick from its real-world OS distribution.
        if self.os in (None, "", "random"):
            return None
        return self.os


@dataclass
class ServerOptions:
    browser: BrowserOptions = field(default_factory=BrowserOptions)
    host: str = DEFAULT_HOST
    http_port: int = DEFAULT_HTTP_PORT
    settle_ms: int = DEFAULT_SETTLE_MS
    dialog_timeout_ms: int = DEFAULT_DIALOG_TIMEOUT_MS
    action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS
    event_capacity: int = DEFAULT_EVENT_CAPACITY
    coalesce_ms: int = DEFAULT_COALESCE_MS
    artifact_ttl_s: int = DEFAULT_ARTIFACT_TTL_S
    debug: bool = False

    def camoufox_kwargs(self, fingerprint: dict[str, Any] | None) -> dict[str, Any]:
        """Keyword arguments for :class:`camoufox.AsyncCamoufox`.

        Kept in one place so the CLI, tests and ``--print-config`` all agree on
        what the browser actually receives.
        """
        b = self.browser
        kwargs: dict[str, Any] = {
            "persistent_context": True,
            "user_data_dir": str(b.profile),
            "headless": b.headless,
            "humanize": b.humanize,
            "geoip": b.geoip,
            "block_images": b.block_images,
            "block_webrtc": b.block_webrtc,
            "block_webgl": b.block_webgl,
            "disable_coop": b.disable_coop,
            "enable_cache": b.enable_cache,
            "main_world_eval": b.main_world_eval,
        }
        if b.target_os():
            kwargs["os"] = b.target_os()
        if b.proxy:
            kwargs["proxy"] = b.proxy
        if b.locale:
            kwargs["locale"] = b.locale
        if b.executable:
            kwargs["executable_path"] = b.executable
        if b.extra_args:
            kwargs["args"] = list(b.extra_args)
        if fingerprint is not None:
            kwargs["fingerprint"] = fingerprint
            # The persisted fpgen identity is deliberate, not a hand-written
            # fingerprint; suppress Camoufox's "custom fingerprint" warning.
            kwargs["i_know_what_im_doing"] = True
        return kwargs
