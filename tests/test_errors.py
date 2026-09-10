"""Error handling must not loop, and must not confuse errors for each other."""
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


class API:
    """Raises whatever the test asks for; counts reauth attempts."""
    def __init__(self, error=None):
        self.error, self.attempts, self.reauths = error, 0, 0
        self.calls = self.rate_limited = 0
    def redact(self, room, event_id, reason=None):
        self.attempts += 1
        if self.error:
            raise RuntimeError(self.error)
        return "$r"
    def members(self, r): raise AssertionError
    def iter_messages(self, r): raise AssertionError
    def leave(self, r): pass
    def forget(self, r): pass


def run(error, tmp):
    store = Store(tmp / "s.json")
    store.record_scan("!r:x", ["bob"], 5, [Target("$e", "m.text", 1)])
    api = API(error)
    paths = new_run(tmp / "r"); configure(paths, quiet=True)
    r = Runner(api, store, Audit(paths.audit_file), Options(execute=True), me="@me:x")
    r.run(["!r:x"], Kind.MESSAGES, hide=False)
    return api, store, r


base = Path(tempfile.mkdtemp())

# the bug that looped: a refused deletion must be recorded once, not retried
api, store, r = run("PUT /redact -> 403 {'errcode': 'M_FORBIDDEN'}", base / "a")
check("refused: tried exactly once", api.attempts, 1)
check("refused: counted as refused", r.counts.refused, 1)
check("refused: recorded, so a rerun will not retry forever",
      store.rooms["!r:x"].targets["$e"].status, "failed")

# already gone is success, not failure
api, store, r = run("PUT /redact -> 404 {'errcode': 'M_NOT_FOUND'}", base / "b")
check("already gone: not a failure", r.counts.failed, 0)
check("already gone: recorded as gone", store.rooms["!r:x"].targets["$e"].status, "gone")

# an unexpected error is a real failure, still not a loop
api, store, r = run("PUT /redact -> 500 server exploded", base / "c")
check("unexpected error: tried once", api.attempts, 1)
check("unexpected error: counted as a failure", r.counts.failed, 1)

# success path unaffected
api, store, r = run(None, base / "d")
check("success: deleted", r.counts.deleted, 1)

# the client must cap re-auths so nothing can loop on them
from rcip.api import Limits, MatrixClient
lim = Limits()
check("client caps re-auth attempts", hasattr(lim, "max_reauths") and lim.max_reauths <= 3, True)
src = (ROOT / "rcip" / "api.py").read_text()
reauth_block = src[src.index("errcode") : src.index("errcode") + 400]
check("M_FORBIDDEN no longer triggers re-auth", "M_FORBIDDEN" in reauth_block, False)

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all error-handling checks passed"))
sys.exit(1 if fails else 0)
