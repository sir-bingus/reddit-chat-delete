"""Getting a session token, the only thing the browser is still needed for.

Reddit's chat credentials are not in localStorage, so rather than reverse
engineer where the client keeps them we launch the real client briefly and
read the bearer token off a request it makes. That also doubles as the login
check: a token appearing means the session is valid.

The token is held in memory for the run and never written to disk.
"""

from __future__ import annotations

import time

from .browser import Browser
from .logging_setup import LOG

DEFAULT_BASE = "https://matrix.redditspace.com"


def harvest_token(profile_dir, hidden: bool = True, timeout_s: int = 180) -> tuple[str, str]:
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
        deadline = time.time() + timeout_s
        prompted = False
        while time.time() < deadline and "token" not in captured:
            if not browser.is_alive():
                raise RuntimeError("the browser was closed before a token appeared")
            if not prompted and time.time() > deadline - timeout_s + 12:
                LOG.warning("Waiting for a signed-in Reddit chat session. If a browser "
                            "window is open, log in there; it will continue on its own.")
                prompted = True
            browser.page.wait_for_timeout(500)

    if "token" not in captured:
        raise RuntimeError("no session token seen - are you logged into Reddit chat?")
    LOG.info("session token acquired (%d chars)", len(captured["token"]))
    return captured["token"], captured.get("base", DEFAULT_BASE)
