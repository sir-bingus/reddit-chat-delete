"""A small cross-platform file lock.

The store used fcntl.flock, which does not exist on Windows. Rather than keep
two locking implementations (fcntl here, msvcrt there) with only one of them
ever exercised, this uses the one primitive every OS does atomically: creating
a file that must not already exist. Same code path on macOS, Linux and
Windows, so the tests that run here cover all three.

A lock left behind by a crashed process is treated as stale after
`stale_after` seconds. Saves take milliseconds, so the default is generous.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class FileLock:
    def __init__(self, path: Path, timeout: float = 60.0, stale_after: float = 30.0,
                 poll: float = 0.02):
        self.path = Path(path)
        self.timeout = timeout
        self.stale_after = stale_after
        self.poll = poll

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self._clear_if_stale()
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {self.path} within {self.timeout}s")
                time.sleep(self.poll)
                continue
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return self

    def __exit__(self, *exc) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    def _clear_if_stale(self) -> None:
        try:
            if time.time() - os.path.getmtime(self.path) > self.stale_after:
                os.remove(self.path)
        except FileNotFoundError:
            pass
