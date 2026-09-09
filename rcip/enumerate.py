"""One-off census of every conversation, for sharding across workers.

Only the coordinator does this. A single pass reliably misses rows the
virtualiser never rendered, so it repeats until a whole pass adds nothing;
on a real account this converged to the same total (801) on repeated runs
where single passes ranged from 387 to 653.
"""

from __future__ import annotations

from .logging_setup import LOG


def enumerate_rooms(browser, cfg, audit=None) -> list[dict]:
    found: dict[str, dict] = {}

    def merge() -> None:
        for r in browser.rcip("visibleRooms()") or []:
            cur = found.get(r["room"])
            # a row caught mid-hydration has an empty aria-label; prefer a
            # later sighting that actually names the person
            if cur is None or (not cur.get("user") and r.get("user")):
                found[r["room"]] = r

    def settle(slices: int = 4, ms: int = 130) -> None:
        for _ in range(slices):
            browser.page.wait_for_timeout(ms)
            merge()

    LOG.info("taking a census of your conversations (needed once, to split the work)...")
    for pass_no in range(1, cfg.sidebar_passes + 1):
        before_pass = len(found)
        browser.rcip("scrollSidebarTop()")
        settle(3)
        stalled = 0
        for round_no in range(cfg.max_sidebar_rounds):
            before = {r["room"] for r in browser.rcip("visibleRooms()") or []}
            adv = browser.rcip("advanceSidebar()")
            if adv is None:
                LOG.error("no conversation list found")
                return []
            settle()
            after = {r["room"] for r in browser.rcip("visibleRooms()") or []}
            stalled = 0 if after - before else stalled + 1
            if round_no % 25 == 0:
                LOG.debug("  pass %d round %d: %d conversation(s)", pass_no, round_no, len(found))
            if stalled >= cfg.sidebar_settle_rounds:
                break
        added = len(found) - before_pass
        LOG.info("  census pass %d: %d conversation(s) (+%d)", pass_no, len(found), added)
        if added == 0:
            break

    merge()
    rooms = list(found.values())
    if audit:
        audit.write("census", count=len(rooms))
    return rooms
