"""The single record of what we know and what we have done.

One file, one source of truth. Every part of the program reads and writes it,
so nothing is ever discovered twice:

  * a scan records, per conversation, who is in it and every message *you*
    sent that still exists - each with its type and current status
  * a deletion run reads those records instead of re-paging history
  * `status` tells you what is left without touching the network at all

A scan deliberately records *all* your messages, not just attachments, even
when the caller only cares about images. Paging the history is the expensive
part and it costs the same either way, so one scan serves an attachments run,
a full-messages run, and the "is this conversation finished" question.

Concurrency: writes take an exclusive lock and re-read first, so several
processes can share one file without losing each other's work.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import LOG

SCHEMA_VERSION = 1

# What a target's `status` can be.
PENDING = "pending"    # still there as far as we know
DELETED = "deleted"    # we redacted it
GONE = "gone"          # it was already absent/redacted when we looked
FAILED = "failed"      # we tried and could not


@dataclass
class Target:
    """One message of yours that a run may want to remove."""
    event_id: str
    msgtype: str                 # m.image, m.video, m.file, m.audio, m.text, ...
    ts: int = 0
    status: str = PENDING
    error: str = ""

    def to_json(self) -> dict:
        d = {"msgtype": self.msgtype, "ts": self.ts, "status": self.status}
        if self.error:
            d["error"] = self.error
        return d

    @staticmethod
    def from_json(event_id: str, d: dict) -> "Target":
        return Target(event_id, d.get("msgtype", ""), d.get("ts", 0),
                      d.get("status", PENDING), d.get("error", ""))


@dataclass
class Room:
    """What we know about one conversation."""
    room_id: str
    participants: list[str] = field(default_factory=list)
    targets: dict[str, Target] = field(default_factory=dict)
    scanned_at: str = ""
    event_count: int = 0
    hidden: bool = False
    skipped: str = ""            # why we deliberately did not touch it

    # ---------------------------------------------------------------- queries

    def pending(self, msgtypes: set[str] | None = None) -> list[Target]:
        """Targets still to remove, optionally narrowed to certain types."""
        return [t for t in self.targets.values()
                if t.status == PENDING and (msgtypes is None or t.msgtype in msgtypes)]

    def failed(self, msgtypes: set[str] | None = None) -> list[Target]:
        return [t for t in self.targets.values()
                if t.status == FAILED and (msgtypes is None or t.msgtype in msgtypes)]

    def is_clean(self, msgtypes: set[str] | None = None) -> bool:
        """True when nothing of yours of these types remains here."""
        return not self.pending(msgtypes) and not self.failed(msgtypes)

    def to_json(self) -> dict:
        return {
            "participants": self.participants,
            "scanned_at": self.scanned_at,
            "event_count": self.event_count,
            "hidden": self.hidden,
            "skipped": self.skipped,
            "targets": {k: v.to_json() for k, v in self.targets.items()},
        }

    @staticmethod
    def from_json(room_id: str, d: dict) -> "Room":
        return Room(
            room_id=room_id,
            participants=list(d.get("participants") or []),
            targets={k: Target.from_json(k, v) for k, v in (d.get("targets") or {}).items()},
            scanned_at=d.get("scanned_at", ""),
            event_count=d.get("event_count", 0),
            hidden=bool(d.get("hidden")),
            skipped=d.get("skipped", "") or "",
        )


class Store:
    """Load/modify/save the record. Call `save()` after a batch of changes."""

    def __init__(self, path: Path):
        self.path = path
        self.account = ""
        self.rooms: dict[str, Room] = {}
        self.load()

    # ------------------------------------------------------------------- io

    def load(self) -> None:
        if not self.path.exists():
            LOG.debug("no store at %s; starting empty", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("store %s is unreadable (%s); starting empty", self.path, exc)
            return
        if raw.get("version") != SCHEMA_VERSION:
            LOG.warning("store %s has version %s, expected %s; starting empty",
                        self.path, raw.get("version"), SCHEMA_VERSION)
            return
        self.account = raw.get("account", "")
        self.rooms = {k: Room.from_json(k, v) for k, v in (raw.get("rooms") or {}).items()}
        LOG.info("loaded records for %d conversation(s) from %s",
                 len(self.rooms), self.path.name)

    def save(self) -> None:
        """Merge into whatever is on disk, under a lock, then replace atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            with open(lock, "a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    disk = {}
                    if self.path.exists():
                        try:
                            d = json.loads(self.path.read_text(encoding="utf-8"))
                            if d.get("version") == SCHEMA_VERSION:
                                disk = d.get("rooms") or {}
                        except Exception:
                            disk = {}
                    merged = dict(disk)
                    for rid, room in self.rooms.items():
                        merged[rid] = room.to_json()   # ours is newer
                    payload = {
                        "version": SCHEMA_VERSION,
                        "account": self.account,
                        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "rooms": merged,
                    }
                    tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
                    os.replace(tmp, self.path)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            LOG.warning("could not save the store: %s", exc)

    # -------------------------------------------------------------- mutation

    def room(self, room_id: str) -> Room:
        return self.rooms.setdefault(room_id, Room(room_id))

    def record_scan(self, room_id: str, participants: list[str], event_count: int,
                    targets: list[Target]) -> Room:
        """Replace what we know about a conversation with a fresh look.

        Statuses already recorded are preserved: a target we deleted stays
        deleted, and anything that has since vanished from the timeline is
        marked `gone` rather than quietly forgotten.
        """
        room = self.room(room_id)
        previous = room.targets
        fresh: dict[str, Target] = {}
        for t in targets:
            old = previous.get(t.event_id)
            if old and old.status in (DELETED, FAILED):
                t.status, t.error = old.status, old.error
            fresh[t.event_id] = t
        for eid, old in previous.items():
            if eid not in fresh and old.status == PENDING:
                old.status = GONE          # no longer in the timeline
                fresh[eid] = old
        room.participants = participants
        room.event_count = event_count
        room.targets = fresh
        room.scanned_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        return room

    def set_status(self, room_id: str, event_id: str, status: str, error: str = "") -> None:
        t = self.room(room_id).targets.get(event_id)
        if t:
            t.status, t.error = status, error

    def mark_skipped(self, room_id: str, reason: str) -> None:
        self.room(room_id).skipped = reason

    def mark_hidden(self, room_id: str) -> None:
        self.room(room_id).hidden = True

    # --------------------------------------------------------------- queries

    def scanned(self) -> list[Room]:
        return [r for r in self.rooms.values() if r.scanned_at]

    def work(self, msgtypes: set[str] | None, include_failed: bool = True) -> list[Room]:
        """Conversations with something of these types still to remove."""
        out = []
        for r in self.rooms.values():
            if r.skipped:
                continue
            if r.pending(msgtypes) or (include_failed and r.failed(msgtypes)):
                out.append(r)
        return out

    def clean_rooms(self, msgtypes: set[str] | None) -> list[Room]:
        """Scanned, not skipped, nothing of yours of these types left."""
        return [r for r in self.rooms.values()
                if r.scanned_at and not r.skipped and r.is_clean(msgtypes)]

    def summary(self, msgtypes: set[str] | None = None) -> dict:
        counts = {PENDING: 0, DELETED: 0, GONE: 0, FAILED: 0}
        for r in self.rooms.values():
            for t in r.targets.values():
                if msgtypes is None or t.msgtype in msgtypes:
                    counts[t.status] = counts.get(t.status, 0) + 1
        return {
            "rooms_known": len(self.rooms),
            "rooms_scanned": len(self.scanned()),
            "rooms_skipped": sum(1 for r in self.rooms.values() if r.skipped),
            "rooms_hidden": sum(1 for r in self.rooms.values() if r.hidden),
            **counts,
        }
