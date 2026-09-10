"""Several processes writing the shared record must not lose each other's work.

Targets rcip.store.Store, which every part of the current program uses.
"""
import json, multiprocessing, random, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rcip.store import Store, Target, DELETED


def worker(args):
    path, wid, n = args
    for i in range(n):
        store = Store(Path(path))          # each write re-reads, like a real run
        rid = f"!w{wid}-r{i}:reddit.com"
        store.record_scan(rid, [f"user{wid}"], 10, [Target(f"$e{wid}-{i}", "m.image", i)])
        store.set_status(rid, f"$e{wid}-{i}", DELETED)
        store.save()
        time.sleep(random.uniform(0, 0.004))
    return wid


def main() -> int:
    fails = []
    def check(name, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
        if not ok: fails.append(name)

    tmp = Path(tempfile.mkdtemp())
    path = tmp / "chat-records.json"
    W, N = 5, 25
    with multiprocessing.Pool(W) as pool:
        pool.map(worker, [(str(path), w, N) for w in range(W)])

    raw = json.loads(path.read_text())
    check("file is valid JSON after concurrent writes", isinstance(raw.get("rooms"), dict), True)
    check(f"{W} writers x {N} conversations, none lost", len(raw["rooms"]), W * N)

    store = Store(path)
    check("a later run sees them all", len(store.rooms), W * N)
    check("statuses survived", store.summary()[DELETED], W * N)
    check("nothing left pending", store.summary()["pending"], 0)

    print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all concurrency checks passed"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
