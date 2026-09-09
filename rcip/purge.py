"""The walk: enumerate conversations, open each, scroll back, delete images.

Both of Reddit's lists are virtualised - only ~20 rows exist in the DOM at any
moment - so everything here is keyed on server ids rather than DOM nodes:

  * conversations by  rs-rooms-nav-room[room="!…:reddit.com"]
  * messages by       rs-timeline-event[data-id="$…"]

Ownership is decided by the hover toolbar: your own messages offer *Delete*,
other people's offer *Report*. Nothing else is treated as deletable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .config import Config, label_matches_skip, normalize_user
from .logging_setup import LOG, Audit, dump_artifacts

ROOM_URL = "https://www.reddit.com/chat/room/{}"


@dataclass
class Stats:
    rooms_listed: int = 0
    rooms_seen: int = 0
    rooms_skipped: int = 0
    rooms_processed: int = 0
    images_found: int = 0
    images_deleted: int = 0
    images_would_delete: int = 0
    images_not_mine: int = 0
    failures: int = 0
    errors: list[str] = field(default_factory=list)


class Purger:
    def __init__(self, browser, cfg: Config, audit: Audit, paths, stats: Stats):
        self.b = browser
        self.cfg = cfg
        self.audit = audit
        self.paths = paths
        self.stats = stats
        self.state_path: Path | None = cfg.state_file
        self.done_rooms: set[str] = self._load_state()
        self.skipped_rooms: set[str] = set()   # skipped/failed this run; not retried

    # ------------------------------------------------------------------ state

    def _load_state(self) -> set[str]:
        if not self.state_path or not self.state_path.exists():
            return set()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            rooms = set(data.get("done_rooms", []))
            LOG.info("resuming: %d conversation(s) already completed in a previous run",
                     len(rooms))
            return rooms
        except Exception as exc:
            LOG.warning("could not read state file %s: %s", self.state_path, exc)
            return set()

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.write_text(
                json.dumps({"done_rooms": sorted(self.done_rooms),
                            "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=1),
                encoding="utf-8")
        except Exception as exc:
            LOG.debug("could not write state file: %s", exc)

    # ------------------------------------------------------------- enumerate

    def _mounted_rooms(self) -> list[dict]:
        return self.b.rcip("visibleRooms()") or []

    def _next_room(self) -> dict | None:
        """The first conversation currently in the sidebar we have not done."""
        for r in self._mounted_rooms():
            if r["room"] not in self.done_rooms and r["room"] not in self.skipped_rooms:
                return r
        return None

    # ------------------------------------------------------------------- run

    def run_list(self, rooms: list[dict]) -> Stats:
        """Process an explicit list of conversations (a worker's shard)."""
        for room in rooms:
            if self.cfg.max_deletes_total and self._handled() >= self.cfg.max_deletes_total:
                LOG.info("hit --max-deletes-total=%d, stopping", self.cfg.max_deletes_total)
                break
            rid, user = room["room"], room.get("user") or ""
            if rid in self.done_rooms:
                continue
            self.stats.rooms_seen += 1
            who = f"u/{user}" if user else (room.get("label") or "(unnamed)")
            LOG.info("[%d/%d] %s", self.stats.rooms_seen, len(rooms), who)

            protected = self._protected(user) or label_matches_skip(room.get("label", ""),
                                                                    self.cfg.skip_users)
            if protected:
                LOG.info("  -> SKIPPED: %r is on the protected list", protected)
                self.stats.rooms_skipped += 1
                self.audit.write("room_skipped", room=rid, user=user,
                                 reason="skip_list", matched=protected)
                continue
            try:
                self._process_room(rid, user, room)
                self.done_rooms.add(rid)
                self._save_state()
            except Exception as exc:
                self.stats.failures += 1
                self.stats.errors.append(f"{who}: {exc}")
                LOG.exception("  -> conversation failed: %s", exc)
                self.audit.write("room_error", room=rid, user=user, error=str(exc)[:400],
                                 **dump_artifacts(self.b.page, self.paths,
                                                  f"room-{self.stats.rooms_seen}"))
        self.stats.rooms_listed = len(rooms)
        return self.stats

    def run(self) -> Stats:
        """Walk conversations one at a time.

        No upfront census: the sidebar is virtualised and lazy-loaded, so
        counting it first meant several minutes of scrolling and an answer that
        changed between runs. Instead we take the first unprocessed row that is
        mounted, handle it, and only scroll for more when we run out - which
        also means work starts immediately.
        """
        stalled = 0
        while True:
            if self.cfg.max_rooms and self.stats.rooms_processed >= self.cfg.max_rooms:
                LOG.info("hit --max-rooms=%d, stopping", self.cfg.max_rooms)
                break
            if self.cfg.max_deletes_total and self._handled() >= self.cfg.max_deletes_total:
                LOG.info("hit --max-deletes-total=%d, stopping", self.cfg.max_deletes_total)
                break

            room = self._next_room()
            if room is None:
                before = {r["room"] for r in self._mounted_rooms()}
                adv = self.b.rcip("advanceSidebar()")
                self.b.page.wait_for_timeout(self.cfg.sidebar_pause_ms)
                after = {r["room"] for r in self._mounted_rooms()}
                if adv is None:
                    LOG.error("no conversation list found - is this the chat page?")
                    dump_artifacts(self.b.page, self.paths, "no-sidebar")
                    break
                if after - before:
                    stalled = 0
                    continue
                stalled += 1
                if stalled >= self.cfg.sidebar_settle_rounds:
                    LOG.info("reached the end of the conversation list")
                    break
                continue

            stalled = 0
            self.stats.rooms_seen += 1
            rid, user = room["room"], room.get("user") or ""
            who = f"u/{user}" if user else (room.get("label") or "(unnamed)")
            LOG.info("[%d] %s", self.stats.rooms_seen, who)

            protected = self._protected(user) or label_matches_skip(room.get("label", ""),
                                                                    self.cfg.skip_users)
            if protected:
                LOG.info("  -> SKIPPED: %r is on the protected list", protected)
                self.stats.rooms_skipped += 1
                self.skipped_rooms.add(rid)
                self.audit.write("room_skipped", room=rid, user=user,
                                 reason="skip_list", matched=protected)
                continue

            if self.cfg.only_rooms and not any(p.lower() in user.lower()
                                               for p in self.cfg.only_rooms):
                self.skipped_rooms.add(rid)
                self.stats.rooms_skipped += 1
                continue

            try:
                self._process_room(rid, user, room)
                self.done_rooms.add(rid)
                self._save_state()
            except Exception as exc:
                self.stats.failures += 1
                self.stats.errors.append(f"{who}: {exc}")
                self.skipped_rooms.add(rid)   # do not retry in a loop
                LOG.exception("  -> conversation failed: %s", exc)
                self.audit.write("room_error", room=rid, user=user, error=str(exc)[:400],
                                 **dump_artifacts(self.b.page, self.paths,
                                                  f"room-{self.stats.rooms_seen}"))
        self.stats.rooms_listed = len(self.done_rooms) + len(self.skipped_rooms)
        return self.stats

    def _handled(self) -> int:
        return self.stats.images_deleted + self.stats.images_would_delete

    def _protected(self, user: str) -> str | None:
        n = normalize_user(user)
        return n if n and n in self.cfg.skip_users else None

    # ---------------------------------------------------------------- a room

    def _process_room(self, rid: str, user: str, room: dict | None = None) -> None:
        self._open_room(rid)

        if self._participant_check(rid, user) is False:
            return

        self.stats.rooms_processed += 1
        self.audit.write("room_open", room=rid, user=user)

        self._scroll_to_oldest()

        # Re-check once the oldest messages are loaded: a group conversation
        # may only reveal some participants further back in its history.
        if self._participant_check(rid, user, second_pass=True) is False:
            self.stats.rooms_processed -= 1
            return

        self._sweep(rid, user)

    def _participant_check(self, rid: str, user: str, second_pass: bool = False) -> bool:
        """False => do not touch this conversation.

        Participants come from the messages themselves rather than a header, so
        group chats are covered and a truncated sidebar label cannot mislead us.
        """
        info = self.b.rcip("roomParticipants()") or {}
        others = info.get("others") or []
        known = set(others) | ({user} if user else set())
        LOG.debug("  participants: %s (me=%s)", sorted(known), info.get("me"))

        for name in known:
            hit = self._protected(name)
            if hit:
                LOG.info("  -> SKIPPED: %r is on the protected list", hit)
                self.stats.rooms_skipped += 1
                self.audit.write("room_skipped", room=rid, user=user, reason="skip_list",
                                 matched=hit, participants=sorted(known),
                                 second_pass=second_pass)
                return False

        # Fail safe: with a protected list in force, never delete inside a
        # conversation whose participants we could not identify at all.
        if self.cfg.skip_users and not known:
            if not second_pass:
                return True  # give the timeline a chance to load first
            LOG.warning("  -> SKIPPED: could not identify anyone in this conversation "
                        "and a protected list is in force")
            self.stats.rooms_skipped += 1
            self.audit.write("room_skipped", room=rid, user=user,
                             reason="unidentified_participants")
            return False
        return True

    def _open_room(self, rid: str) -> None:
        """Click the sidebar row; fall back to its URL if that fails.

        Clicking keeps the single-page app alive, so the sidebar keeps its
        place and the next conversation is already mounted. Navigating by URL
        reloads everything and sends the list back to the top.
        """
        if self.cfg.open_by_click and self.b.rcip("openRoomByClick(a)", rid):
            self.b.page.wait_for_timeout(self.cfg.room_settle_ms)
            self.b.ensure_injected()
            if self.b.rcip("currentRoomId()") == rid:
                return
            LOG.debug("  click did not switch rooms; falling back to the URL")
        url = self.cfg.room_url_template.format(quote(rid, safe=""))
        LOG.debug("  opening %s", url)
        self.b.page.goto(url, wait_until="domcontentloaded")
        self.b.page.wait_for_timeout(self.cfg.room_settle_ms)
        self.b.ensure_injected()

    def _scroll_to_oldest(self) -> None:
        """Page back until Reddit stops loading older messages.

        Driven by elements, not pixels: scroll the oldest mounted message to
        the top and Reddit loads whatever came before it. When the oldest
        message stops changing, we are at the beginning.
        """
        seen_first = None
        idle = 0
        for round_no in range(self.cfg.max_scroll_rounds):
            st = self.b.rcip("advanceTimelineUp()")
            if st is None:
                LOG.warning("  no timeline found")
                dump_artifacts(self.b.page, self.paths, "no-timeline")
                return
            self.b.page.wait_for_timeout(self.cfg.scroll_pause_ms)
            first = st.get("firstId")
            idle = idle + 1 if first == seen_first else 0
            seen_first = first
            if round_no % 15 == 0:
                LOG.debug("  scroll-back round %d: %d event(s) mounted",
                          round_no, st.get("events"))
            if idle >= self.cfg.idle_scroll_rounds:
                LOG.info("  reached the oldest message after %d page(s)", round_no + 1)
                return
        LOG.warning("  stopped after --max-scroll-rounds=%d; older messages may remain",
                    self.cfg.max_scroll_rounds)

    def _sweep(self, rid: str, user: str) -> None:
        """Oldest -> newest, handling every image message on the way."""
        handled: set[str] = set()
        in_room = 0
        stagnant = 0

        while True:
            self.b.ensure_injected()
            events = self.b.rcip("imageEvents()") or []
            pending = [e for e in events if e["id"] not in handled]

            for ev in pending:
                handled.add(ev["id"])
                self.stats.images_found += 1
                if (self.cfg.max_deletes_per_room and in_room >= self.cfg.max_deletes_per_room):
                    LOG.info("  hit --max-deletes-per-room=%d", self.cfg.max_deletes_per_room)
                    return
                if (self.cfg.max_deletes_total and self._handled() >= self.cfg.max_deletes_total):
                    LOG.info("  hit --max-deletes-total=%d", self.cfg.max_deletes_total)
                    return
                outcome = self._handle_image(rid, user, ev)
                if outcome in ("deleted", "would-delete"):
                    in_room += 1
                if outcome == "deleted":
                    self.b.page.wait_for_timeout(self.cfg.delete_delay_ms)

            if not pending:
                before = self.b.rcip("advanceTimelineDown()") or {}
                self.b.page.wait_for_timeout(self.cfg.scroll_pause_ms)
                after = self.b.rcip("advanceTimelineDown()") or {}
                # the newest message stops changing once we are at the end
                stagnant = stagnant + 1 if after.get("lastId") == before.get("lastId") else 0
                if stagnant >= self.cfg.idle_scroll_rounds:
                    LOG.info("  done: %d image(s) %s here", in_room,
                             "deleted" if self.cfg.execute else "flagged")
                    return

    # ------------------------------------------------------------ one image

    def _hover_menu(self, ev_id: str) -> list[dict]:
        """Hover a message and read its action toolbar."""
        page = self.b.page
        box = self.b.rcip("scrollEventIntoView(a)", ev_id)
        if not box:
            return []
        page.wait_for_timeout(250)
        box = self.b.rcip("eventBox(a)", ev_id) or box
        page.mouse.move(box["x"] + box["w"] // 2, box["y"] + min(box["h"] // 2, 40))
        page.wait_for_timeout(self.cfg.hover_pause_ms)
        labels = self.b.rcip("menuLabelsFor(a)", ev_id) or []
        # Only trust toolbar buttons rendered alongside this message.
        lo, hi = box["y"] - 30, box["y"] + box["h"] + 30
        return [b for b in labels if lo <= b["box"]["y"] <= hi]

    def _handle_image(self, rid: str, user: str, ev: dict) -> str:
        ev_id = ev["id"]
        url0 = (ev.get("urls") or [""])[0]
        page = self.b.page
        base = dict(room=rid, user=user, event_id=ev_id, url=url0[:200], kind=ev.get("kind", ""))

        def done(outcome: str, **extra) -> str:
            self.audit.write("image", outcome=outcome, **base, **extra)
            return outcome

        buttons = self._hover_menu(ev_id)
        if not buttons:
            buttons = self._hover_menu(ev_id)  # one retry; the toolbar can lag
        labels = [(b["label"] or "") for b in buttons]

        if not buttons:
            LOG.warning("    no action toolbar appeared for %s", ev_id[:14])
            self.stats.failures += 1
            dump_artifacts(page, self.paths, f"no-toolbar-{ev_id[:8]}")
            return done("no-toolbar")

        delete_btn = next((b for b in buttons if (b["label"] or "").strip().lower() == "delete"), None)
        if not delete_btn:
            self.stats.images_not_mine += 1
            LOG.info("    - not yours (toolbar offers %s) - left alone", "/".join(labels) or "nothing")
            return done("not-deletable", labels=labels)

        if not self.cfg.execute:
            self.stats.images_would_delete += 1
            LOG.info("    [dry-run] WOULD DELETE %s", url0[:80] or ev_id[:14])
            return done("would-delete")

        LOG.info("    deleting %s", url0[:80] or ev_id[:14])
        bb = delete_btn["box"]
        page.mouse.click(bb["x"] + bb["w"] // 2, bb["y"] + bb["h"] // 2)
        page.wait_for_timeout(self.cfg.dialog_pause_ms)

        confirm = next((b for b in (self.b.rcip("dialogButtons()") or [])
                        if b["text"].strip().lower() in ("yes, delete", "yes delete", "delete")), None)
        if not confirm:
            LOG.warning("    no confirmation button found; leaving the message alone")
            page.keyboard.press("Escape")
            self.stats.failures += 1
            dump_artifacts(page, self.paths, f"no-confirm-{ev_id[:8]}")
            return done("no-confirm-button")

        cb = confirm["box"]
        page.mouse.click(cb["x"] + cb["w"] // 2, cb["y"] + cb["h"] // 2)

        for _ in range(24):
            page.wait_for_timeout(250)
            if not self.b.rcip("eventExists(a)", ev_id):
                self.stats.images_deleted += 1
                return done("deleted")

        self.stats.failures += 1
        LOG.warning("    message still present after confirming - see artifacts")
        dump_artifacts(page, self.paths, f"not-gone-{ev_id[:8]}")
        return done("delete-unconfirmed")
