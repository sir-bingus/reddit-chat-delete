"""Getting a session token, the one thing the browser is still needed for.

Reddit's chat credentials are not in localStorage, so instead of digging for
them we let the real client load and read the bearer token off a request it
makes. A token appearing also proves the session is logged in.

The token is cached in .session-token.json (owner-only, git-ignored) so most
runs never open a browser. It is re-harvested when missing or rejected.

Logging in: the browser normally runs hidden. On the first run, or whenever a
hidden attempt produces no token (which usually means you are logged out), it
reopens visibly and waits for you to log in. While you are on a login page it
never reloads, so it will not wipe what you are typing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .browser import CHAT_URL, Browser
from .logging_setup import LOG

DEFAULT_BASE = "https://matrix.redditspace.com"
LOGIN_HINTS = ("/login", "/register", "/account/", "accounts.reddit.com")


def _first_run(profile_dir: Path) -> bool:
    """No saved browser profile yet, so there cannot be a saved login."""
    p = Path(profile_dir)
    return not p.exists() or not any(p.iterdir())


def _attempt(profile_dir, hidden: bool, timeout_s: int, reload_after_s: int) -> dict:
    captured: dict[str, str] = {}

    def on_request(request) -> None:
        if "_matrix" not in request.url:
            return
        for key, value in request.headers.items():
            if key.lower() == "authorization" and value.lower().startswith("bearer "):
                captured.setdefault("token", value.split(" ", 1)[1])
                captured.setdefault("base", request.url.split("/_matrix")[0])

    with Browser(profile_dir, hidden=hidden) as browser:
        browser.page.on("request", on_request)
        browser.open_chat()

        started = time.time()
        deadline = started + timeout_s
        next_reload = started + reload_after_s
        next_report = started + 10
        told_login = False
        while time.time() < deadline and "token" not in captured:
            if not browser.is_alive():
                raise RuntimeError("the browser was closed before a token appeared")
            now = time.time()
            url = browser.page.url or ""
            on_login = any(h in url for h in LOGIN_HINTS)

            if on_login:
                if not told_login:
                    LOG.warning("Reddit is asking you to log in. Log in in the browser "
                                "window; this continues by itself once you are in "
                                "(waiting up to %ds).", timeout_s)
                    told_login = True
            elif "chat" not in url:
                # after logging in Reddit may land on its home page; the token
                # only shows up once the chat client is running
                LOG.info("  opening the chat page")
                try:
                    browser.page.goto(CHAT_URL, wait_until="domcontentloaded")
                except Exception as exc:
                    LOG.debug("  navigation failed: %s", exc)
                next_reload = now + reload_after_s
            elif now >= next_reload:
                # served from cache, the client may never make an API call on
                # its own; a reload prompts one
                LOG.info("  no API call seen after %.0fs; reloading the chat page",
                         now - started)
                try:
                    browser.page.reload(wait_until="domcontentloaded")
                except Exception as exc:
                    LOG.debug("  reload failed: %s", exc)
                next_reload = now + reload_after_s
            elif now >= next_report and not on_login:
                LOG.info("  still waiting for a session token (%.0fs)", now - started)
                next_report = now + 10
            browser.page.wait_for_timeout(500)
    return captured


def harvest_token(profile_dir, hidden: bool = True, timeout_s: int = 300,
                  reload_after_s: int = 15, reveal_after_s: int = 45) -> tuple[str, str]:
    """Return (token, api_base), showing the browser for a login if needed."""
    if hidden and _first_run(profile_dir):
        LOG.info("first run: opening a browser window so you can log in to Reddit")
        hidden = False

    LOG.info("opening a browser%s to pick up your session token",
             " (hidden)" if hidden else "")
    captured = _attempt(profile_dir, hidden,
                        reveal_after_s if hidden else timeout_s, reload_after_s)

    if "token" not in captured and hidden:
        LOG.warning("No session after %ds, so you are probably logged out. Opening "
                    "the browser window so you can log in.", reveal_after_s)
        captured = _attempt(profile_dir, False, timeout_s, reload_after_s)

    if "token" not in captured:
        raise RuntimeError(
            f"no session token after {timeout_s}s. Make sure you are logged in to "
            "Reddit in the browser window, then run the command again.")
    LOG.info("session token acquired")
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
        # unique per process: two runs saving at once must not share a path
        tmp = cache.with_suffix(f".{os.getpid()}.tmp")
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
