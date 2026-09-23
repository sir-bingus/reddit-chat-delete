"""Run configuration: the protected-user list and how names are matched."""

from __future__ import annotations

import re
from pathlib import Path

from .logging_setup import LOG

# Reddit usernames: 3-20 chars of letters, digits, underscore, hyphen.
_USER_TOKEN = re.compile(r"[A-Za-z0-9_\-]{3,20}")


def normalize_user(name: str) -> str:
    """Fold a username to the form the protected list compares against."""
    return name.strip().lstrip("@").removeprefix("u/").removeprefix("U/").strip().lower()


def _read_text(path: Path) -> str:
    """Read a text file whatever editor wrote it.

    Windows editors often add a byte-order mark. Left in, it sticks to the
    first username - "bob" becomes "\\ufeffbob" - which then never matches,
    so that person would silently lose their protection. Notepad's "Unicode"
    option writes UTF-16, which would not decode as UTF-8 at all.
    """
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def load_skip_list(path: Path | None) -> set[str]:
    """Usernames to leave completely alone. Case-insensitive, `u/` optional."""
    names: set[str] = set()
    if path is None:
        return names
    if not path.exists():
        LOG.warning("protected-user list %s does not exist - NO users are protected", path)
        return names
    for raw in _read_text(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(normalize_user(line))
    LOG.info("loaded %d protected username(s) from %s", len(names), path)
    return names


def label_matches_skip(label: str, skip: set[str]) -> str | None:
    """The protected username `label` refers to, if any.

    Matches whole username-like tokens rather than substrings, so protecting
    `bob` does not also protect `bobcat_99`.
    """
    if not skip:
        return None
    tokens = {t.lower() for t in _USER_TOKEN.findall(label or "")}
    for name in skip:
        if name in tokens:
            return name
    return None
