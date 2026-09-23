"""The pieces that differ between macOS, Linux and Windows.

Everything here runs the same code on every OS, so passing on one platform
is real evidence for the others: no fcntl, no `ps`, no os.kill probing.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rcip.config import load_skip_list  # noqa: E402
from rcip.filelock import FileLock  # noqa: E402

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
    if not ok:
        fails.append(name)


tmp = Path(tempfile.mkdtemp())

# --- the lock excludes -------------------------------------------------------
lock = tmp / "x.lock"
inside, overlaps = [0], [0]
def worker():
    for _ in range(30):
        with FileLock(lock):
            inside[0] += 1
            if inside[0] > 1:
                overlaps[0] += 1
            time.sleep(0.001)
            inside[0] -= 1
threads = [threading.Thread(target=worker) for _ in range(6)]
for t in threads: t.start()
for t in threads: t.join()
check("never two holders at once", overlaps[0], 0)
check("lock file removed when released", lock.exists(), False)

# --- a lock left by a crashed process does not block forever ------------------
lock.write_text("12345")
old = time.time() - 120
os.utime(lock, (old, old))
t0 = time.monotonic()
with FileLock(lock, stale_after=30):
    pass
check("stale lock is cleared promptly", time.monotonic() - t0 < 2, True)

# --- a fresh lock held by someone else is respected, then times out ----------
lock.write_text("99999")
try:
    with FileLock(lock, timeout=0.3, stale_after=30):
        got = "acquired"
except TimeoutError:
    got = "timed out"
check("a live lock is not stolen", got, "timed out")
lock.unlink()

# --- protected list survives whatever editor wrote it -------------------------
f = tmp / "skip.txt"
cases = {
    "UTF-8":            b"bob\nu/Alice\n",
    "UTF-8 with BOM":   b"\xef\xbb\xbfbob\r\nu/Alice\r\n",
    "UTF-16 (Notepad)": "bob\r\nu/Alice\r\n".encode("utf-16"),
    "CRLF + comments":  b"# my list\r\nbob  # friend\r\n\r\nu/Alice\r\n",
}
for label, data in cases.items():
    f.write_bytes(data)
    check(f"skip list, {label}", sorted(load_skip_list(f)), ["alice", "bob"])

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all portability checks passed"))
sys.exit(1 if fails else 0)
