"""The walk: enumerate conversations, open each, scroll back, delete images.

Both of Reddit's lists are virtualised - only ~20 rows exist in the DOM at any
moment - so everything here is keyed on server ids rather than DOM nodes:

  * conversations by  rs-rooms-nav-room[room="!…:reddit.com"]
  * messages by       rs-timeline-event[data-id="$…"]

Ownership is decided by the hover toolbar: your own messages offer *Delete*,
other people's offer *Report*. Nothing else is treated as deletable.
"""

from __future__ import annotations

import fcntl
import json
import os
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
        self._room_failures = 0                # deletions that failed in the current room
        self._me: str = ""                     # our own reddit display name
        self.unresolved: dict[str, set[str]] = {}   # room -> image ids still undecided
        self._full_sweep = True                # did this conversation get fully walked?

    # ------------------------------------------------------------------ state

    def _load_state(self) -> set[str]:
        """Everything finished before now, from this run's file and its siblings.

        Parallel runs write one progress file per worker. A later run - single
        window or parallel with a different worker count - has to see all of
        them, otherwise it happily redoes hundreds of finished conversations.
        """
        if not self.state_path:
            return set()
        rooms: set[str] = set()
        self.unresolved: dict[str, set[str]] = {}
        sources = [self.state_path]
        # legacy per-worker files from before runs shared one state file
        sources += sorted(self.state_path.parent.glob("state-w*.json"))
        for path in sources:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                found = set(data.get("done_rooms", []))
                rooms |= found
                for rid, ids in (data.get("unresolved") or {}).items():
                    self.unresolved.setdefault(rid, set()).update(ids)
                LOG.debug("  %s: %d conversation(s)", path.name, len(found))
            except Exception as exc:
                LOG.warning("could not read state file %s: %s", path, exc)
        # A conversation holding images we never resolved is not finished,
        # whatever an earlier pass recorded. Otherwise those images are lost:
        # the conversation is skipped forever and nobody ever looks again.
        blocked = {r for r, ids in self.unresolved.items() if ids}
        revisit = rooms & blocked
        rooms -= blocked
        if rooms:
            LOG.info("resuming: %d conversation(s) already completed in earlier run(s)",
                     len(rooms))
        if revisit:
            LOG.info("%d conversation(s) will be revisited: they still hold %d image(s) "
                     "whose ownership was never resolved",
                     len(revisit), sum(len(self.unresolved[r]) for r in revisit))
        return rooms

    def _save_state(self) -> None:
        """Merge our progress into the shared state file, under a lock.

        Every run and every parallel worker writes to the same file, so it is
        always the single source of truth and never needs merging by hand.
        Concurrent writers take an exclusive lock and re-read before writing,
        so nobody clobbers anybody else's conversations.
        """
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_path, "a+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.seek(0)
                    raw = fh.read().strip()
                    existing = set()
                    if raw:
                        try:
                            existing = set(json.loads(raw).get("done_rooms", []))
                        except json.JSONDecodeError:
                            LOG.warning("state file was corrupt; rebuilding it from this "
                                        "run's progress")
                    merged = existing | self.done_rooms
                    self.done_rooms = merged      # adopt others' progress too
                    prior = {}
                    if raw:
                        try:
                            prior = json.loads(raw).get("unresolved") or {}
                        except json.JSONDecodeError:
                            prior = {}
                    combined = {r: set(v) for r, v in prior.items()}
                    for r, ids in self.unresolved.items():
                        combined[r] = set(ids) if ids else set()
                    combined = {r: sorted(v) for r, v in combined.items() if v}
                    fh.seek(0)
                    fh.truncate()
                    json.dump({"done_rooms": sorted(merged),
                               "unresolved": combined,
                               "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh, indent=1)
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
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
                self._settle_room(rid)
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
                self._settle_room(rid)
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
        self._room_failures = 0
        self._full_sweep = True
        self._open_room(rid)
        if not self._me:
            self._me = self.b.rcip("currentUserName()") or ""
            if self._me:
                LOG.info("signed in as u/%s", self._me)
            else:
                LOG.warning("could not read your own username; ownership will rely on the "
                            "Delete/Report toolbar alone")

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

    def _settle_room(self, rid: str) -> None:
        """Decide whether this conversation is finished, and record it.

        Finished means: nothing failed this pass, we walked the whole history
        rather than stopping early, and no image here is still undecided from
        any previous pass. The last condition matters - without it a
        conversation that failed once and later passed without re-encountering
        those images gets marked done and they are never looked at again.
        """
        leftover = {i for i in self.unresolved.get(rid, set())}
        if self._room_failures:
            LOG.warning("  %d image(s) unresolved here; keeping this conversation on "
                        "the list for a rerun", self._room_failures)
        elif not self._full_sweep:
            LOG.warning("  did not reach the whole history here; keeping this "
                        "conversation on the list for a rerun")
        elif leftover:
            LOG.warning("  %d image(s) from an earlier pass are still undecided; "
                        "keeping this conversation on the list", len(leftover))
        else:
            self.unresolved.pop(rid, None)
            self.done_rooms.add(rid)
        self._save_state()

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
        # The fixed settle below is deliberate. It is not just waiting for the
        # page: it spaces requests out, and Reddit rate-limits this account
        # hard (thousands of 429s per run). Polling instead would pack calls
        # closer together and make the throttling worse, so the pause stays.
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
        self._full_sweep = False

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

    def _wait_out_rate_limit(self) -> None:
        """Reddit 429s the endpoint that carries deletions. If we have just been
        throttled, pause before touching it again."""
        last = getattr(self.b.page, "_rcip_last_429", 0)
        if not last:
            return
        since = time.time() - last
        if since < self.cfg.rate_limit_cooldown_s:
            wait = self.cfg.rate_limit_cooldown_s - since
            LOG.info("    rate limited by Reddit %0.0fs ago - waiting %0.0fs", since, wait)
            self.b.page.wait_for_timeout(int(wait * 1000))

    def _handle_image(self, rid: str, user: str, ev: dict) -> str:
        ev_id = ev["id"]
        url0 = (ev.get("urls") or [""])[0]
        page = self.b.page
        base = dict(room=rid, user=user, event_id=ev_id, url=url0[:200], kind=ev.get("kind", ""))

        def done(outcome: str, **extra) -> str:
            self.audit.write("image", outcome=outcome, **base, **extra)
            bucket = self.unresolved.setdefault(rid, set())
            if outcome in ("deleted", "not-deletable", "would-delete"):
                bucket.discard(ev_id)          # settled, one way or the other
            else:
                bucket.add(ev_id)              # still owed an answer
            return outcome

        # Ownership needs positive evidence either way. Reddit shows Delete on
        # your messages and Report on everyone else's; under rate limiting the
        # toolbar can render incompletely, and treating "no Delete button" as
        # "not yours" would silently skip your own images. So an unreadable
        # toolbar is a failure to retry, never a decision.
        delete_btn = labels = sender = None
        verdict = "unknown"
        for attempt in range(1, self.cfg.ownership_attempts + 1):
            self._wait_out_rate_limit()
            buttons = self._hover_menu(ev_id)
            labels = [(b["label"] or "").strip() for b in buttons]
            low = {x.lower() for x in labels}
            sender = self.b.rcip("eventSender(a)", ev_id)
            mine_by_sender = bool(sender and self._me
                                  and sender.strip().lower() == self._me.strip().lower())

            if "delete" in low:
                delete_btn = next(b for b in buttons
                                  if (b["label"] or "").strip().lower() == "delete")
                verdict = "mine"
                break
            if "report" in low and not mine_by_sender:
                verdict = "theirs"
                break
            if "report" in low and mine_by_sender:
                # contradictory: the sender is us but Delete is missing
                LOG.debug("    attempt %d: toolbar says Report but sender is you", attempt)
            elif not buttons:
                LOG.debug("    attempt %d: no toolbar appeared", attempt)
            else:
                LOG.debug("    attempt %d: toolbar incomplete (%s)", attempt, "/".join(labels))
            if attempt < self.cfg.ownership_attempts:
                page.mouse.move(5, 5)   # force the toolbar to re-render on next hover
                page.wait_for_timeout(self.cfg.ownership_retry_ms * attempt)

        if verdict == "theirs":
            self.stats.images_not_mine += 1
            LOG.info("    - not yours (sent by u/%s) - left alone", sender or "?")
            return done("not-deletable", labels=labels, sender=sender)

        if verdict == "unknown":
            self.stats.failures += 1
            self._room_failures += 1
            LOG.warning("    could not tell whose message this is after %d attempt(s) "
                        "(toolbar: %s, sender: %s) - leaving it and flagging the "
                        "conversation for a rerun",
                        self.cfg.ownership_attempts, "/".join(labels) or "none", sender or "?")
            return done("ownership-unknown", labels=labels, sender=sender)

        if not self.cfg.execute:
            self.stats.images_would_delete += 1
            LOG.info("    [dry-run] WOULD DELETE %s", url0[:80] or ev_id[:14])
            return done("would-delete")

        LOG.info("    deleting %s", url0[:80] or ev_id[:14])

        for attempt in range(1, self.cfg.delete_attempts + 1):
            self._wait_out_rate_limit()
            if attempt > 1:
                # re-hover: the toolbar is gone and the box may have moved
                buttons = self._hover_menu(ev_id) or self._hover_menu(ev_id)
                delete_btn = next((b for b in buttons
                                   if (b["label"] or "").strip().lower() == "delete"), None)
                if not delete_btn:
                    if not self.b.rcip("eventExists(a)", ev_id):
                        self.stats.images_deleted += 1
                        return done("deleted", attempts=attempt)
                    LOG.warning("    retry %d: Delete no longer offered", attempt)
                    break

            bb = delete_btn["box"]
            page.mouse.click(bb["x"] + bb["w"] // 2, bb["y"] + bb["h"] // 2)
            page.wait_for_timeout(self.cfg.dialog_pause_ms)

            confirm = next((b for b in (self.b.rcip("dialogButtons()") or [])
                            if b["text"].strip().lower() in ("yes, delete", "yes delete", "delete")),
                           None)
            if not confirm:
                LOG.warning("    no confirmation button found; leaving the message alone")
                page.keyboard.press("Escape")
                self.stats.failures += 1
                return done("no-confirm-button", attempts=attempt)

            cb = confirm["box"]
            before_429 = getattr(page, "_rcip_429_count", 0)
            page.mouse.click(cb["x"] + cb["w"] // 2, cb["y"] + cb["h"] // 2)

            for _ in range(self.cfg.delete_confirm_polls):
                page.wait_for_timeout(250)
                if not self.b.rcip("eventExists(a)", ev_id):
                    self.stats.images_deleted += 1
                    return done("deleted", attempts=attempt)

            throttled = getattr(page, "_rcip_429_count", 0) > before_429
            if attempt < self.cfg.delete_attempts:
                backoff = self.cfg.delete_retry_backoff_s * attempt * (3 if throttled else 1)
                LOG.warning("    still present after attempt %d%s - retrying in %ds",
                            attempt, " (rate limited)" if throttled else "", backoff)
                page.keyboard.press("Escape")
                page.wait_for_timeout(backoff * 1000)

        self.stats.failures += 1
        self._room_failures += 1
        LOG.warning("    could not delete after %d attempt(s); leaving it in place",
                    self.cfg.delete_attempts)
        return done("delete-unconfirmed", attempts=self.cfg.delete_attempts)
