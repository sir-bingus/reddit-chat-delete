"""Run several browser windows at once, each on its own shard of conversations.

Why shards of explicit room ids rather than "worker N skips the first 20*N
rows": the sidebar is virtualised and lazy-loaded, so a positional offset means
something different in each window and drifts as rows load. Ids cannot drift,
so no conversation is done twice and none is missed.

Each worker needs its own browser profile - Chromium locks a profile directory
to a single process - so the logged-in profile is copied once per worker. The
copy skips caches, which is most of the bulk.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .logging_setup import LOG

SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
             "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "Crashpad",
             "component_crx_cache", "extensions_crx_cache", "Safe Browsing"}
SKIP_FILES = {"SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"}


def _ignore(_dir, names):
    return [n for n in names if n in SKIP_DIRS or n in SKIP_FILES]


def clone_profile(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=_ignore, symlinks=True, dirs_exist_ok=True)


def shard(rooms: list[dict], n: int) -> list[list[dict]]:
    """Round-robin, so a run of big conversations does not land on one worker."""
    out: list[list[dict]] = [[] for _ in range(n)]
    for i, r in enumerate(rooms):
        out[i % n].append(r)
    return out


def run_workers(rooms: list[dict], n: int, paths, base_argv: list[str],
                master_profile: Path, state_file: Path, poll_s: float = 5.0) -> dict:
    work = paths.dir / "workers"
    work.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    shards = shard(rooms, n)

    LOG.info("preparing %d browser profile(s) (copying the logged-in session)...", n)
    procs = []
    for i, part in enumerate(shards):
        wdir = work / f"w{i}"
        wdir.mkdir(parents=True, exist_ok=True)
        rooms_file = wdir / "rooms.json"
        rooms_file.write_text(json.dumps(part), encoding="utf-8")
        profile = paths.root.parent / ".profiles" / f"w{i}"
        profile.parent.mkdir(parents=True, exist_ok=True)
        clone_profile(master_profile, profile)

        argv = [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py"),
                *base_argv,
                "--rooms-file", str(rooms_file),
                "--profile", str(profile),
                "--reports", str(wdir / "reports"),
                # Every worker writes the same shared, locked state file, so
                # progress is never split across files that need merging.
                "--state", str(state_file)]
        LOG.info("  worker %d: %d conversation(s)", i, len(part))
        log_fh = (wdir / "console.log").open("w", encoding="utf-8")
        procs.append({"i": i, "p": subprocess.Popen(argv, stdout=log_fh, stderr=subprocess.STDOUT),
                      "dir": wdir, "fh": log_fh, "count": len(part)})

    LOG.info("%d worker(s) running; watching for progress", n)
    started = time.time()
    while any(w["p"].poll() is None for w in procs):
        time.sleep(poll_s)
        done = [w for w in procs if w["p"].poll() is not None]
        totals = _collect(procs)
        LOG.info("  [%5.1f min] workers finished %d/%d | conversations %d | images %d | "
                 "deleted %d | would-delete %d | failures %d",
                 (time.time() - started) / 60, len(done), len(procs),
                 totals["rooms_seen"], totals["images_found"], totals["images_deleted"],
                 totals["images_would_delete"], totals["failures"])

    for w in procs:
        w["fh"].close()
        if w["p"].returncode not in (0, 1):
            LOG.warning("  worker %d exited with code %s - see %s",
                        w["i"], w["p"].returncode, w["dir"] / "console.log")
    return _collect(procs)


def _collect(procs) -> dict:
    keys = ("rooms_seen", "rooms_processed", "rooms_skipped", "images_found",
            "images_deleted", "images_would_delete", "images_not_mine", "failures")
    totals = dict.fromkeys(keys, 0)
    for w in procs:
        for stats_file in (w["dir"] / "reports").glob("*/stats.json"):
            try:
                d = json.loads(stats_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            for k in keys:
                totals[k] += int(d.get(k, 0) or 0)
    return totals
