"""End-to-end test against a fixture replicating Reddit's real chat DOM.

Covers the things that would be expensive to get wrong on a live account:
  * conversations enumerated from a virtualised sidebar
  * scroll-back that pulls in lazily-loaded older history
  * ownership read from the hover toolbar (Delete = yours, Report = theirs)
  * the protected list, including a group chat protected via a second member
  * dry run deletes nothing; execute deletes exactly the deletable images
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rcip.browser import Browser  # noqa: E402
from rcip.config import Config  # noqa: E402
from rcip.logging_setup import Audit, configure, new_run  # noqa: E402
from rcip.purge import Purger, Stats  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixture_chat.html").resolve().as_uri()
ROOM_URL = FIXTURE + "?room={}"

# fixture: alice 3 own + 2 older own + 1 theirs | bob 2 own | carol(group) 1 own + 1 theirs
OWN_ALL = 8
OWN_NO_BOB = 6
OTHERS_NO_BOB = 2
OWN_NO_GROUP = 7
OTHERS_NO_GROUP = 1


def run_from_list(rooms: list[dict], tmp: Path, execute: bool = False) -> Stats:
    """Drive conversations straight from a cached list, by URL."""
    paths = new_run(tmp / "reports")
    configure(paths, verbosity=2)
    audit = Audit(paths.audit_file)
    cfg = Config(execute=execute, headless=True, profile_dir=tmp / "profile",
                 delete_delay_ms=40, scroll_pause_ms=90, room_settle_ms=900,
                 hover_pause_ms=250, dialog_pause_ms=350, max_scroll_rounds=60,
                 room_url_template=ROOM_URL, state_file=None, open_by_click=False,
                 delete_retry_backoff_s=0, delete_confirm_polls=4)
    stats = Stats()
    with Browser(cfg.profile_dir, headless=True, chat_url=FIXTURE) as b:
        b.open_chat()
        assert b.wait_for_login(20), "fixture sidebar did not render"
        Purger(b, cfg, audit, paths, stats).run_list(rooms)
    audit.close()
    return stats


def run(execute: bool, skip: set[str], tmp: Path, only: list[str] | None = None,
        state_file: Path | None = None) -> Stats:
    paths = new_run(tmp / "reports")
    configure(paths, verbosity=2)
    audit = Audit(paths.audit_file)
    cfg = Config(execute=execute, skip_users=skip, headless=True,
                 profile_dir=tmp / "profile", delete_delay_ms=40, scroll_pause_ms=90,
                 sidebar_pause_ms=60, room_settle_ms=900, hover_pause_ms=250,
                 dialog_pause_ms=350, max_scroll_rounds=60, max_sidebar_rounds=12,
                 room_url_template=ROOM_URL, state_file=state_file,
                 only_rooms=only or [], delete_retry_backoff_s=0,
                 delete_confirm_polls=4)
    stats = Stats()
    with Browser(cfg.profile_dir, headless=True, chat_url=FIXTURE) as b:
        b.open_chat()
        assert b.wait_for_login(20), "fixture sidebar did not render"
        Purger(b, cfg, audit, paths, stats).run()
    audit.close()
    return stats


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rcip-test-"))
    failures = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
        if not ok:
            failures.append(name)

    try:
        print("\n=== 1. dry run, protecting u/bob_y ===")
        s = run(False, {"bob_y"}, tmp / "a")
        check("conversations found", s.rooms_listed, 3)
        check("conversations skipped", s.rooms_skipped, 1)
        check("conversations processed", s.rooms_processed, 2)
        check("would delete (incl. lazy-loaded history)", s.images_would_delete, OWN_NO_BOB)
        check("actually deleted (must be 0)", s.images_deleted, 0)
        check("left alone (not yours)", s.images_not_mine, OTHERS_NO_BOB)
        check("failures", s.failures, 0)

        print("\n=== 2. dry run, protecting u/dave_w (2nd member of the group chat) ===")
        s = run(False, {"dave_w"}, tmp / "b")
        check("group conversation skipped", s.rooms_skipped, 1)
        check("would delete", s.images_would_delete, OWN_NO_GROUP)
        check("left alone (not yours)", s.images_not_mine, OTHERS_NO_GROUP)
        check("failures", s.failures, 0)

        print("\n=== 3. execute, protecting u/bob_y ===")
        s = run(True, {"bob_y"}, tmp / "c")
        check("deleted", s.images_deleted, OWN_NO_BOB)
        check("left alone (not yours)", s.images_not_mine, OTHERS_NO_BOB)
        check("conversations skipped", s.rooms_skipped, 1)
        check("failures", s.failures, 0)

        print("\n=== 4. execute, no protected list ===")
        s = run(True, set(), tmp / "d")
        check("deleted", s.images_deleted, OWN_ALL)
        check("failures", s.failures, 0)
        print("\n=== 5. a delete that fails once then succeeds (rate limiting) ===")
        s = run(True, set(), tmp / "e", only=["flaky_f"])
        check("deleted after retry", s.images_deleted, 1)
        check("failures", s.failures, 0)

        print("\n=== 6. a delete that never takes ===")
        state = tmp / "f" / "state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        s = run(True, set(), tmp / "f", only=["stuck_s"], state_file=state)
        check("deleted", s.images_deleted, 0)
        check("reported as a failure", s.failures, 1)
        done = json.loads(state.read_text())["done_rooms"] if state.exists() else []
        check("conversation NOT marked done (so a rerun retries it)",
              "!stuck:reddit.com" in done, False)
        print("\n=== 7. toolbar renders without Delete/Report (rate limiting) ===")
        state7 = tmp / "g" / "state.json"
        state7.parent.mkdir(parents=True, exist_ok=True)
        s = run(True, set(), tmp / "g", only=["throt_t"], state_file=state7)
        check("NOT written off as someone else's", s.images_not_mine, 0)
        check("deleted (must not delete what it cannot identify)", s.images_deleted, 0)
        check("flagged as a failure instead", s.failures, 1)
        done7 = json.loads(state7.read_text())["done_rooms"] if state7.exists() else []
        check("conversation left for a rerun", "!throttled:reddit.com" in done7, False)
        print("\n=== 8. driving from a cached list by URL (no sidebar walk) ===")
        cached = [{"room": "!alice:reddit.com", "user": "alice_x"},
                  {"room": "!bob:reddit.com", "user": "bob_y"}]
        s = run_from_list(cached, tmp / "h", execute=True)
        check("both cached conversations processed", s.rooms_processed, 2)
        check("deleted (alice 5 + bob 2)", s.images_deleted, 7)
        check("failures", s.failures, 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
