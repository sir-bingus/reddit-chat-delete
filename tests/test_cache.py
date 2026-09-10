"""Unit test for the census cache decision logic (no browser needed)."""
import json, sys, tempfile
from pathlib import Path
ROOT = Path(".")
sys.path.insert(0, str(ROOT))
from rcip.config import Config
from rcip import enumerate as en

calls = {"n": 0}
def fake_enumerate(browser, cfg, audit=None, limit=None):
    calls["n"] += 1
    return [{"room": f"!r{i}:reddit.com", "user": f"u{i}"} for i in range(5)]
en.enumerate_rooms = fake_enumerate

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
    if not ok: fails.append(name)

tmp = Path(tempfile.mkdtemp())
cache = tmp / "rooms.json"

calls["n"] = 0
cfg = Config(rooms_cache=cache)
r = en.load_or_build_census(None, cfg)
check("missing cache -> builds", (len(r), calls["n"]), (5, 1))
check("missing cache -> writes file", cache.exists(), True)

calls["n"] = 0
r = en.load_or_build_census(None, cfg)
check("existing cache -> reused, no rebuild", (len(r), calls["n"]), (5, 0))

calls["n"] = 0
cfg2 = Config(rooms_cache=cache, refresh_census=True)
r = en.load_or_build_census(None, cfg2)
check("--refresh-census -> rebuilds", calls["n"], 1)

calls["n"] = 0
cache.write_text("[]")
r = en.load_or_build_census(None, cfg)
check("empty cache -> rebuilds", (len(r), calls["n"]), (5, 1))

calls["n"] = 0
cache.write_text("{ not json")
r = en.load_or_build_census(None, cfg)
check("corrupt cache -> rebuilds", (len(r), calls["n"]), (5, 1))

calls["n"] = 0
cache.unlink()
r = en.load_or_build_census(None, cfg, limit=3)
check("--max-rooms -> truncated result", len(r), 3)
check("--max-rooms -> truncated list NOT cached", cache.exists(), False)

cache.write_text(json.dumps([{"room": "!ok:reddit.com"}, {"bogus": 1}]))
r = en.load_or_build_census(None, cfg)
check("malformed entries dropped", len(r), 1)

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "all cache checks passed"))
sys.exit(1 if fails else 0)
