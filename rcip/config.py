"""Run configuration: CLI args, skip list, tunables."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import LOG

_USER_TOKEN = re.compile(r"[A-Za-z0-9_\-]{3,20}")


def load_skip_list(path: Path | None) -> set[str]:
    """Read usernames to protect. Case-insensitive, `u/` prefix optional."""
    names: set[str] = set()
    if path is None:
        return names
    if not path.exists():
        LOG.warning("skip list %s does not exist - NO users are protected", path)
        return names
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        names.add(normalize_user(line))
    LOG.info("loaded %d protected username(s) from %s", len(names), path)
    LOG.debug("protected: %s", sorted(names))
    return names


def normalize_user(name: str) -> str:
    return name.strip().lstrip("@").removeprefix("u/").removeprefix("U/").strip().lower()


def label_matches_skip(label: str, skip: set[str]) -> str | None:
    """Return the protected username that `label` refers to, if any.

    Reddit room labels look like "u/someone  ·  2h  ·  last message text", so
    match on whole username-ish tokens rather than a naive substring test -
    that stops "bob" from protecting "bobcat_99" and vice versa.
    """
    if not skip:
        return None
    tokens = {t.lower() for t in _USER_TOKEN.findall(label or "")}
    for name in skip:
        if name in tokens:
            return name
    return None


@dataclass
class Config:
    execute: bool = False
    skip_users: set[str] = field(default_factory=set)
    only_rooms: list[str] = field(default_factory=list)
    max_rooms: int | None = None
    max_deletes_per_room: int | None = None
    max_deletes_total: int | None = None
    delete_delay_ms: int = 1200
    scroll_pause_ms: int = 700
    max_scroll_rounds: int = 400
    idle_scroll_rounds: int = 4
    max_sidebar_rounds: int = 1200
    sidebar_settle_rounds: int = 10
    sidebar_passes: int = 4
    open_by_click: bool = True
    sidebar_pause_ms: int = 500
    room_settle_ms: int = 4000
    hover_pause_ms: int = 500
    dialog_pause_ms: int = 900
    state_file: "Path | None" = None
    room_url_template: str = "https://www.reddit.com/chat/room/{}"
    headless: bool = False
    hidden: bool = False
    profile_dir: Path = Path(".browser-profile")
    login_timeout_s: int = 300
    keep_open: bool = False
