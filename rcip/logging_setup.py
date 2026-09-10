"""Logging for the purge run.

Three sinks, because when a selector silently stops matching you want all of
them:

  * console      - human readable, level controlled by -v / -q
  * run log file - everything at DEBUG, one file per run, kept forever
  * audit JSONL  - one machine-readable record per image considered

"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("rcip")

_CONSOLE_FMT = "%(asctime)s %(levelname)-5s %(message)s"
_FILE_FMT = "%(asctime)s.%(msecs)03d %(levelname)-8s [%(name)s:%(funcName)s:%(lineno)d] %(message)s"


@dataclass
class RunPaths:
    root: Path
    run_id: str

    @property
    def dir(self) -> Path:
        return self.root / self.run_id

    @property
    def log_file(self) -> Path:
        return self.dir / "run.log"

    @property
    def audit_file(self) -> Path:
        return self.dir / "audit.jsonl"

    @property
    def artifacts(self) -> Path:
        return self.dir / "artifacts"


def new_run(reports_root: Path) -> RunPaths:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    paths = RunPaths(reports_root, run_id)
    paths.dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts.mkdir(parents=True, exist_ok=True)
    return paths


def configure(paths: RunPaths, verbosity: int = 0, quiet: bool = False) -> None:
    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()
    LOG.propagate = False

    console = logging.StreamHandler(sys.stderr)
    if quiet:
        console.setLevel(logging.WARNING)
    elif verbosity >= 2:
        console.setLevel(logging.DEBUG)
    elif verbosity == 1:
        console.setLevel(logging.INFO)
    else:
        console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt="%H:%M:%S"))
    LOG.addHandler(console)

    fh = logging.FileHandler(paths.log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    LOG.addHandler(fh)

    LOG.debug("run log -> %s", paths.log_file)


class Audit:
    """Append-only JSONL record of everything the run decided."""

    def __init__(self, path: Path):
        self._path = path
        self._fh = path.open("a", encoding="utf-8")

    def write(self, event: str, **fields) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        LOG.debug("audit %s %s", event, fields)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # pragma: no cover - best effort
            pass


def attach_page_logging(page, audit: "Audit | None" = None) -> None:
    """Mirror browser-side noise into our log; it is often the only clue."""

    def on_console(msg):
        try:
            LOG.debug("page.console[%s] %s", msg.type, msg.text)
        except Exception:
            pass

    def on_pageerror(err):
        LOG.warning("page error: %s", err)
        if audit:
            audit.write("page_error", detail=str(err)[:500])

    def on_requestfailed(req):
        LOG.debug("request failed: %s %s (%s)", req.method, req.url[:160],
                  req.failure if isinstance(req.failure, str) else getattr(req, "failure", None))

    def on_response(resp):
        if resp.status == 429:
            # Reddit throttling. Redactions ride the same endpoint as history
            # paging, so a burst of these means deletions are being rejected.
            page._rcip_last_429 = time.time()
            page._rcip_429_count = getattr(page, "_rcip_429_count", 0) + 1
        if resp.status >= 400:
            LOG.debug("http %s %s", resp.status, resp.url[:160])

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("requestfailed", on_requestfailed)
    page.on("response", on_response)
    page.on("framenavigated", lambda f: LOG.debug("navigated: %s", f.url[:160]) if f == page.main_frame else None)
    LOG.debug("page logging attached")
