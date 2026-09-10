"""Progress must be shared across runs and worker layouts (no browser needed)."""
import json, sys, tempfile
from pathlib import Path
ROOT = Path(".")
sys.path.insert(0, str(ROOT))
from rcip.config import Config
from rcip.purge import Purger

def test_unresolved_blocks_done():
    """A conversation holding undecided images must never count as finished."""
    import tempfile, json
    from pathlib import Path
    from rcip.config import Config
    from rcip.purge import Purger

    fails = []
    def check(name, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
        if not ok: fails.append(name)

    tmp = Path(tempfile.mkdtemp())
    st = tmp / "state.json"
    st.write_text(json.dumps({
        "done_rooms": ["!a:x", "!b:x", "!c:x"],
        "unresolved": {"!b:x": ["$img1", "$img2"]},
    }))
    obj = type("P", (), {})()
    obj.state_path = st
    obj.cfg = Config(state_file=st)
    loaded = Purger._load_state.__get__(obj)()
    check("rooms with undecided images are dropped from done", sorted(loaded), ["!a:x", "!c:x"])
    check("their image ids are remembered", sorted(obj.unresolved.get("!b:x", [])),
          ["$img1", "$img2"])

    # once resolved, the room can be recorded as done and stays done
    obj.done_rooms = set(loaded)
    obj.unresolved["!b:x"] = set()
    obj.done_rooms.add("!b:x")
    Purger._save_state.__get__(obj)()
    obj2 = type("P", (), {})()
    obj2.state_path = st
    obj2.cfg = Config(state_file=st)
    again = Purger._save_state and Purger._load_state.__get__(obj2)()
    check("resolved room now counts as done", "!b:x" in again, True)
    check("no stale unresolved entry left", obj2.unresolved.get("!b:x"), None)
    return fails


fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
    if not ok: fails.append(name)

def load(state_file):
    cfg = Config(state_file=state_file)
    return Purger.__new__(Purger).__class__._load_state.__get__(
        type("P", (), {"state_path": state_file, "cfg": cfg})()
    )()

tmp = Path(tempfile.mkdtemp())
main = tmp / "state.json"

check("no files -> empty", len(load(main)), 0)

main.write_text(json.dumps({"done_rooms": ["!a", "!b"]}))
check("main file only", len(load(main)), 2)

(tmp / "state-w0.json").write_text(json.dumps({"done_rooms": ["!c", "!d"]}))
(tmp / "state-w1.json").write_text(json.dumps({"done_rooms": ["!e"]}))
check("main + two worker files merged", len(load(main)), 5)

(tmp / "state-w2.json").write_text(json.dumps({"done_rooms": ["!a", "!c"]}))
check("overlapping ids de-duplicated", len(load(main)), 5)

(tmp / "state-w3.json").write_text("{ corrupt")
check("corrupt worker file ignored, rest still load", len(load(main)), 5)

main.unlink()
check("worker files alone, no main file (!b lived only in main)", len(load(main)), 4)

print()
print("--- undecided images must block completion ---")
fails += test_unresolved_blocks_done()

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all state checks passed"))
sys.exit(1 if fails else 0)
