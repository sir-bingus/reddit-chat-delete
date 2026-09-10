"""What counts as deletable, in one place.

Both the attachments run and the full-messages run classify events with these
functions, so the two can never drift apart. Anything that needs to know "is
this an image?" or "is this mine?" asks here.
"""

from __future__ import annotations

# Attachment-style messages: what an "images" run removes.
MEDIA_MSGTYPES = frozenset({"m.image", "m.video", "m.file", "m.audio"})
# Everything a person can send: what a "messages" run removes.
TEXT_MSGTYPES = frozenset({"m.text", "m.emote", "m.notice"})
ALL_MSGTYPES = None          # None means "no filter" - every message of yours


class Kind:
    """The two things a run can target. Used for CLI names and filtering."""
    IMAGES = "images"
    MESSAGES = "messages"

    @staticmethod
    def msgtypes(kind: str):
        """The message types a run of this kind removes.

        `messages` returns None rather than a set: it means everything you
        sent, including types we have not enumerated here, which is safer than
        a whitelist that silently misses a new one.
        """
        if kind == Kind.IMAGES:
            return set(MEDIA_MSGTYPES)
        if kind == Kind.MESSAGES:
            return ALL_MSGTYPES
        raise ValueError(f"unknown kind: {kind}")

    @staticmethod
    def describe(kind: str) -> str:
        return "attachments" if kind == Kind.IMAGES else "messages"


def is_message(event: dict) -> bool:
    """A real chat message, as opposed to membership/state noise."""
    return event.get("type") == "m.room.message" and bool(event.get("content"))


def is_redacted(event: dict) -> bool:
    """Already deleted - by us on an earlier run, or by anyone since."""
    if (event.get("unsigned") or {}).get("redacted_because"):
        return True
    return not (event.get("content") or {})


def msgtype_of(event: dict) -> str:
    return (event.get("content") or {}).get("msgtype") or ""


def is_media(event: dict) -> bool:
    return msgtype_of(event) in MEDIA_MSGTYPES


def is_mine(event: dict, me: str) -> bool:
    """Ownership, decided by the server's own record of who sent it.

    This is why the API engine has no equivalent of the browser's
    "ownership-unknown": there is nothing to infer.
    """
    return event.get("sender") == me


def deletable(event: dict, me: str) -> bool:
    """Every message of yours that still exists - regardless of run kind.

    Scans record all of these so one pass serves both an attachments run and
    a full-messages run; the filtering happens later, from stored data.
    """
    return is_message(event) and is_mine(event, me) and not is_redacted(event)
