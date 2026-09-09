#!/usr/bin/env python3
"""Bulk-delete your own image attachments across every Reddit chat conversation.

Dry-run by default. Nothing is deleted until you pass --execute.

    python main.py                       # scan and report, delete nothing
    python main.py --skip-users skip_users.txt
    python main.py --execute --skip-users skip_users.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from rcip.browser import Browser, BrowserClosed
from rcip.config import Config, load_skip_list
from rcip.enumerate import enumerate_rooms
from rcip.logging_setup import LOG, Audit, attach_page_logging, configure, dump_artifacts, new_run
from rcip.parallel import run_workers
from rcip.purge import Purger, Stats

HERE = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reddit-chat-image-purge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--execute", action="store_true",
                   help="actually delete. Without this the run is a dry run.")
    p.add_argument("--skip-users", type=Path, default=HERE / "skip_users.txt",
                   help="file of usernames whose conversations are left untouched "
                        "(default: ./skip_users.txt)")
    p.add_argument("--skip", action="append", default=[], metavar="USER",
                   help="protect a single username; repeatable, merged with --skip-users")
    p.add_argument("--only", action="append", default=[], metavar="REGEX",
                   help="only process conversations whose label matches this regex; repeatable")
    p.add_argument("--max-rooms", type=int, help="stop after this many conversations")
    p.add_argument("--max-deletes-per-room", type=int)
    p.add_argument("--max-deletes-total", type=int)
    p.add_argument("--delay", type=int, default=1200, metavar="MS",
                   help="pause after each deletion (default 1200ms; be kind to Reddit)")
    p.add_argument("--scroll-pause", type=int, default=700, metavar="MS")
    p.add_argument("--max-scroll-rounds", type=int, default=400)
    p.add_argument("--headless", action="store_true",
                   help="run without a visible window (only once you are logged in)")
    p.add_argument("--profile", type=Path, default=HERE / ".browser-profile",
                   help="persistent browser profile directory")
    p.add_argument("--login-timeout", type=int, default=300, metavar="SECONDS")
    p.add_argument("--keep-open", action="store_true",
                   help="leave the browser open at the end (handy while debugging)")
    p.add_argument("--inspect", action="store_true",
                   help="dump the page structure and exit; use this to fix selectors "
                        "after Reddit changes its markup")
    p.add_argument("--state", type=Path, default=HERE / "state.json",
                   help="progress file so an interrupted run resumes where it stopped "
                        "(default: ./state.json; use --no-resume to ignore it)")
    p.add_argument("--no-resume", action="store_true",
                   help="ignore the progress file and revisit every conversation")
    p.add_argument("--room-settle", type=int, default=4000, metavar="MS",
                   help="how long to wait for a conversation to render (default 4000ms)")
    p.add_argument("--workers", type=int, default=1, metavar="N",
                   help="run N browser windows in parallel, each on its own shard of "
                        "conversations (default 1). Takes a one-off census first so the "
                        "work can be split by conversation id.")
    p.add_argument("--rooms-file", type=Path,
                   help="internal: process exactly the conversations in this JSON file "
                        "(used for worker shards)")
    p.add_argument("--census-only", type=Path, metavar="FILE",
                   help="write the full conversation list to FILE and exit")
    p.add_argument("--reports", type=Path, default=HERE / "reports")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v for debug on the console (everything is in the log file regardless)")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    paths = new_run(args.reports)
    configure(paths, verbosity=args.verbose, quiet=args.quiet)
    audit = Audit(paths.audit_file)

    skip = load_skip_list(args.skip_users if args.skip_users.exists() else None)
    from rcip.config import normalize_user
    skip |= {normalize_user(s) for s in args.skip}

    cfg = Config(
        execute=args.execute,
        skip_users=skip,
        only_rooms=args.only,
        max_rooms=args.max_rooms,
        max_deletes_per_room=args.max_deletes_per_room,
        max_deletes_total=args.max_deletes_total,
        delete_delay_ms=args.delay,
        scroll_pause_ms=args.scroll_pause,
        max_scroll_rounds=args.max_scroll_rounds,
        headless=args.headless,
        profile_dir=args.profile,
        login_timeout_s=args.login_timeout,
        keep_open=args.keep_open,
        room_settle_ms=args.room_settle,
        state_file=None if args.no_resume else args.state,
        open_by_click=not args.rooms_file,
    )

    LOG.info("=" * 72)
    LOG.info("run %s | mode=%s | log=%s", paths.run_id,
             "EXECUTE (deletions are permanent)" if cfg.execute else "DRY RUN",
             paths.log_file)
    if cfg.execute and not skip:
        LOG.warning("EXECUTE mode with an EMPTY protected-user list - every conversation "
                    "is in scope. Ctrl-C now if that is not what you want.")
    LOG.info("=" * 72)
    audit.write("run_start", mode="execute" if cfg.execute else "dry-run",
                skip_users=sorted(skip), argv=sys.argv[1:])

    stats = Stats()

    try:
        with Browser(cfg.profile_dir, headless=cfg.headless) as browser:
            attach_page_logging(browser.page, audit)
            browser.open_chat()

            if not browser.wait_for_login(cfg.login_timeout_s):
                dump_artifacts(browser.page, paths, "login")
                audit.write("run_abort", reason="not-logged-in")
                return 2

            if args.census_only is not None:
                rooms = enumerate_rooms(browser, cfg, audit)
                args.census_only.write_text(json.dumps(rooms, indent=1), encoding="utf-8")
                LOG.info("wrote %d conversation(s) to %s", len(rooms), args.census_only)
                return 0

            if args.inspect:
                info = browser.rcip("describe()")
                out = paths.dir / "inspect.json"
                out.write_text(json.dumps(info, indent=2), encoding="utf-8")
                LOG.info("page structure written to %s", out)
                print(json.dumps(info, indent=2))
                dump_artifacts(browser.page, paths, "inspect")
                return 0

            purger = Purger(browser, cfg, audit, paths, stats)
            if args.rooms_file:
                shard_rooms = json.loads(args.rooms_file.read_text(encoding="utf-8"))
                LOG.info("worker mode: %d conversation(s) in this shard", len(shard_rooms))
                purger.run_list(shard_rooms)
            elif args.workers > 1:
                rooms = enumerate_rooms(browser, cfg, audit, limit=args.max_rooms)
                if not rooms:
                    LOG.error("census found no conversations; aborting")
                    return 2
                (paths.dir / "rooms.json").write_text(json.dumps(rooms, indent=1),
                                                      encoding="utf-8")
                LOG.info("census complete: %d conversation(s); splitting across %d worker(s)",
                         len(rooms), args.workers)
                worker_argv = _worker_argv(args)
                browser.__exit__(None, None, None)   # free the profile before cloning
                totals = run_workers(rooms, args.workers, paths, worker_argv, cfg.profile_dir)
                for k, v in totals.items():
                    setattr(stats, k, v)
                stats.rooms_listed = len(rooms)
                return 1 if stats.failures else 0
            else:
                purger.run()

            if cfg.keep_open:
                LOG.info("--keep-open: press Enter in this terminal to close the browser")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
    except KeyboardInterrupt:
        LOG.warning("interrupted by user")
        audit.write("run_interrupted")
    except BrowserClosed as exc:
        LOG.warning("run stopped: %s", exc)
        audit.write("run_abort", reason="browser-closed")
    except PlaywrightError as exc:
        if Browser._closed_error(exc):
            LOG.warning("run stopped: the browser window was closed")
            audit.write("run_abort", reason="browser-closed")
        else:
            LOG.exception("playwright error: %s", exc)
            audit.write("run_error", error=str(exc)[:500])
            raise
    finally:
        try:
            (paths.dir / "stats.json").write_text(json.dumps(stats.__dict__, default=list,
                                                             indent=1), encoding="utf-8")
        except Exception:
            pass
        _summary(stats, cfg, paths)
        audit.write("run_end", **stats.__dict__)
        audit.close()

    return 1 if stats.failures else 0


def _worker_argv(args) -> list[str]:
    """Flags that every worker should inherit (profile/reports/state are per worker)."""
    argv: list[str] = []
    if args.execute:
        argv.append("--execute")
    if args.skip_users:
        argv += ["--skip-users", str(args.skip_users)]
    for u in args.skip:
        argv += ["--skip", u]
    for o in args.only:
        argv += ["--only", o]
    if args.max_deletes_per_room:
        argv += ["--max-deletes-per-room", str(args.max_deletes_per_room)]
    argv += ["--delay", str(args.delay), "--scroll-pause", str(args.scroll_pause),
             "--room-settle", str(args.room_settle),
             "--max-scroll-rounds", str(args.max_scroll_rounds)]
    if args.verbose:
        argv.append("-" + "v" * args.verbose)
    return argv


def _summary(stats: Stats, cfg: Config, paths) -> None:
    LOG.info("-" * 72)
    LOG.info("SUMMARY (%s)", "EXECUTE" if cfg.execute else "DRY RUN")
    LOG.info("  conversations found                  : %d", stats.rooms_listed)
    LOG.info("  conversations seen/processed/skipped : %d / %d / %d",
             stats.rooms_seen, stats.rooms_processed, stats.rooms_skipped)
    LOG.info("  images found                         : %d", stats.images_found)
    if cfg.execute:
        LOG.info("  images deleted                       : %d", stats.images_deleted)
    else:
        LOG.info("  images that WOULD be deleted         : %d", stats.images_would_delete)
    LOG.info("  images left alone (not yours)        : %d", stats.images_not_mine)
    LOG.info("  failures                             : %d", stats.failures)
    for e in stats.errors[:10]:
        LOG.info("    ! %s", e)
    LOG.info("  full log   : %s", paths.log_file)
    LOG.info("  audit trail: %s", paths.audit_file)
    LOG.info("-" * 72)


if __name__ == "__main__":
    raise SystemExit(main())
