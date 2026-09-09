"""Browser lifecycle: persistent profile, login gate, helper injection."""

from __future__ import annotations

import time
from pathlib import Path

import platform
import subprocess

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .logging_setup import LOG

CHAT_URL = "https://chat.reddit.com/"

# Keep a hidden window rendering normally instead of being throttled as
# "occluded" - the virtualised lists depend on it actually painting.
ANTI_THROTTLE = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]


class BrowserClosed(RuntimeError):
    """Raised when the window goes away mid-run - usually the user closed it."""
DOM_JS = Path(__file__).with_name("dom.js")


class Browser:
    """A persistent Chromium profile pointed at Reddit chat.

    The profile lives in the project so you log in once and every later run
    reuses the cookies. Nothing touches your real Chrome.
    """

    def __init__(self, profile_dir: Path, headless: bool = False, chat_url: str = CHAT_URL,
                 hidden: bool = False):
        self.profile_dir = profile_dir
        self.headless = headless
        self.hidden = hidden
        self.chat_url = chat_url  # overridden by the test fixture
        self._pw = None
        self.context = None
        self.page = None

    def __enter__(self) -> "Browser":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        if self.hidden:
            # Reddit's chat renders an empty shell in headless Chromium (both the
            # old headless shell and the new mode), so "invisible" means a real
            # browser that is hidden by the OS after launch. macOS ignores
            # --window-position, so off-screen placement is not an option.
            args += ANTI_THROTTLE
        LOG.info("launching Chromium with profile %s (%s)", self.profile_dir,
                 "headless" if self.headless else ("off-screen window" if self.hidden
                                                   else "visible window"))
        if self.headless:
            LOG.warning("--headless is not supported by Reddit chat: the app renders an "
                        "empty page. Use --hidden for an off-screen window instead.")
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 950},
            args=args,
        )
        self.context.add_init_script(path=str(DOM_JS))
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15_000)
        if self.hidden:
            self._hide_window()
        return self

    # ------------------------------------------------------------------ hiding

    def _browser_pid(self) -> int | None:
        """The Chromium process owning our profile (not its helper processes)."""
        try:
            out = subprocess.run(["ps", "-Ao", "pid,command"], capture_output=True,
                                 text=True, timeout=10).stdout
        except Exception as exc:
            LOG.debug("ps failed: %s", exc)
            return None
        needle = f"--user-data-dir={self.profile_dir}"
        for line in out.splitlines():
            if needle in line and "Helper" not in line:
                try:
                    return int(line.split()[0])
                except ValueError:
                    continue
        return None

    def _hide_window(self) -> None:
        """Ask the OS to hide the browser. Never fatal - we just stay visible."""
        if platform.system() != "Darwin":
            LOG.warning("--hidden is only implemented for macOS; the window will stay visible")
            return
        pid = self._browser_pid()
        if pid is None:
            LOG.warning("could not find the browser process; the window will stay visible")
            return
        script = ('tell application "System Events" to set visible of '
                  f'(first process whose unix id is {pid}) to false')
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True,
                               text=True, timeout=15)
        except Exception as exc:
            LOG.warning("could not hide the browser (%s); it will stay visible", exc)
            return
        if r.returncode == 0:
            LOG.info("browser hidden (pid %d); it keeps running off-screen", pid)
        else:
            LOG.warning("could not hide the browser: %s. This usually means Terminal needs "
                        "Accessibility permission in System Settings > Privacy & Security. "
                        "The run continues with the window visible.",
                        (r.stderr or "").strip()[:160])

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
