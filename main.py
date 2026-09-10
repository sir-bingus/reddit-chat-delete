#!/usr/bin/env python3
"""Clean up your own content in Reddit chat.

Four commands, one shared engine and one shared record of what has been done:

    python main.py scan                    take stock, delete nothing
    python main.py images                  remove attachments you sent
    python main.py messages                remove every message you sent
    python main.py hide                    leave conversations already clean
    python main.py status                  what is left, without touching the network

Every command is a dry run unless you pass --execute.

Deleting is permanent: Reddit's own wording is "removed for everyone in this
chat, you can't undo this". Leaving a conversation is visible to the other
person and cannot be undone either.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rcip.api import Limits, MatrixClient
from rcip.auth import get_token, save_cached
from rcip.config import load_skip_list, normalize_user
from rcip.logging_setup import LOG, Audit, configure, new_run
from rcip.runner import Options, Runner
from rcip.store import Store
from rcip.targets import Kind

HERE = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    g = parent.add_argument_group("what to touch")
    g.add_argument("--execute", action="store_true",
                   help="actually make changes. Without it, nothing is modified.")
    g.add_argument("--skip-users", type=Path, default=HERE / "skip_users.txt",
                   help="file of usernames to leave completely alone "
                        "(default: ./skip_users.txt)")
    g.add_argument("--skip", action="append", default=[], metavar="USER",
                   help="protect one username inline; repeatable")
    g.add_argument("--max-rooms", type=int, metavar="N",
                   help="stop after N conversations")
    g.add_argument("--max-deletes", type=int, metavar="N",
                   help="stop after N deletions - use this for a cautious first run")

    g = parent.add_argument_group("freshness")
    g.add_argument("--rescan", action="store_true",
                   help="re-read history even for conversations already scanned")
    g.add_argument("--rescan-after", type=int, default=0, metavar="HOURS",
                   help="treat scans older than this as stale (default: never)")

    g = parent.add_argument_group("plumbing")
    g.add_argument("--store", type=Path, default=HERE / "chat-records.json",
                   help="the shared record of what is known and done "
                        "(default: ./chat-records.json)")
    g.add_argument("--profile", type=Path, default=HERE / ".browser-profile",
                   help="browser profile holding your Reddit login")
    g.add_argument("--token-cache", type=Path, default=HERE / ".session-token.json",
                   help="where the session token is cached between runs so a browser "
                        "is rarely needed (default: ./.session-token.json, owner-only)")
    g.add_argument("--fresh-token", action="store_true",
                   help="ignore the cached token and harvest a new one")
    g.add_argument("--show-window", action="store_true",
                   help="show the browser used to pick up your session token")
    g.add_argument("--min-interval", type=float, default=0.0, metavar="SEC",
                   help="minimum gap between API calls (default 0; the client already "
                        "obeys the server's own rate-limit instructions)")
    g.add_argument("--reports", type=Path, default=HERE / "reports")
    g.add_argument("-v", "--verbose", action="count", default=0)
    g.add_argument("-q", "--quiet", action="store_true")

    p = argparse.ArgumentParser(prog="reddit-chat-cleanup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("scan", parents=[parent],
                    help="record what exists; change nothing")
    subs.add_parser("images", parents=[parent],
                    help="remove attachments you sent (images, video, files, audio)")
    subs.add_parser("messages", parents=[parent],
                    help="remove every message you sent, attachments included")
    h = subs.add_parser("hide", parents=[parent],
                        help="leave conversations that have nothing of yours left")
    h.add_argument("--require", choices=[Kind.IMAGES, Kind.MESSAGES], default=Kind.MESSAGES,
                   help="what must already be gone before hiding "
                        "(default: everything you sent)")
    for name in ("images", "messages"):
        sp = [a for a in subs.choices[name]._actions if a.dest == "help"]
        del sp
        subs.choices[name].add_argument(
            "--then-hide", action="store_true",
            help="after a conversation is clean, leave and hide it")
    subs.add_parser("status", parents=[parent],
                    help="summarise the stored records; makes no network calls")
    n = subs.add_parser("nuke", parents=[parent],
                        help="the lot: scan, delete every message you sent, then hide "
                             "every conversation. Asks for confirmation.")
    n.add_argument("--yes", action="store_true",
                   help="skip the typed confirmation (for unattended runs)")
    n.add_argument("--keep-conversations", action="store_true",
                   help="delete everything but do not leave/hide the conversations")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    paths = new_run(args.reports)
    configure(paths, verbosity=args.verbose, quiet=args.quiet)
    audit = Audit(paths.audit_file)
    store = Store(args.store)

    if args.command == "status":
        return _status(store)

    skip = load_skip_list(args.skip_users if args.skip_users.exists() else None)
    skip |= {normalize_user(s) for s in args.skip}

    if args.command == "nuke":
        return _nuke(args, skip, paths, audit, store)

    kind = {"images": Kind.IMAGES, "messages": Kind.MESSAGES}.get(args.command)
    hide = args.command == "hide" or getattr(args, "then_hide", False)
    changing = args.execute and (kind or hide)

    LOG.info("=" * 70)
    LOG.info("%s | %s", args.command.upper(),
             "EXECUTE - changes are permanent" if changing else "DRY RUN - nothing changes")
    if kind:
        LOG.info("targeting: %s you sent", Kind.describe(kind))
    if hide:
        LOG.info("will leave conversations once they are clean")
    if changing and not skip:
        LOG.warning("no protected users configured - every conversation is in scope")
    LOG.info("=" * 70)

    opts = Options(execute=args.execute, skip_users=skip, max_rooms=args.max_rooms,
                   max_deletes=args.max_deletes, rescan=args.rescan,
                   rescan_after_s=args.rescan_after * 3600)

    token, base = get_token(args.profile, args.token_cache,
                            hidden=not args.show_window, force=args.fresh_token)

    def reauth() -> str:
        """Called when the server rejects our token mid-run."""
        fresh, fresh_base = get_token(args.profile, args.token_cache,
                                      hidden=not args.show_window, force=True)
        save_cached(args.token_cache, fresh, fresh_base)
        return fresh

    client = MatrixClient(token, base, Limits(min_interval_s=args.min_interval),
                          on_reauth=reauth)
    try:
        me = client.whoami()["user_id"]
    except Exception:
        LOG.info("cached token was not accepted; getting a fresh one")
        client.token = reauth()
        me = client.whoami()["user_id"]
    store.account = me
    LOG.info("signed in as %s", me)

    rooms = client.joined_rooms()
    LOG.info("%d conversation(s) on the account", len(rooms))
    audit.write("run_start", command=args.command, execute=bool(args.execute),
                kind=kind, hide=hide, rooms=len(rooms), account=me,
                skip_users=sorted(skip))

    if args.command == "hide":
        # Hiding never scans on its own: it acts on what we already know, so
        # you cannot accidentally leave a conversation we have not examined.
        known = [r for r in rooms if store.rooms.get(r) and store.rooms[r].scanned_at]
        if len(known) < len(rooms):
            LOG.warning("%d conversation(s) have never been scanned and will be left "
                        "alone; run `scan` first to include them", len(rooms) - len(known))
        rooms = known
        kind = args.require

    runner = Runner(client, store, audit, opts, me)
    try:
        counts = runner.run(
            rooms,
            kind=None if args.command == "hide" else kind,
            hide=hide,
            # `hide` deletes nothing, so its standard comes from --require;
            # --then-hide judges by whatever that run was deleting.
            hide_require=args.require if args.command == "hide" else kind,
        )
    except KeyboardInterrupt:
        LOG.warning("interrupted - progress is saved")
        counts = runner.counts
        store.save()
    finally:
        _summary(args, counts, store, client, kind)
        audit.write("run_end", **counts.__dict__, api_calls=client.calls,
                    rate_limited=client.rate_limited)
        audit.close()
    return 1 if counts.failed else 0


def _nuke(args, skip, paths, audit, store) -> int:
    """scan -> delete every message -> hide, in one command.

    Sequenced rather than reimplemented: each phase is the ordinary command,
    so nothing here can drift from the individually tested behaviour.
    """
    protected = f"{len(skip)} protected user(s) will be left alone" if skip else \
                "NO protected users are configured - nothing is exempt"
    LOG.info("=" * 70)
    LOG.info("NUKE - scan, delete every message you sent%s",
             "" if args.keep_conversations else ", then leave every conversation")
    LOG.info("%s", protected)
    LOG.info("=" * 70)

    if not args.execute:
        LOG.info("This is a DRY RUN. Re-run with --execute to actually do it.")
    elif not args.yes:
        LOG.warning("This permanently deletes every message you have sent in Reddit chat")
        LOG.warning("and cannot be undone. Reddit removes them for everyone in the chat.")
        try:
            typed = input("Type  DELETE EVERYTHING  to continue: ").strip()
        except (EOFError, KeyboardInterrupt):
            typed = ""
        if typed != "DELETE EVERYTHING":
            LOG.info("cancelled - nothing was changed")
            return 1

    phases = [("scan", None, False), ("messages", Kind.MESSAGES, False)]
    if not args.keep_conversations:
        phases.append(("hide", None, True))

    for name, kind, hide in phases:
        LOG.info("")
        LOG.info(">>> phase: %s", name)
        rc = main([name, *_passthrough(args), *(["--execute"] if args.execute else [])])
        if rc not in (0, 1):
            LOG.error("phase %s failed; stopping", name)
            return rc
    return 0


def _passthrough(args) -> list[str]:
    """The flags a nuke phase should inherit from the nuke invocation."""
    out = ["--store", str(args.store), "--profile", str(args.profile),
           "--skip-users", str(args.skip_users), "--reports", str(args.reports),
           "--token-cache", str(args.token_cache)]
    for u in args.skip:
        out += ["--skip", u]
    if args.max_rooms:
        out += ["--max-rooms", str(args.max_rooms)]
    if args.max_deletes:
        out += ["--max-deletes", str(args.max_deletes)]
    if args.min_interval:
        out += ["--min-interval", str(args.min_interval)]
    if args.show_window:
        out.append("--show-window")
    if args.verbose:
        out.append("-" + "v" * args.verbose)
    return out


def _summary(args, counts, store, client, kind) -> None:
    LOG.info("-" * 70)
    LOG.info("SUMMARY (%s)", "EXECUTE" if args.execute else "DRY RUN")
    LOG.info("  conversations seen/scanned/skipped : %d / %d / %d",
             counts.rooms_seen, counts.rooms_scanned, counts.rooms_skipped)
    if kind:
        label = "deleted" if args.execute else "would delete"
        LOG.info("  %-33s: %d", label,
                 counts.deleted if args.execute else counts.would_delete)
        LOG.info("  %-33s: %d", "already gone", counts.already_gone)
        if counts.refused:
            LOG.info("  %-33s: %d", "refused by Reddit", counts.refused)
    if counts.rooms_hidden:
        LOG.info("  %-33s: %d", "conversations left/hidden", counts.rooms_hidden)
    LOG.info("  %-33s: %d", "failures", counts.failed)
    LOG.info("  %-33s: %d / %d", "API calls / rate-limit waits",
             client.calls, client.rate_limited)
    LOG.info("  records: %s", store.path)
    LOG.info("-" * 70)


def _status(store: Store) -> int:
    """Everything we know, without a single network call."""
    if not store.rooms:
        LOG.info("no records yet - run `scan` first")
        return 0
    images = Kind.msgtypes(Kind.IMAGES)
    overall = store.summary()
    img = store.summary(images)
    LOG.info("=" * 70)
    LOG.info("conversations known/scanned/skipped/hidden : %d / %d / %d / %d",
             overall["rooms_known"], overall["rooms_scanned"],
             overall["rooms_skipped"], overall["rooms_hidden"])
    LOG.info("")
    LOG.info("%-12s %10s %10s %8s %8s", "", "pending", "deleted", "gone", "failed")
    LOG.info("%-12s %10d %10d %8d %8d", "attachments", img["pending"], img["deleted"],
             img["gone"], img["failed"])
    LOG.info("%-12s %10d %10d %8d %8d", "all messages", overall["pending"],
             overall["deleted"], overall["gone"], overall["failed"])
    LOG.info("")
    LOG.info("conversations with attachments left : %d", len(store.work(images)))
    LOG.info("conversations with any message left : %d", len(store.work(None)))
    LOG.info("conversations clean of attachments  : %d", len(store.clean_rooms(images)))
    LOG.info("conversations clean of everything   : %d", len(store.clean_rooms(None)))
    LOG.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
