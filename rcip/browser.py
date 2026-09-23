"""A browser, used for one thing only: obtaining a session token.

Everything else talks to the API directly. This exists because Reddit's chat
credentials are not in localStorage, so the practical way to get a token is to
let the real client sign in and read the token off a request it makes.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import psutil

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .logging_setup import LOG

CHAT_URL = "https://chat.reddit.com/"

# Keep a hidden window rendering normally instead of being throttled as
# "occluded"; Reddit's chat app does not render at all in headless Chromium.
ANTI_THROTTLE = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]


class Browser:
    """A persistent Chromium profile holding your Reddit login."""

    def __init__(self, profile_dir: Path, hidden: bool = False):
        self.profile_dir = Path(profile_dir)
        self.hidden = hidden
        self._pw = None
        self.context = None
        self.page = None

    def __enter__(self) -> "Browser":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        if self.hidden:
            args += ANTI_THROTTLE
        LOG.info("launching Chromium with profile %s (%s)", self.profile_dir,
                 "off-screen window" if self.hidden else "visible window")
        self._pw = sync_playwright().start()
        try:
            self.context = self._launch(args)
        except PlaywrightError as exc:
            if "already in use" not in str(exc) and "existing browser session" not in str(exc):
                raise
            # A hidden window from an interrupted run can outlive it and keep
            # the profile locked. It is our own browser on our own profile, so
            # clear it and try once more rather than failing with Playwright's
            # rather opaque message.
            orphan = self._browser_pid()
            if orphan is None:
                raise RuntimeError(
                    f"the profile {self.profile_dir} is locked by another Chromium "
                    "that we cannot find. Close it, or quit Chromium, and retry."
                ) from exc
            LOG.warning("a browser from an earlier run (pid %d) still holds this "
                        "profile; closing it", orphan)
            self._kill(orphan)
            self.context = self._launch(args)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15_000)
        if self.hidden:
            self._hide_window()
        return self

    def _launch(self, args: list[str]):
        return self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=False,          # Reddit chat renders nothing when headless
            viewport={"width": 1440, "height": 950},
            args=args,
        )

    @staticmethod
    def _kill(pid: int) -> None:
        """End a browser process: politely, then firmly. Works on every OS."""
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            LOG.warning("not allowed to close pid %d; close it yourself and retry", pid)
        except psutil.TimeoutExpired:
            LOG.warning("pid %d would not exit; close it yourself and retry", pid)

    def __exit__(self, *exc) -> None:
        # Capture the pid before closing: a hidden window sometimes survives
        # context.close(), and an orphan holding the profile blocks every
        # later run.
        pid = self._browser_pid()
        for closer in (getattr(self.context, "close", None), getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception as e:                      # pragma: no cover
                LOG.debug("teardown error: %s", e)
        self.context = self._pw = self.page = None
        if pid is not None and self._running(pid):
            LOG.debug("browser pid %d outlived close(); ending it", pid)
            self._kill(pid)

    @staticmethod
    def _running(pid: int) -> bool:
        # Not os.kill(pid, 0): on Windows that terminates the process instead
        # of probing it.
        try:
            return psutil.Process(pid).is_running()
        except psutil.Error:
            return False

    def is_alive(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    def open_chat(self) -> None:
        LOG.info("navigating to %s", CHAT_URL)
        try:
            self.page.goto(CHAT_URL, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            LOG.warning("could not open the chat page: %s", exc)

    # ------------------------------------------------------------ hiding

    def _browser_pid(self) -> int | None:
        """The main Chromium process using our profile.

        Chromium's helpers (renderer, GPU, ...) share the --user-data-dir
        argument but carry --type=...; the browser process itself does not.
        """
        target = str(self.profile_dir.resolve())
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                args = proc.info.get("cmdline") or []
            except psutil.Error:
                continue
            if any(a.startswith("--type=") for a in args):
                continue
            for a in args:
                if a.startswith("--user-data-dir="):
                    given = a.split("=", 1)[1]
                    try:
                        same = Path(given).resolve() == Path(target)
                    except OSError:
                        same = given == target
                    if same:
                        return proc.info["pid"]
        return None

    def _hide_window(self) -> None:
        """Ask macOS to hide the window. Never fatal - we just stay visible.

        macOS ignores --window-position, so parking it off-screen is not an
        option; the window genuinely has to be hidden by the OS.
        """
        if platform.system() != "Darwin":
            # Expected, not a problem: only macOS lets us hide it. The window
            # closes by itself once the token is picked up.
            LOG.info("the browser window will be visible for a few seconds "
                     "(hiding it is only possible on macOS)")
            return
        pid = self._browser_pid()
        if pid is None:
            LOG.warning("could not find the browser process; the window stays visible")
            return
        script = ('tell application "System Events" to set visible of '
                  f'(first process whose unix id is {pid}) to false')
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True,
                                    text=True, timeout=15)
        except Exception as exc:
            LOG.warning("could not hide the browser (%s); it stays visible", exc)
            return
        if result.returncode == 0:
            LOG.info("browser hidden (pid %d)", pid)
        else:
            LOG.warning("could not hide the browser: %s. Terminal may need Accessibility "
                        "permission in System Settings > Privacy & Security. Continuing "
                        "with the window visible.", (result.stderr or "").strip()[:140])
