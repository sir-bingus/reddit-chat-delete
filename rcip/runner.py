"""The one engine. Every mode is a thin layer over the same three operations.

    scan   - look at conversations and record what is there
    delete - remove recorded targets of a given kind
    hide    - leave conversations that have nothing of yours left

Deliberately shaped so nothing is done twice: `delete` never pages history
itself, it works from what `scan` recorded, and `hide` decides from the same
records rather than looking again. Running `delete` on an unscanned
conversation scans it first, so a single command still works end to end.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .api import MatrixClient
from .config import normalize_user
from .logging_setup import LOG, Audit
from .store import DELETED, FAILED, GONE, Store, Target
from .targets import Kind, deletable, msgtype_of


@dataclass
class Options:
    execute: bool = False
    skip_users: set[str] = field(default_factory=set)
    max_rooms: int | None = None
    max_deletes: int | None = None
    rescan: bool = False          # re-page history even if we scanned before
    rescan_after_s: int = 0       # treat scans older than this as stale (0 = never)


@dataclass
class Counters:
    rooms_seen: int = 0
    rooms_skipped: int = 0
    rooms_scanned: int = 0
    rooms_hidden: int = 0
    events_read: int = 0
    deleted: int = 0
    would_delete: int = 0
    failed: int = 0
    already_gone: int = 0


class Runner:
    def __init__(self, client: MatrixClient, store: Store, audit: Audit,
                 opts: Options, me: str):
        self.api = client
        self.store = store
        self.audit = audit
        self.opts = opts
        self.me = me
        self.counts = Counters()

    # ------------------------------------------------------------------ scan

    def _needs_scan(self, room_id: str) -> bool:
        room = self.store.rooms.get(room_id)
        if room is None or not room.scanned_at:
            return True
        if self.opts.rescan:
            return True
        if self.opts.rescan_after_s:
            age = time.time() - time.mktime(time.strptime(room.scanned_at,
                                                          "%Y-%m-%dT%H:%M:%S"))
            return age > self.opts.rescan_after_s
        return False

    def _protected(self, participants: list[str]) -> list[str]:
        return sorted(p for p in participants if normalize_user(p) in self.opts.skip_users)

    def _allowed(self, room) -> bool:
        """False if this conversation must be left alone.

        Checked afresh on every run against the stored participants, so the
        protected list applies to conversations scanned before a name was
        added to it. A conversation whose participants we never established
        is refused while any protected list is in force.
        """
        hit = self._protected(room.participants)
        if hit:
            self.store.mark_skipped(room.room_id, f"protected:{','.join(hit)}")
            self.counts.rooms_skipped += 1
            LOG.info("  SKIPPED - protected: %s", ", ".join(hit))
            self.audit.write("room_skipped", room=room.room_id, matched=hit,
                             participants=room.participants, reason="skip_list_recheck")
            return False
        if not room.participants:
            self.counts.rooms_skipped += 1
            LOG.warning("  SKIPPED - participants unknown and a protected list is in force")
            self.audit.write("room_skipped", room=room.room_id,
                             reason="unidentified_participants")
            return False
        return True

    def scan_room(self, room_id: str) -> object:
        """Page a conversation's history and record every message of yours.

        Records all types, not only the kind the caller wants, so this never
        has to be repeated for a different mode.
        """
        participants: list[str] = []
        if self.opts.skip_users:
            # An authoritative member list is a safety decision, so pay for it
            # whenever a protected list is in force.
            members = self.api.members(room_id)
            participants = sorted({v for k, v in members.items() if k != self.me and v})
            hit = self._protected(participants)
            if hit:
                self.store.mark_skipped(room_id, f"protected:{','.join(hit)}")
                self.counts.rooms_skipped += 1
                LOG.info("  SKIPPED - protected: %s", ", ".join(hit))
                self.audit.write("room_skipped", room=room_id, matched=hit,
                                 participants=participants, reason="skip_list")
                return None
            if not participants:
                self.store.mark_skipped(room_id, "unidentified")
                self.counts.rooms_skipped += 1
                LOG.warning("  SKIPPED - could not identify anyone, and a protected "
                            "list is in force")
                self.audit.write("room_skipped", room=room_id, reason="unidentified")
                return None

        found: list[Target] = []
        seen = 0
        names: dict[str, str] = {}
        for ev in self.api.iter_messages(room_id):
            seen += 1
            if ev.get("type") == "m.room.member":
                name = (ev.get("content") or {}).get("displayname")
                if name and ev.get("state_key") != self.me:
                    names[ev["state_key"]] = name
            if deletable(ev, self.me):
                found.append(Target(ev["event_id"], msgtype_of(ev),
                                    int(ev.get("origin_server_ts") or 0)))
        if not participants:
            participants = sorted(set(names.values()))

        self.counts.events_read += seen
        self.counts.rooms_scanned += 1
        room = self.store.record_scan(room_id, participants, seen, found)
        self.audit.write("room_scanned", room=room_id, participants=participants,
                         events=seen, mine=len(found))
        return room

    def room_for(self, room_id: str):
        """The stored record, scanning first only if we have to."""
        if self._needs_scan(room_id):
            return self.scan_room(room_id)
        return self.store.rooms.get(room_id)

    # ---------------------------------------------------------------- delete

    def delete_in(self, room, kind: str) -> None:
        """Remove this conversation's pending targets of the requested kind."""
        msgtypes = Kind.msgtypes(kind)
        pending = room.pending(msgtypes) + room.failed(msgtypes)
        if not pending:
            return
        LOG.info("  %d %s to remove", len(pending), Kind.describe(kind))
        for target in pending:
            if self.opts.max_deletes and self._done() >= self.opts.max_deletes:
                LOG.info("  reached the deletion limit")
                return
            if not self.opts.execute:
                self.counts.would_delete += 1
                self.audit.write("target", outcome="would-delete", room=room.room_id,
                                 event_id=target.event_id, msgtype=target.msgtype)
                continue
            try:
                self.api.redact(room.room_id, target.event_id)
                self.store.set_status(room.room_id, target.event_id, DELETED)
                self.counts.deleted += 1
                self.audit.write("target", outcome="deleted", room=room.room_id,
                                 event_id=target.event_id, msgtype=target.msgtype)
            except Exception as exc:
                msg = str(exc)[:200]
                # Already gone is a success, not a failure.
                if "M_NOT_FOUND" in msg or "not found" in msg.lower():
                    self.store.set_status(room.room_id, target.event_id, GONE)
                    self.counts.already_gone += 1
                    continue
                self.store.set_status(room.room_id, target.event_id, FAILED, msg)
                self.counts.failed += 1
                LOG.warning("    could not delete %s: %s", target.event_id[:16], msg)
                self.audit.write("target", outcome="failed", room=room.room_id,
                                 event_id=target.event_id, msgtype=target.msgtype,
                                 error=msg)

    def _done(self) -> int:
        return self.counts.deleted + self.counts.would_delete

    # ------------------------------------------------------------------ hide

    def hide_room(self, room, kind: str) -> bool:
        """Leave a conversation, but only when nothing of yours is left in it."""
        if room.hidden:
            return False
        if not room.is_clean(Kind.msgtypes(kind)):
            LOG.debug("  not hiding %s: still has %d item(s) of yours",
                      room.room_id[:20], len(room.pending(Kind.msgtypes(kind))))
            return False
        if not self.opts.execute:
            LOG.info("  [dry run] would leave and hide this conversation")
            self.audit.write("room_hide", room=room.room_id, outcome="would-hide")
            return False
        try:
            self.api.leave(room.room_id)
            self.api.forget(room.room_id)
            self.store.mark_hidden(room.room_id)
            self.counts.rooms_hidden += 1
            LOG.info("  left and hid this conversation")
            self.audit.write("room_hide", room=room.room_id, outcome="hidden")
            return True
        except Exception as exc:
            self.counts.failed += 1
            LOG.warning("  could not leave: %s", str(exc)[:160])
            self.audit.write("room_hide", room=room.room_id, outcome="failed",
                             error=str(exc)[:200])
            return False

    # ------------------------------------------------------------------- run

    def run(self, room_ids: list[str], kind: str | None, hide: bool,
            hide_require: str | None = None) -> Counters:
        """Walk conversations once, doing whatever this invocation asks for.

        `kind` is what to delete (None = delete nothing). `hide_require` is
        what must already be gone before a conversation may be left; it is
        separate from `kind` because `hide` on its own deletes nothing yet
        still needs to know the standard to judge against.
        """
        if hide:
            LOG.info("hiding requires: nothing of yours left of type '%s'",
                     hide_require or kind or Kind.MESSAGES)
        total = len(room_ids)
        for index, room_id in enumerate(room_ids, 1):
            if self.opts.max_rooms and self.counts.rooms_seen >= self.opts.max_rooms:
                LOG.info("reached the conversation limit")
                break
            if self.opts.max_deletes and self._done() >= self.opts.max_deletes:
                LOG.info("reached the deletion limit")
                break
            self.counts.rooms_seen += 1

            known = self.store.rooms.get(room_id)
            if known is not None and known.skipped:
                self.counts.rooms_skipped += 1
                continue

            room = self.room_for(room_id)
            if room is None:            # skipped during the scan
                continue

            # Re-check protection every run, not just at scan time. A name
            # added to the list after a conversation was scanned must still
            # protect it, and stored records would otherwise sail straight
            # past the check.
            if self.opts.skip_users and not self._allowed(room):
                continue

            who = ", ".join(room.participants) or "(unknown)"
            pending = len(room.pending(Kind.msgtypes(kind))) if kind else 0
            LOG.info("[%d/%d] u/%-24s %s", index, total, who[:24],
                     f"{pending} {Kind.describe(kind)} pending" if kind
                     else f"{len(room.targets)} of your messages on record")

            if kind:
                self.delete_in(room, kind)
            if hide:
                self.hide_room(room, hide_require or kind or Kind.MESSAGES)

            if index % 25 == 0:
                self.store.save()
        self.store.save()
        return self.counts
