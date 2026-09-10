"""Many processes writing progress must never lose each other's work."""
import json, multiprocessing, sys, tempfile, random, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rcip.config import Config
from rcip.purge import Purger


def _stub(state_file):
    obj = type("P", (), {})()
    obj.state_path = state_file
    obj.cfg = Config(state_file=state_file)
    obj.done_rooms = set()
    obj._save_state = Purger._save_state.__get__(obj)
    obj._load_state = Purger._load_state.__get__(obj)
    return obj


def worker(args):
    state_file, wid, n = args
    p = _stub(Path(state_file))
    for i in range(n):
        p.done_rooms.add(f"!w{wid}-r{i}:reddit.com")
        p._save_state()
        time.sleep(random.uniform(0, 0.004))
    return wid


def main() -> int:
    fails = []
    def check(name, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
        if not ok: fails.append(name)

    tmp = Path(tempfile.mkdtemp())
    state = tmp / "state.json"
    W, N = 6, 40
    with multiprocessing.Pool(W) as pool:
        pool.map(worker, [(str(state), w, N) for w in range(W)])

    done = set(json.loads(state.read_text())["done_rooms"])
    check(f"{W} concurrent writers x {N} conversations, none lost", len(done), W * N)
    check("file is valid JSON with no duplicates",
          len(done) == len(json.loads(state.read_text())["done_rooms"]), True)

    # a later run sees everything without any manual merging
    check("a fresh run loads all prior progress", len(_stub(state)._load_state()), W * N)

    print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all concurrency checks passed"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
