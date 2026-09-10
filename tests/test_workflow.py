"""Do the commands compose? Run individually, in sequence, and after a stop.

Covers the questions that matter for real use:
  * do images / messages / hide all honour the same protected list
  * can each be used on its own
  * does running them in sequence do the right thing
  * does stopping mid-run and restarting resume rather than redo
"""
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
    def __init__(self, fail_after=None):
        self.redacted, self.left, self.calls, self.rate_limited = [], [], 0, 0
        self.fail_after = fail_after
    def redact(self, room, event_id, reason=None):
        if self.fail_after is not None and len(self.redacted) >= self.fail_after:
            raise KeyboardInterrupt("simulated stop")
        self.redacted.append(event_id); return "$r"
    def members(self, room): raise AssertionError("delete must not re-fetch members")
    def iter_messages(self, room): raise AssertionError("delete must not re-page history")
    def leave(self, room): self.left.append(room)
    def forget(self, room): pass


def fresh(tmp, skip=frozenset(), rooms=(("!a:x", "alice"), ("!b:x", "bob"))):
    store = Store(tmp / "s.json")
    for rid, user in rooms:
        store.record_scan(rid, [user], 20, [
            Target(f"$img-{rid}", "m.image", 1), Target(f"$txt-{rid}", "m.text", 2)])
    store.save()
    return store


def runner_for(store, tmp, skip=frozenset(), api=None):
    api = api or FakeAPI()
    paths = new_run(tmp / "r"); configure(paths, quiet=True)
    return api, Runner(api, store, Audit(paths.audit_file),
                       Options(execute=True, skip_users=set(skip)), me="@me:x")


base = Path(tempfile.mkdtemp())

# --- each command individually -----------------------------------------------
tmp = base / "solo"; tmp.mkdir(parents=True)
store = fresh(tmp)
api, r = runner_for(store, tmp)
r.run(["!a:x", "!b:x"], Kind.IMAGES, hide=False)
check("images alone: only attachments", sorted(api.redacted),
      ["$img-!a:x", "$img-!b:x"])

tmp = base / "solo2"; tmp.mkdir(parents=True)
store = fresh(tmp)
api, r = runner_for(store, tmp)
r.run(["!a:x", "!b:x"], Kind.MESSAGES, hide=False)
check("messages alone: everything", len(api.redacted), 4)

# --- sequential: images -> messages -> hide ----------------------------------
tmp = base / "seq"; tmp.mkdir(parents=True)
store = fresh(tmp)
api1, r1 = runner_for(store, tmp); r1.run(["!a:x", "!b:x"], Kind.IMAGES, hide=False)
store.save()

store2 = Store(tmp / "s.json")           # a separate later invocation
api2, r2 = runner_for(store2, tmp); r2.run(["!a:x", "!b:x"], Kind.MESSAGES, hide=False)
check("sequential: 2nd run skips what the 1st deleted", sorted(api2.redacted),
      ["$txt-!a:x", "$txt-!b:x"])
store2.save()

store3 = Store(tmp / "s.json")
api3, r3 = runner_for(store3, tmp)
r3.run(["!a:x", "!b:x"], kind=None, hide=True, hide_require=Kind.MESSAGES)
check("sequential: both conversations are empty afterwards",
      all(store3.rooms[r].is_clean(Kind.msgtypes(Kind.MESSAGES)) for r in ("!a:x", "!b:x")),
      True)
check("sequential: nothing is left, since Reddit refuses it", api3.left, [])

# --- hide --require images must be honoured ----------------------------------
tmp = base / "req"; tmp.mkdir(parents=True)
store = fresh(tmp)
api, r = runner_for(store, tmp); r.run(["!a:x"], Kind.IMAGES, hide=False)
store.save()
store2 = Store(tmp / "s.json")
api2, r2 = runner_for(store2, tmp)
r2.run(["!a:x"], kind=None, hide=True, hide_require=Kind.IMAGES)
check("hide never leaves a room while unsupported", api2.left, [])

# --- protected list applies to all three -------------------------------------
for label, kind, hide in (("images", Kind.IMAGES, False),
                          ("messages", Kind.MESSAGES, False),
                          ("hide", None, True)):
    tmp = base / f"prot-{label}"; tmp.mkdir(parents=True)
    store = fresh(tmp)
    api, r = runner_for(store, tmp, skip={"alice"})
    r.run(["!a:x", "!b:x"], kind, hide=hide, hide_require=Kind.IMAGES if hide else None)
    touched_a = any("!a:x" in x for x in api.redacted) or "!a:x" in api.left
    check(f"{label}: protected conversation untouched", touched_a, False)

# --- stop and resume ---------------------------------------------------------
tmp = base / "resume"; tmp.mkdir(parents=True)
store = fresh(tmp)
api, r = runner_for(store, tmp, api=FakeAPI(fail_after=2))
try:
    r.run(["!a:x", "!b:x"], Kind.MESSAGES, hide=False)
except KeyboardInterrupt:
    store.save()                          # what main.py does on Ctrl+C
check("interrupted after 2 deletions", len(api.redacted), 2)

store2 = Store(tmp / "s.json")
api2, r2 = runner_for(store2, tmp)
r2.run(["!a:x", "!b:x"], Kind.MESSAGES, hide=False)
check("resume deletes only what was left", len(api2.redacted), 2)
check("resume does not redo the first two",
      set(api.redacted) & set(api2.redacted), set())
check("everything ends up deleted",
      store2.summary()["deleted"], 4)

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all workflow checks passed"))
sys.exit(1 if fails else 0)
