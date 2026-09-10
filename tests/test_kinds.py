"""`images` and `messages` must select exactly the right things."""
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
    def __init__(self): self.redacted = []; self.calls = 0; self.rate_limited = 0
    def redact(self, room, event_id, reason=None): self.redacted.append(event_id); return "$r"
    def members(self, room): raise AssertionError("delete must not re-fetch members")
    def iter_messages(self, room): raise AssertionError("delete must not re-page history")
    def set_hidden(self, room, hidden=True): pass


MIXED = [Target("$img", "m.image", 1), Target("$vid", "m.video", 2),
         Target("$file", "m.file", 3), Target("$txt1", "m.text", 4),
         Target("$txt2", "m.text", 5), Target("$emote", "m.emote", 6)]


def run(kind, execute=True, tmp=None):
    store = Store(tmp / "s.json")
    store.record_scan("!r:x", ["bob"], 50, [Target(t.event_id, t.msgtype, t.ts) for t in MIXED])
    api = FakeAPI()
    paths = new_run(tmp / "reports"); configure(paths, quiet=True)
    r = Runner(api, store, Audit(paths.audit_file), Options(execute=execute), me="@me:x")
    r.run(["!r:x"], kind, hide=False)
    return api, store, r


tmp = Path(tempfile.mkdtemp())

api, store, r = run(Kind.IMAGES, tmp=tmp / "a")
check("images: removes only attachments", sorted(api.redacted), ["$file", "$img", "$vid"])
check("images: leaves text alone", store.rooms["!r:x"].targets["$txt1"].status, "pending")
check("images: conversation now clean of attachments",
      store.rooms["!r:x"].is_clean(Kind.msgtypes(Kind.IMAGES)), True)
check("images: NOT clean of everything",
      store.rooms["!r:x"].is_clean(Kind.msgtypes(Kind.MESSAGES)), False)

api, store, r = run(Kind.MESSAGES, tmp=tmp / "b")
check("messages: removes everything you sent", len(api.redacted), 6)
check("messages: includes attachments too",
      all(e in api.redacted for e in ("$img", "$vid", "$file")), True)
check("messages: conversation clean of everything",
      store.rooms["!r:x"].is_clean(Kind.msgtypes(Kind.MESSAGES)), True)

api, store, r = run(Kind.MESSAGES, execute=False, tmp=tmp / "c")
check("dry run deletes nothing", api.redacted, [])
check("dry run leaves everything pending",
      all(t.status == "pending" for t in store.rooms["!r:x"].targets.values()), True)
check("dry run still counts what it would do", r.counts.would_delete, 6)

# Cleanliness is judged per kind. (Acting on it - leaving the conversation -
# is disabled because Reddit refuses it; see HIDE_SUPPORTED.)
api, store, r = run(Kind.IMAGES, tmp=tmp / "d")
room = store.rooms["!r:x"]
check("clean of attachments after an images run",
      room.is_clean(Kind.msgtypes(Kind.IMAGES)), True)
check("not clean of everything: text remains",
      room.is_clean(Kind.msgtypes(Kind.MESSAGES)), False)
check("hide --require images: allowed once attachments are gone",
      r.hide_room(room, Kind.IMAGES), True)
api2, store2, r2 = run(Kind.IMAGES, tmp=tmp / "e")
check("hide --require messages: refused while text remains",
      r2.hide_room(store2.rooms["!r:x"], Kind.MESSAGES), False)

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all kind checks passed"))
sys.exit(1 if fails else 0)
