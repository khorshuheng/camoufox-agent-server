from __future__ import annotations

import os
import subprocess
import sys

import pytest

from camoufox_agent_server import browser


def test_profile_lock_recovers_from_a_dead_pid(tmp_path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    lock = browser.lock_file_for(profile)
    lock.write_text("999999999\n", encoding="utf-8")

    acquired = browser.acquire_profile_lock(profile)

    assert acquired == lock
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    browser.release_profile_lock(lock)
    assert not lock.exists()


def test_profile_lock_rejects_a_live_holder(tmp_path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    lock = browser.lock_file_for(profile)
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")

    with pytest.raises(browser.BrowserLaunchError, match="already in use"):
        browser.acquire_profile_lock(profile)


def test_profile_lock_reclaims_a_recycled_pid(tmp_path, monkeypatch) -> None:
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock = browser.lock_file_for(profile)
        lock.write_text(f"{holder.pid}\n", encoding="utf-8")
        monkeypatch.setattr(browser, "_looks_like_our_server", lambda _pid: False)

        acquired = browser.acquire_profile_lock(profile)

        assert acquired == lock
        assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
        browser.release_profile_lock(lock)
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_release_does_not_remove_another_owners_lock(tmp_path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    lock = browser.lock_file_for(profile)
    lock.write_text("12345\n", encoding="utf-8")

    browser.release_profile_lock(lock)

    assert lock.exists()
    assert lock.read_text(encoding="utf-8").strip() == "12345"


def test_is_process_alive() -> None:
    assert browser.is_process_alive(os.getpid()) is True
    assert browser.is_process_alive(999_999_999) is False
