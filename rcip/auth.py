"""Getting a session token, the only thing the browser is still needed for.

Reddit's chat credentials are not in localStorage, so rather than reverse
engineer where the client keeps them we launch the real client briefly and
read the bearer token off a request it makes. That also doubles as the login
check: a token appearing means the session is valid.

The token is held in memory for the run and never written to disk.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .browser import Browser
from .logging_setup import LOG

DEFAULT_BASE = "https://matrix.redditspace.com"


def harvest_token(profile_dir, hidden: bool = True, timeout_s: int = 180,
                  reload_after_s: int = 15) -> tuple[str, str]:
    """Return (token, api_base). Raises if the user is not signed in."""
    captured: dict[str, str] = {}

    def on_request(request) -> None:
        if "_matrix" not in request.url:
            return
        for key, value in request.headers.items():
            if key.lower() == "authorization" and value.lower().startswith("bearer "):
                captured.setdefault("token", value.split(" ", 1)[1])
                captured.setdefault("base", request.url.split("/_matrix")[0])

    LOG.info("opening a browser briefly to pick up your session token")
    with Browser(profile_dir, hidden=hidden) as browser:
        browser.page.on("request", on_request)
        browser.open_chat()

        # Waiting passively for the client to call the API is not enough: served
        # from cache it may never make one. Reload periodically to force fresh
        # requests, and say what we are doing so a slow start is not mistaken
        # for a hang.
        started = time.time()
        deadline = started + timeout_s
        next_reload = started + reload_after_s
        next_report = started + 10
        while time.time() < deadline and "token" not in captured:
            if not browser.is_alive():
                raise RuntimeError("the browser was closed before a token appeared")
            now = time.time()
            if now >= next_reload:
                LOG.info("  no API call seen yet after %.0fs; reloading the page to "
                         "prompt one", now - started)
                try:
                    browser.page.reload(wait_until="domcontentloaded")
                except Exception as exc:
                    LOG.debug("  reload failed: %s", exc)
                next_reload = now + reload_after_s
            elif now >= next_report:
                LOG.info("  still waiting for a session token (%.0fs of %ds)",
                         now - started, timeout_s)
                next_report = now + 10
            browser.page.wait_for_timeout(500)

    if "token" not in captured:
        raise RuntimeError(
            f"no session token seen in {timeout_s}s. The browser reached "
            f"{'the chat page' if True else '?'} but made no authenticated API call - "
            "you are probably signed out. Run with --show-window and log in.")
    LOG.info("session token acquired (%d chars)", len(captured["token"]))
    return captured["token"], captured.get("base", DEFAULT_BASE)


def load_cached(cache: Path) -> tuple[str, str] | None:
    """A previously harvested token, if we have one. Not validated here."""
    if not cache or not cache.exists():
        return None
    try:
        d = json.loads(cache.read_text(encoding="utf-8"))
    except Exception as exc:
        LOG.debug("token cache unreadable (%s)", exc)
        return None
    token, base = d.get("token"), d.get("base")
    if not token or not base:
        return None
    age_min = (time.time() - d.get("acquired", 0)) / 60
    LOG.info("reusing the cached session token (%.0f min old)", age_min)
    return token, base


def save_cached(cache: Path, token: str, base: str) -> None:
    """Persist the token so the next run does not need a browser at all.

    It is a credential, so the file is owner-only and git-ignored.
    """
    if not cache:
        return
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": token, "base": base,
                                   "acquired": int(time.time())}), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, cache)
        LOG.debug("cached the session token to %s", cache)
    except Exception as exc:
        LOG.debug("could not cache the token: %s", exc)


def get_token(profile_dir, cache: Path | None = None, hidden: bool = True,
              force: bool = False) -> tuple[str, str]:
    """The token to use, preferring a cached one.

    Harvesting needs a browser and, as observed live, can take a minute of
    forced reloads before the client makes an authenticated call. Caching
    turns that into a rare event: we only pay it when there is no token or
    the server rejects the one we have.
    """
    if not force:
        cached = load_cached(cache) if cache else None
        if cached:
            return cached
    token, base = harvest_token(profile_dir, hidden=hidden)
    save_cached(cache, token, base)
    return token, base
