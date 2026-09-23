"""Direct client for the Matrix API behind Reddit chat.

The web UI talks to matrix.redditspace.com; so can we. Compared with driving
the browser this is roughly fifty times faster (~0.2s vs ~11s per
conversation), sees every room rather than whatever the virtualised sidebar
happened to render, and settles ownership from the event's `sender` instead of
inferring it from which buttons a hover toolbar drew.

Credentials come from a short browser launch: we read the bearer token off a
request the real client makes. Nothing is stored on disk.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .logging_setup import LOG

DEFAULT_BASE = "https://matrix.redditspace.com"


class AuthExpired(RuntimeError):
    """The access token stopped working mid-run."""


@dataclass
class Limits:
    max_retries: int = 6
    max_reauths: int = 3          # hard stop, so no error can loop on re-auth
    backoff_s: float = 1.0
    min_interval_s: float = 0.0     # optional politeness delay between calls


@dataclass
class MatrixClient:
    token: str
    base: str = DEFAULT_BASE
    limits: Limits = field(default_factory=Limits)
    on_reauth: object = None        # callable returning a fresh token
    _last_call: float = 0.0
    user_id: str = ""            # needed to address per-user account data
    rate_limited: int = 0
    reauths: int = 0
    calls: int = 0

    # ----------------------------------------------------------------- plumbing

    def _url(self, path: str, params: dict | None = None) -> str:
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return f"{self.base}{path}{q}"

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None):
        """One API call, honouring Matrix's own rate-limit instructions.

        Matrix answers 429 with `retry_after_ms`, so we can wait exactly as
        long as the server asks instead of guessing - the reason this backs off
        far more gracefully than the UI did.
        """
        for attempt in range(1, self.limits.max_retries + 1):
            if self.limits.min_interval_s:
                gap = time.monotonic() - self._last_call
                if gap < self.limits.min_interval_s:
                    time.sleep(self.limits.min_interval_s - gap)
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(self._url(path, params), data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    payload = json.loads(raw or b"{}")
                except Exception:
                    payload = {}
                if e.code == 429:
                    self.rate_limited += 1
                    wait = payload.get("retry_after_ms")
                    wait = (wait / 1000) if wait else self.limits.backoff_s * attempt
                    LOG.debug("rate limited; waiting %.1fs as instructed", wait)
                    time.sleep(min(wait, 60))
                    continue
                # Only these mean "your token is bad". M_FORBIDDEN means "you
                # may not do this particular thing" - refusing one redaction is
                # not an auth failure, and re-harvesting on it loops forever.
                if payload.get("errcode") in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"):
                    if self.on_reauth and self.reauths < self.limits.max_reauths:
                        self.reauths += 1
                        LOG.warning("access token rejected (%s); fetching a fresh one (%d/%d)",
                                    payload.get("errcode"), self.reauths,
                                    self.limits.max_reauths)
                        self.token = self.on_reauth()
                        continue
                    raise AuthExpired(payload.get("error", "token rejected"))
                if 500 <= e.code < 600 and attempt < self.limits.max_retries:
                    time.sleep(self.limits.backoff_s * attempt)
                    continue
                raise RuntimeError(f"{method} {path} -> {e.code} {payload or raw[:120]}")
            except urllib.error.URLError as e:
                if attempt >= self.limits.max_retries:
                    raise
                LOG.debug("network error (%s); retrying", e)
                time.sleep(self.limits.backoff_s * attempt)
        raise RuntimeError(f"{method} {path}: giving up after {self.limits.max_retries} tries")

    # -------------------------------------------------------------------- calls

    def whoami(self) -> dict:
        return self.request("GET", "/_matrix/client/v3/account/whoami")

    def joined_rooms(self) -> list[str]:
        return self.request("GET", "/_matrix/client/v3/joined_rooms").get("joined_rooms", [])

    def members(self, room: str) -> dict[str, str]:
        """user id -> Reddit username.

        Reddit's gateway does not implement /joined_members (404) - only the
        older /members, which returns m.room.member events.
        """
        path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}/members"
        out = {}
        for ev in (self.request("GET", path).get("chunk") or []):
            uid = ev.get("state_key")
            content = ev.get("content") or {}
            if uid and content.get("membership") in (None, "join", "invite"):
                out[uid] = content.get("displayname") or ""
        return out


    def iter_messages(self, room: str, page: int = 200):
        """Every event in a room, newest first, following pagination."""
        path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}/messages"
        token = None
        while True:
            params = {"dir": "b", "limit": page}
            if token:
                params["from"] = token
            body = self.request("GET", path, params)
            chunk = body.get("chunk") or []
            for ev in chunk:
                yield ev
            token = body.get("end")
            if not token or not chunk:
                return

    def redact(self, room: str, event_id: str, reason: str | None = None) -> str:
        txn = f"rcip{int(time.time() * 1000)}{abs(hash(event_id)) % 10000}"
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}"
                f"/redact/{urllib.parse.quote(event_id, safe='')}/{txn}")
        body = {"reason": reason} if reason else {}
        return self.request("PUT", path, body=body).get("event_id", "")

    def set_hidden(self, room: str, hidden: bool = True) -> None:
        """Hide (or unhide) a conversation in your own chat list.

        This is what Reddit's own hide button does - per-room account data,
        not leaving the room. Matrix leave is rejected outright on Reddit
        chat rooms. Being account data, it is private to you and reversible;
        the other person sees nothing.
        """
        path = (f"/_matrix/client/v3/user/{urllib.parse.quote(self.user_id, safe='')}"
                f"/rooms/{urllib.parse.quote(room, safe='')}"
                f"/account_data/com.reddit.hidden_chat")
        self.request("PUT", path, body={"hidden": bool(hidden)})

    def is_hidden(self, room: str) -> bool:
        path = (f"/_matrix/client/v3/user/{urllib.parse.quote(self.user_id, safe='')}"
                f"/rooms/{urllib.parse.quote(room, safe='')}"
                f"/account_data/com.reddit.hidden_chat")
        try:
            return bool(self.request("GET", path).get("hidden"))
        except Exception:
            return False
