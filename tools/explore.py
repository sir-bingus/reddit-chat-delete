#!/usr/bin/env python3
"""Headed reconnaissance against the real chat.reddit.com, using the saved profile.

Read-only: it opens conversations and dumps structure. It never opens an action
menu and never deletes anything.

    .venv/bin/python tools/explore.py            # sidebar only
    .venv/bin/python tools/explore.py --open 1   # also open conversation #1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

EXPLORE_JS = Path(__file__).with_name("explore.js")
OUT = ROOT / "reports" / "explore"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", type=Path, default=ROOT / ".browser-profile")
    ap.add_argument("--url", default="https://chat.reddit.com/")
    ap.add_argument("--open", type=int, default=None,
                    help="index into the detected conversation rows to click open")
    ap.add_argument("--settle", type=int, default=9000, help="ms to wait for the app to render")
    ap.add_argument("--min-repeat", type=int, default=4)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(args.profile), headless=False,
            viewport={"width": 1500, "height": 980},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("console", lambda m: print(f"  [console:{m.type}] {m.text[:160]}"))
        try:
            print(f"-> {args.url}")
            page.goto(args.url, wait_until="domcontentloaded")
            page.wait_for_timeout(args.settle)
            print(f"   landed on {page.url}")

            page.add_script_tag(path=str(EXPLORE_JS))
            data = page.evaluate("(m) => window.__EXPLORE.all(m)", args.min_repeat)
            (OUT / "sidebar.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
            page.screenshot(path=str(OUT / "sidebar.png"))
            print(f"   wrote {OUT/'sidebar.json'} ({len(data['repeatedGroups'])} repeated groups)")
            summarize(data)

            if args.open is not None:
                grp = pick_conversation_group(data)
                if not grp:
                    print("!! no conversation-like group detected; see sidebar.json")
                    return 1
                print(f"\n-> opening conversation #{args.open} from group {grp['childSig']!r}")
                ok = page.evaluate(
                    """([sigPath, childSig, idx]) => {
                        const E = window.__EXPLORE;
                        for (const p of E.deepAll('*')) {
                            if (E.path(p) !== sigPath) continue;
                            const kids = Array.from(p.children).filter((k) => {
                                const cls = (typeof k.className === 'string' ? k.className : '')
                                  .split(/\\s+/).filter(Boolean).slice(0,3).join('.');
                                return k.tagName.toLowerCase() + (cls ? '.' + cls : '') === childSig;
                            });
                            const el = kids[idx];
                            if (!el) return false;
                            el.scrollIntoView();
                            (el.querySelector('a,button') || el).click();
                            return true;
                        }
                        return false;
                    }""",
                    [grp["parentPath"], grp["childSig"], args.open])
                print(f"   click dispatched: {ok}")
                page.wait_for_timeout(6000)
                page.add_script_tag(path=str(EXPLORE_JS))
                room = page.evaluate("(m) => window.__EXPLORE.all(m)", args.min_repeat)
                (OUT / "room.json").write_text(json.dumps(room, indent=2), encoding="utf-8")
                page.screenshot(path=str(OUT / "room.png"))
                print(f"   wrote {OUT/'room.json'}")
                summarize(room)
        finally:
            ctx.close()
    return 0


def pick_conversation_group(data: dict) -> dict | None:
    """The conversation list: many similar rows in a tall, narrow, scrolling column."""
    best = None
    for g in data["repeatedGroups"]:
        r = g["parentRect"]
        if r["w"] > 520 or r["h"] < 300 or g["count"] < 5:
            continue
        score = g["count"] + (30 if g["parentScrolls"] else 0)
        if best is None or score > best[0]:
            best = (score, g)
    return best[1] if best else None


def summarize(d: dict) -> None:
    print(f"   url={d['url']}  title={d['title']!r}")
    print(f"   customTags: {', '.join(d['customTags'][:12]) or '(none)'}")
    print(f"   testids: {', '.join(d['testids'][:12]) or '(none)'}")
    print("   top repeated groups:")
    for g in d["repeatedGroups"][:8]:
        r = g["parentRect"]
        print(f"     {g['count']:4d}x {g['childSig'][:52]:52s} "
              f"box={r['w']}x{r['h']} scrolls={g['parentScrolls']} imgs={g['sampleHasImg']}")
        if g["sampleText"] and any(g["sampleText"]):
            print(f"           e.g. {g['sampleText'][0][:80]!r}")
    print("   scrollers:")
    for s in d["scrollers"][:5]:
        print(f"     {s['rect']['w']}x{s['rect']['h']} sh={s['scrollHeight']} kids={s['childCount']} "
              f"{s['path'][-70:]}")


if __name__ == "__main__":
    raise SystemExit(main())
