#!/usr/bin/env python3
"""Open a visible browser and record what the Reddit client sends.

Used to discover how the web UI does something we cannot reach from the API -
hiding a chat, for instance. Read-only on our side: we only watch.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rcip.browser import Browser  # noqa: E402

OUT = ROOT / "reports" / "capture.jsonl"
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 420
# GETs are the client constantly syncing; anything that CHANGES state is a
# POST/PUT/DELETE, which is what we are looking for.
INTERESTING = {"POST", "PUT", "DELETE"}


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fh = OUT.open("w", encoding="utf-8")
    seen = 0

    def on_request(req):
        nonlocal seen
        if req.method not in INTERESTING:
            return
        try:
            body = req.post_data
        except Exception:
            body = None
        rec = {"t": time.strftime("%H:%M:%S"), "method": req.method,
               "url": req.url, "body": (body or "")[:600]}
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        seen += 1
        print(f"  [{rec['t']}] {req.method} {req.url[:120]}", flush=True)

    with Browser(ROOT / ".browser-profile", hidden=False) as b:
        b.page.on("request", on_request)
        b.open_chat()
        print(f"\nBrowser is open. Recording state-changing requests for {SECONDS}s.")
        print("Open a conversation you have not opened yet, then hide it.\n", flush=True)
        deadline = time.time() + SECONDS
        while time.time() < deadline and b.is_alive():
            b.page.wait_for_timeout(1000)
    fh.close()
    print(f"\ncaptured {seen} state-changing request(s) -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
