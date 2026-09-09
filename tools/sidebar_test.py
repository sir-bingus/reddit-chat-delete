#!/usr/bin/env python3
"""Trace sidebar scrolling: does scrollTop advance, and does the room set grow?"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rcip.browser import Browser  # noqa: E402


def main() -> int:
    with Browser(ROOT / ".browser-profile", headless=False) as b:
        b.open_chat()
        if not b.wait_for_login(90):
            return 2
        seen = set()
        stagnant = 0
        for i in range(240):
            for r in b.rcip("visibleRooms()") or []:
                seen.add(r["room"])
            st = b.rcip("scrollSidebar(a)", None)
            b.page.wait_for_timeout(350)
            for r in b.rcip("visibleRooms()") or []:
                seen.add(r["room"])
            if st is None:
                print("no scroller"); break
            if i % 10 == 0 or st["atBottom"]:
                print(f"  {i:3d}: top={st['after']:6d}/{st['scrollHeight']:6d} "
                      f"moved={st['moved']} atBottom={st['atBottom']} unique={len(seen)}")
            stagnant = stagnant + 1 if not st["moved"] else 0
            if stagnant >= 25:
                print(f"  stopped moving at round {i}, top={st['after']}/{st['scrollHeight']}")
                break
        print(f"\nTOTAL unique conversations: {len(seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
