"""One-off census of every conversation, for sharding across workers.

Only the coordinator does this. A single pass reliably misses rows the
virtualiser never rendered, so it repeats until a whole pass adds nothing;
on a real account this converged to the same total (801) on repeated runs
where single passes ranged from 387 to 653.
"""

from __future__ import annotations

from .logging_setup import LOG


def enumerate_rooms(browser, cfg, audit=None, limit: int | None = None) -> list[dict]:
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
    if limit:
        LOG.info("  (stopping early at %d, per --max-rooms)", limit)
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
            if limit and len(found) >= limit:
                break
            if stalled >= cfg.sidebar_settle_rounds:
                break
        if limit and len(found) >= limit:
            break
        added = len(found) - before_pass
        LOG.info("  census pass %d: %d conversation(s) (+%d)", pass_no, len(found), added)
        if added == 0:
            break

    merge()
    rooms = list(found.values())
    if limit:
        rooms = rooms[:limit]
    if audit:
        audit.write("census", count=len(rooms))
    return rooms


def load_or_build_census(browser, cfg, audit=None, limit: int | None = None) -> list[dict]:
    """Return the conversation list, reusing a cached census when we can.

    The census costs several minutes of scrolling, and the list barely changes
    between runs, so it is written once and reused. It is rebuilt when the
    cache is missing, empty, unreadable, or when --refresh-census is passed.
    """
    import json
    import time

    cache = cfg.rooms_cache
    if cache and not cfg.refresh_census:
        rooms = _read_cache(cache)
        if rooms:
            age_h = (time.time() - cache.stat().st_mtime) / 3600
            LOG.info("reusing the cached conversation list: %d conversation(s) from %s "
                     "(%.1f hours old). Pass --refresh-census to rebuild it.",
                     len(rooms), cache.name, age_h)
            return rooms[:limit] if limit else rooms

    if cache and cfg.refresh_census:
        LOG.info("--refresh-census given: rebuilding the conversation list")

    rooms = enumerate_rooms(browser, cfg, audit, limit=limit)
    if limit:
        rooms = rooms[:limit]

    if cache and rooms and not limit:
        # A truncated list (--max-rooms) must never be cached as if complete.
        try:
            cache.write_text(json.dumps(rooms, indent=1), encoding="utf-8")
            LOG.info("cached %d conversation(s) to %s for future runs", len(rooms), cache)
        except Exception as exc:
            LOG.warning("could not write the census cache %s: %s", cache, exc)
    elif cache and limit:
        LOG.debug("not caching a census truncated by --max-rooms")
    return rooms


def read_census_cache(cache) -> list[dict]:
    """The cached conversation list, or [] if there is not a usable one."""
    return _read_cache(cache) if cache else []


def _read_cache(cache) -> list[dict]:
    import json
    if not cache.exists():
        LOG.debug("no census cache at %s", cache)
        return []
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except Exception as exc:
        LOG.warning("census cache %s is unreadable (%s); rebuilding", cache, exc)
        return []
    if not isinstance(data, list) or not data:
        LOG.info("census cache %s is empty; rebuilding", cache)
        return []
    rooms = [r for r in data if isinstance(r, dict) and r.get("room")]
    if len(rooms) != len(data):
        LOG.warning("census cache had %d malformed entr(y/ies), ignoring them",
                    len(data) - len(rooms))
    return rooms
