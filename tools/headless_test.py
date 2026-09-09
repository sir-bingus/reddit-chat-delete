#!/usr/bin/env python3
"""Which invisible-browser mode actually renders Reddit chat?

Each mode is scored on what the real engine needs: conversations in the
sidebar, and a timeline with messages in an opened room.
"""
from __future__ import annotations
import shutil, sys, tempfile
from pathlib import Path
from urllib.parse import quote
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright  # noqa: E402

DOM_JS = ROOT / "rcip" / "dom.js"
MASTER = ROOT / ".browser-profile"

MODES = [
    ("headed (baseline)",            dict(headless=False), []),
    ("old headless shell",           dict(headless=True), []),
    ("new headless (channel)",       dict(headless=True, channel="chromium"), []),
    ("headed, window offscreen",     dict(headless=False), ["--window-position=-3000,-3000"]),
]


def try_mode(pw, name, kwargs, extra_args) -> str:
    tmp = Path(tempfile.mkdtemp(prefix="rcip-hl-"))
    prof = tmp / "p"
    try:
        shutil.copytree(MASTER, prof, symlinks=True,
                        ignore=lambda d, n: [x for x in n if x in
                                             {"Cache", "Code Cache", "GPUCache", "Crashpad",
                                              "SingletonLock", "SingletonCookie",
                                              "SingletonSocket", "Safe Browsing"}])
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(prof), viewport={"width": 1500, "height": 980},
            args=["--disable-blink-features=AutomationControlled", *extra_args], **kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://chat.reddit.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(11000)
            page.add_script_tag(path=str(DOM_JS))
            rooms = page.evaluate("() => window.__RCIP.visibleRooms().length")
            if not rooms:
                return f"FAIL  sidebar empty (url={page.url[:60]})"
            rid = page.evaluate("() => window.__RCIP.visibleRooms()[0].room")
            page.goto(f"https://www.reddit.com/chat/room/{quote(rid, safe='')}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(7000)
            page.add_script_tag(path=str(DOM_JS))
            st = page.evaluate("() => window.__RCIP.timelineState()") or {}
            evs = st.get("eventCount", 0)
            ok = rooms > 0 and evs > 0
            return (f"{'OK   ' if ok else 'FAIL '} {rooms} conversation(s), "
                    f"{evs} message(s) mounted, scroller {st.get('scrollHeight')}px")
        finally:
            ctx.close()
    except Exception as exc:
        return f"ERROR {type(exc).__name__}: {str(exc)[:90]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    with sync_playwright() as pw:
        for name, kwargs, extra in MODES:
            print(f"  {name:28s} -> {try_mode(pw, name, kwargs, extra)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
