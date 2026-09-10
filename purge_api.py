#!/usr/bin/env python3
"""Delete your own chat attachments (or all your messages) via Reddit's API.

Much faster than driving the browser, and more accurate: ownership comes from
each event's `sender` rather than from which buttons a hover toolbar rendered,
and it sees every conversation instead of whatever the virtualised sidebar
happened to load.

    python purge_api.py                       # dry run over everything
    python purge_api.py --execute
    python purge_api.py --execute --all-messages
    python purge_api.py --execute --leave-when-clean
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from rcip.api import (MatrixClient, Limits, harvest_token, is_media, is_message,
                      is_redacted, members_from_events)
from rcip.config import load_skip_list, normalize_user
from rcip.logging_setup import LOG, Audit, configure, new_run

HERE = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="purge-api", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--execute", action="store_true",
                   help="actually delete. Without this it is a dry run.")
    p.add_argument("--all-messages", action="store_true",
                   help="delete every message you sent, not just attachments. "
                        "Slower than images alone - it is more deletions, not fewer.")
    p.add_argument("--leave-when-clean", action="store_true",
                   help="after a conversation has nothing of yours left, leave and hide it. "
                        "The other person can see that you left, and it cannot be undone.")
    p.add_argument("--skip-users", type=Path, default=HERE / "skip_users.txt")
    p.add_argument("--skip", action="append", default=[], metavar="USER")
    p.add_argument("--max-rooms", type=int)
    p.add_argument("--max-deletes-total", type=int)
    p.add_argument("--min-interval", type=float, default=0.0, metavar="SEC",
                   help="pause between API calls if you want to go gently (default 0; "
                        "the client already obeys the server's own rate-limit instructions)")
    p.add_argument("--profile", type=Path, default=HERE / ".browser-profile")
    p.add_argument("--state", type=Path, default=HERE / "state-api.json")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--reports", type=Path, default=HERE / "reports")
    p.add_argument("--show-window", action="store_true",
                   help="show the browser used to pick up the token")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("-q", "--quiet", action="store_true")
    return p


class Stats:
    def __init__(self):
        self.rooms = self.rooms_skipped = self.rooms_done = self.rooms_left = 0
        self.events = self.mine = self.deleted = self.would = self.failed = 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    paths = new_run(args.reports)
    configure(paths, verbosity=args.verbose, quiet=args.quiet)
    audit = Audit(paths.audit_file)

    skip = load_skip_list(args.skip_users if args.skip_users.exists() else None)
    skip |= {normalize_user(s) for s in args.skip}

    LOG.info("=" * 70)
    LOG.info("run %s | %s | %s", paths.run_id,
             "EXECUTE (permanent)" if args.execute else "DRY RUN",
             "all messages" if args.all_messages else "attachments only")
    if args.execute and not skip:
        LOG.warning("EXECUTE with an EMPTY protected list - every conversation is in scope.")
    LOG.info("=" * 70)

    done: set[str] = set()
    if not args.no_resume and args.state.exists():
        try:
            done = set(json.loads(args.state.read_text()).get("done_rooms", []))
            LOG.info("resuming: %d conversation(s) already clean", len(done))
        except Exception as exc:
            LOG.warning("could not read %s: %s", args.state, exc)

    token, base = harvest_token(args.profile, hidden=not args.show_window)
    client = MatrixClient(token, base, Limits(min_interval_s=args.min_interval),
                          on_reauth=lambda: harvest_token(args.profile,
                                                          hidden=not args.show_window)[0])

    me = client.whoami()["user_id"]
    LOG.info("signed in as %s", me)
    rooms = client.joined_rooms()
    LOG.info("%d conversation(s) visible to the API", len(rooms))
    audit.write("run_start", mode="execute" if args.execute else "dry-run",
                all_messages=args.all_messages, rooms=len(rooms), me=me,
                skip_users=sorted(skip))

    st = Stats()
    t0 = time.time()
    try:
        for i, room in enumerate(rooms, 1):
            if args.max_rooms and st.rooms >= args.max_rooms:
                LOG.info("hit --max-rooms"); break
            if args.max_deletes_total and (st.deleted + st.would) >= args.max_deletes_total:
                LOG.info("hit --max-deletes-total"); break
            if room in done:
                continue
            st.rooms += 1
            try:
                _do_room(client, room, me, skip, args, st, audit, i, len(rooms), done, args.state)
            except Exception as exc:
                st.failed += 1
                LOG.exception("  conversation failed: %s", exc)
                audit.write("room_error", room=room, error=str(exc)[:300])
    except KeyboardInterrupt:
        LOG.warning("interrupted")
    finally:
        dt = time.time() - t0
        LOG.info("-" * 70)
        LOG.info("SUMMARY (%s)", "EXECUTE" if args.execute else "DRY RUN")
        LOG.info("  conversations examined      : %d", st.rooms)
        LOG.info("  conversations skipped       : %d", st.rooms_skipped)
        LOG.info("  conversations now clean     : %d", st.rooms_done)
        if args.leave_when_clean:
            LOG.info("  conversations left/hidden   : %d", st.rooms_left)
        LOG.info("  your messages found         : %d", st.mine)
        LOG.info("  %-27s : %d", "deleted" if args.execute else "would delete",
                 st.deleted if args.execute else st.would)
        LOG.info("  failures                    : %d", st.failed)
        LOG.info("  API calls / rate-limit waits: %d / %d", client.calls, client.rate_limited)
        LOG.info("  elapsed                     : %.1f min (%.2fs per conversation)",
                 dt / 60, dt / max(st.rooms, 1))
        LOG.info("  log: %s", paths.log_file)
        LOG.info("-" * 70)
        audit.write("run_end", **{k: v for k, v in st.__dict__.items()},
                    seconds=round(dt, 1), api_calls=client.calls)
        audit.close()
    return 1 if st.failed else 0


def _do_room(client, room, me, skip, args, st, audit, idx, total, done, state_path) -> None:
    # An authoritative member list costs one call per conversation. Pay it
    # whenever a protected list is in force, since that is a safety decision;
    # otherwise take names from the timeline for free.
    others: set[str] = set()
    if skip:
        members = client.members(room)
        others = {v for k, v in members.items() if k != me and v}
        protected = sorted(n for n in others if normalize_user(n) in skip)
        if protected:
            st.rooms_skipped += 1
            LOG.info("[%d/%d] u/%s -> SKIPPED (protected)", idx, total,
                     "/".join(sorted(others)))
            audit.write("room_skipped", room=room, participants=sorted(others),
                        matched=protected, reason="skip_list")
            return
        if not others:
            st.rooms_skipped += 1
            LOG.warning("[%d/%d] could not identify anyone here and a protected list "
                        "is in force -> SKIPPED", idx, total)
            audit.write("room_skipped", room=room, reason="unidentified_participants")
            return

    targets = []
    seen = 0
    page_events = []
    for ev in client.iter_messages(room):
        seen += 1
        if len(page_events) < 400:
            page_events.append(ev)
        if ev.get("sender") != me or not is_message(ev) or is_redacted(ev):
            continue
        if not args.all_messages and not is_media(ev):
            continue
        targets.append(ev)
    st.events += seen
    st.mine += len(targets)
    if not others:
        others = {v for k, v in members_from_events(page_events).items() if k != me and v}

    who = "/".join(sorted(others)) or "(unknown)"
    if not targets:
        LOG.info("[%d/%d] u/%-24s nothing of yours to remove (%d events)",
                 idx, total, who[:24], seen)
        _finish(client, room, args, st, audit, done, state_path, clean=True)
        return

    LOG.info("[%d/%d] u/%-24s %d of your %s to remove (%d events)", idx, total, who[:24],
             len(targets), "messages" if args.all_messages else "attachments", seen)

    ok = True
    for ev in targets:
        eid = ev["event_id"]
        kind = (ev.get("content") or {}).get("msgtype")
        if not args.execute:
            st.would += 1
            audit.write("message", outcome="would-delete", room=room, event_id=eid,
                        msgtype=kind, participants=sorted(others))
            continue
        try:
            client.redact(room, eid)
            st.deleted += 1
            audit.write("message", outcome="deleted", room=room, event_id=eid, msgtype=kind)
        except Exception as exc:
            ok = False
            st.failed += 1
            LOG.warning("    could not delete %s: %s", eid[:16], exc)
            audit.write("message", outcome="failed", room=room, event_id=eid,
                        msgtype=kind, error=str(exc)[:200])
        if args.max_deletes_total and (st.deleted + st.would) >= args.max_deletes_total:
            return
    _finish(client, room, args, st, audit, done, state_path, clean=ok and args.execute)


def _finish(client, room, args, st, audit, done, state_path, clean: bool) -> None:
    if not clean:
        return
    st.rooms_done += 1
    done.add(room)
    try:
        state_path.write_text(json.dumps({"done_rooms": sorted(done)}, indent=1))
    except Exception as exc:
        LOG.debug("state write failed: %s", exc)
    if args.leave_when_clean and args.execute:
        try:
            client.leave(room)
            client.forget(room)
            st.rooms_left += 1
            audit.write("room_left", room=room)
        except Exception as exc:
            LOG.warning("  could not leave the conversation: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
