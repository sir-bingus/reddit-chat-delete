"""The protected list must hold at delete time, not just at scan time."""
import sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rcip.logging_setup import Audit, configure, new_run
from rcip.runner import Options, Runner
from rcip.store import Store, Target
from rcip.targets import Kind

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
    if not ok: fails.append(name)


class FakeAPI:
    """Records redactions instead of performing them."""
    def __init__(self):
        self.redacted, self.left = [], []
        self.calls = self.rate_limited = 0
    def redact(self, room, event_id, reason=None):
        self.redacted.append((room, event_id)); return "$r"
    def members(self, room): raise AssertionError("should not re-fetch members")
    def iter_messages(self, room): raise AssertionError("should not re-page history")
    def set_hidden(self, room, hidden=True): self.left.append(room)


def build(skip, participants, tmp):
    store = Store(tmp / "s.json")
    store.record_scan("!r:x", participants, 10, [Target("$img", "m.image", 1)])
    api = FakeAPI()
    paths = new_run(tmp / "reports"); configure(paths, quiet=True)
    runner = Runner(api, store, Audit(paths.audit_file),
                    Options(execute=True, skip_users=skip), me="@me:x")
    return api, store, runner


tmp = Path(tempfile.mkdtemp())

# scanned before the name was protected; the name is protected now
api, store, runner = build({"alice"}, ["alice"], tmp / "a")
runner.run(["!r:x"], Kind.IMAGES, hide=False)
check("protected at delete time even though scanned earlier", api.redacted, [])
check("recorded as skipped", store.rooms["!r:x"].skipped.startswith("protected"), True)
check("its image is untouched", store.rooms["!r:x"].targets["$img"].status, "pending")

# an unprotected conversation still gets cleaned
api, store, runner = build({"someone_else"}, ["bob"], tmp / "b")
runner.run(["!r:x"], Kind.IMAGES, hide=False)
check("unprotected conversation is processed", len(api.redacted), 1)
check("its image is recorded deleted", store.rooms["!r:x"].targets["$img"].status, "deleted")

# participants unknown + a protected list in force -> refuse
api, store, runner = build({"alice"}, [], tmp / "c")
runner.run(["!r:x"], Kind.IMAGES, hide=False)
check("unknown participants are refused while a list is in force", api.redacted, [])

# no protected list at all -> proceed
api, store, runner = build(set(), [], tmp / "d")
runner.run(["!r:x"], Kind.IMAGES, hide=False)
check("no protected list means no restriction", len(api.redacted), 1)

# hide refuses while content remains - and, for now, refuses regardless
api, store, runner = build(set(), ["bob"], tmp / "e")
store.room("!r:x").targets["$img"].status = "pending"
runner.opts.execute = True
check("will not hide a conversation that still has your content",
      runner.hide_room(store.room("!r:x"), Kind.MESSAGES), False)
store.room("!r:x").targets["$img"].status = "deleted"
check("hides once it is clean", runner.hide_room(store.room("!r:x"), Kind.MESSAGES), True)
check("the hide was applied to that conversation", api.left, ["!r:x"])

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all protection checks passed"))
sys.exit(1 if fails else 0)
