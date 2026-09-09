"""Browser lifecycle: persistent profile, login gate, helper injection."""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .logging_setup import LOG

CHAT_URL = "https://chat.reddit.com/"


class BrowserClosed(RuntimeError):
    """Raised when the window goes away mid-run - usually the user closed it."""
DOM_JS = Path(__file__).with_name("dom.js")


class Browser:
    """A persistent Chromium profile pointed at Reddit chat.

    The profile lives in the project so you log in once and every later run
    reuses the cookies. Nothing touches your real Chrome.
    """

    def __init__(self, profile_dir: Path, headless: bool = False, chat_url: str = CHAT_URL):
        self.profile_dir = profile_dir
        self.headless = headless
        self.chat_url = chat_url  # overridden by the test fixture
        self._pw = None
        self.context = None
        self.page = None

    def __enter__(self) -> "Browser":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        LOG.info("launching Chromium with profile %s (headless=%s)", self.profile_dir, self.headless)
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 950},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context.add_init_script(path=str(DOM_JS))
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15_000)
        return self

    def __exit__(self, *exc) -> None:
        if self._pw is None and self.context is None:
            return              # already closed (coordinator frees it early)
        LOG.debug("closing browser context")
        for closer in (getattr(self.context, "close", None), getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception as e:  # pragma: no cover
                LOG.debug("teardown error: %s", e)
        self.context = None
        self._pw = None
        self.page = None

    # ------------------------------------------------------------------ helpers

    def ensure_injected(self) -> None:
        """Re-inject the DOM helpers if a client-side nav wiped them."""
        if not self.is_alive():
            raise BrowserClosed("browser window is closed")
        present = self.page.evaluate("() => !!window.__RCIP")
        if not present:
            LOG.debug("re-injecting dom.js")
            self.page.add_script_tag(path=str(DOM_JS))

    def rcip(self, expr: str, *args):
        """Call into window.__RCIP with the helpers guaranteed present."""
        self.ensure_injected()
        return self.page.evaluate(f"(a) => window.__RCIP.{expr}", args[0] if args else None)

    # -------------------------------------------------------------------- login

    def open_chat(self) -> None:
        LOG.info("navigating to %s", self.chat_url)
        self.page.goto(self.chat_url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        LOG.debug("landed on %s", self.page.url)

    def is_alive(self) -> bool:
        """False once the user (or a crash) has closed the window."""
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    def wait_for_login(self, timeout_s: int) -> bool:
        """Block until the room list renders, prompting the user if needed.

        Returns False - rather than raising - if the window is closed while we
        wait, which is the normal way someone aborts a run.
        """
        deadline = time.time() + timeout_s
        prompted = False
        while time.time() < deadline:
            if not self.is_alive():
                LOG.warning("browser window was closed before login completed - nothing was done")
                return False
            self.ensure_injected()
            try:
                rooms = self.page.evaluate("() => window.__RCIP.visibleRooms().length")
            except PlaywrightError as exc:
                if self._closed_error(exc):
                    LOG.warning("browser window was closed before login completed - "
                                "nothing was done")
                    return False
                LOG.debug("room probe failed: %s", exc)
                rooms = 0
            except Exception as exc:
                LOG.debug("room probe failed: %s", exc)
                rooms = 0
            if rooms > 0:
                LOG.info("logged in - %d conversation(s) visible in the sidebar", rooms)
                return True
            if not prompted:
                LOG.warning(
                    "Not logged in yet. Log into Reddit in the Chromium window that just "
                    "opened, then leave it on the chat page. Waiting up to %ds...", timeout_s)
                prompted = True
            try:
                self.page.wait_for_timeout(2000)
            except PlaywrightError as exc:
                if self._closed_error(exc):
                    LOG.warning("browser window was closed before login completed - "
                                "nothing was done")
                    return False
                raise
        LOG.error("timed out after %ds waiting for a logged-in chat page (url=%s)",
                  timeout_s, self.page.url if self.is_alive() else "<closed>")
        return False

    @staticmethod
    def _closed_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        return ("target page, context or browser has been closed" in text
                or "browser has been closed" in text
                or "target closed" in text)
