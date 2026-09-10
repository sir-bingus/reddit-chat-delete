"""What a second run does: what it retries, skips, and leaves alone.

These are the semantics documented in the README under "What a second run
does", so they are pinned here.
"""
import sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rcip.logging_setup import Audit, configure, new_run
from rcip.runner import Options, Runner
from rcip.store import Store, Target, DELETED, FAILED, GONE, PENDING
from rcip.targets import Kind

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
    if not ok: fails.append(name)


class API:
    def __init__(self): self.redacted, self.hidden, self.scanned = [], [], []
    def redact(self, room, event_id, reason=None): self.redacted.append(event_id); return "$r"
    def set_hidden(self, room, hidden=True): self.hidden.append(room)
    def members(self, room): return {"@them:x": "bob"}
    def iter_messages(self, room):
        self.scanned.append(room)
        return iter([{"type": "m.room.message", "event_id": "$new", "sender": "@me:x",
                      "content": {"msgtype": "m.text", "body": "hi"},
                      "origin_server_ts": 1}])
    calls = rate_limited = 0


def make(tmp, skip=frozenset()):
    api = API()
    store = Store(tmp / "s.json")
    paths = new_run(tmp / "r"); configure(paths, quiet=True)
    return api, store, Runner(api, store, Audit(paths.audit_file),
                              Options(execute=True, skip_users=set(skip)), me="@me:x")


base = Path(tempfile.mkdtemp())

# a scanned conversation is not re-scanned
tmp = base / "a"; tmp.mkdir(parents=True)
api, store, r = make(tmp)
r.run(["!a:x"], None, hide=False)
check("first run scans", api.scanned, ["!a:x"])
api2, store2, r2 = make(tmp)
store2.rooms = store.rooms
r2.run(["!a:x"], None, hide=False)
check("second run does not re-scan", api2.scanned, [])

# statuses decide what a rerun retries
tmp = base / "b"; tmp.mkdir(parents=True)
api, store, r = make(tmp)
store.record_scan("!a:x", ["bob"], 4, [
    Target("$p", "m.text", 1), Target("$d", "m.text", 2),
    Target("$g", "m.text", 3), Target("$f", "m.text", 4)])
store.set_status("!a:x", "$d", DELETED)
store.set_status("!a:x", "$g", GONE)
store.set_status("!a:x", "$f", FAILED, "timed out")
r.run(["!a:x"], Kind.MESSAGES, hide=False)
check("retries pending and failed, not deleted or gone",
      sorted(api.redacted), ["$f", "$p"])

# an already-hidden conversation is not hidden twice
tmp = base / "c"; tmp.mkdir(parents=True)
api, store, r = make(tmp)
store.record_scan("!a:x", ["bob"], 1, [])
store.mark_hidden("!a:x")
r.run(["!a:x"], None, hide=True)
check("does not re-hide", api.hidden, [])

# removing a name from the protected list lets the conversation back in
tmp = base / "d"; tmp.mkdir(parents=True)
api, store, r = make(tmp, skip={"bob"})
r.run(["!a:x"], Kind.MESSAGES, hide=False)
check("protected on the first run", api.redacted, [])
check("recorded as protected", store.rooms["!a:x"].skipped.startswith("protected"), True)

api2, store2, r2 = make(tmp, skip=set())      # bob removed from the list
store2.rooms = store.rooms
r2.run(["!a:x"], Kind.MESSAGES, hide=False)
check("no longer protected once the name is removed", len(api2.redacted) > 0, True)

# ...but stays protected while the name is still listed
tmp = base / "e"; tmp.mkdir(parents=True)
api, store, r = make(tmp, skip={"bob"})
r.run(["!a:x"], Kind.MESSAGES, hide=False)
api2, store2, r2 = make(tmp, skip={"bob"})
store2.rooms = store.rooms
r2.run(["!a:x"], Kind.MESSAGES, hide=False)
check("still protected while the name remains listed", api2.redacted, [])

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all rerun checks passed"))
sys.exit(1 if fails else 0)
