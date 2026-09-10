# reddit-chat-cleanup

Deletes your own content from Reddit chat: the attachments you sent, or every
message you sent, and optionally leaves the conversation afterwards. People you
list as protected are never touched.

It talks to the API behind Reddit chat rather than clicking through the web UI,
so a full pass over ~1000 conversations takes minutes rather than hours.

**Deleting is permanent.** Reddit's own wording is *"removed for everyone in
this chat, you can't undo this."* Leaving a conversation is visible to the other
person and cannot be undone either.

---

## TL;DR

```bash
./setup.sh
```

Add anyone you want to protect to `skip_users.txt`, one username per line. Then:

```bash
./.venv/bin/python main.py scan               # look, change nothing
./.venv/bin/python main.py images             # dry run: what would go
./.venv/bin/python main.py images --execute   # actually delete
```

That is the normal sequence: **scan -> dry run -> execute**. Every command is a
dry run until you add `--execute`.

Check what is left at any point, without touching the network:

```bash
./.venv/bin/python main.py status
```

### Delete everything in one command

```bash
./.venv/bin/python main.py nuke --execute
```

Scans and deletes every message you have ever sent. It asks you to type
`DELETE EVERYTHING` first.

---

## Commands

| Command | What it does |
| --- | --- |
| `scan` | Reads every conversation and records what is there. Changes nothing. |
| `images` | Deletes attachments you sent: images, video, files, audio. |
| `messages` | Deletes every message you sent, attachments included. |
| `hide` | **Not supported** - see below. |
| `status` | Prints what is known and what remains. No network calls. |
| `nuke` | `scan` then `messages`, behind a confirmation prompt. |

They share one record of what has been done, so they compose freely:

```bash
./.venv/bin/python main.py images   --execute    # attachments first
./.venv/bin/python main.py messages --execute    # then the text
```

The second run does not redo the first one's work, and stopping with Ctrl-C then
restarting picks up where it left off.

### Options

| Flag | Effect |
| --- | --- |
| `--execute` | Actually make changes. Without it, nothing is modified. |
| `--skip-users FILE` | Protected-user list (default `./skip_users.txt`). |
| `--skip USER` | Protect one username inline; repeatable. |
| `--max-deletes N` | Stop after N deletions. Good for a cautious first run. |
| `--max-rooms N` | Stop after N conversations. |
| `--rescan` | Re-read history even for conversations already scanned. |
| `--rescan-after H` | Treat scans older than H hours as stale. |
| `--min-interval SEC` | Minimum gap between API calls, to go gently. |
| `--show-window` | Show the browser used to pick up your session token. |
| `--fresh-token` | Ignore the cached token and get a new one. |
| `-v` | More console detail; the log file always has everything. |

A cautious first live run:

```bash
./.venv/bin/python main.py images --execute --max-deletes 3 -v
```

---

## Protected users

Put usernames in `skip_users.txt`:

```
some_friend
u/AnotherPerson
```

Matching ignores case and an optional `u/`, and matches whole usernames, so
`bob` does not also protect `bobcat_99`.

Protection is re-checked on **every run**, not only when a conversation was
first scanned, so adding a name later still protects conversations scanned
before you added it. If a conversation's participants cannot be identified at
all and a protected list is in force, that conversation is skipped rather than
risked.

---

## How it works

Reddit chat is a Matrix client. The web app talks to `matrix.redditspace.com`,
and so does this:

- **history** - one request returns ~100 messages, so there is no scrolling
- **ownership** - each event carries its `sender`, so "did I send this?" is a
  fact rather than an inference
- **deletion** - one `PUT .../redact/{eventId}` per message

A browser is used for exactly one thing: getting a session token. Reddit's chat
credentials are not in `localStorage`, so the real client is launched briefly
and the token read from a request it makes. The token is then cached
(owner-only, git-ignored) and reused, so most runs need no browser at all.

There is no bulk-delete endpoint - the gateway advertises Matrix v1.2 with no
redaction extensions - so it is one call per message. When Reddit rate-limits
us it replies with `retry_after_ms`, and we wait exactly that long.

A scan records **all** your messages, not just attachments, because paging the
history costs the same either way. One scan therefore serves an `images` run, a
`messages` run, and the question of whether a conversation is finished.
Deleting never re-reads history; it works from what the scan recorded.

### Layout

```
main.py               the CLI: scan / images / messages / hide / status / nuke
rcip/runner.py        the engine: scan, delete, hide
rcip/store.py         the shared record of what is known and done
rcip/targets.py       what counts as deletable, in one place
rcip/api.py           Matrix client
rcip/auth.py          session token, harvested once then cached
rcip/browser.py       the browser, used only for that token
rcip/config.py        protected-user list and name matching
rcip/logging_setup.py console log, run log, audit trail
tests/                behaviour tests; no Reddit account needed
```

---

## Logs

Every run writes `reports/<timestamp>/`:

| File | Contents |
| --- | --- |
| `run.log` | Full trace of the run at DEBUG level. |
| `audit.jsonl` | One JSON record per decision: scans, skips, deletions, failures. |

```bash
# what was deleted, and where
jq -r 'select(.event=="target" and .outcome=="deleted") | .room' reports/*/audit.jsonl | sort | uniq -c

# which conversations were skipped, and why
jq -r 'select(.event=="room_skipped") | .reason' reports/*/audit.jsonl | sort | uniq -c
```

---

## Hiding conversations is not supported

Emptying a conversation does not remove it from your chat list, and this tool
cannot remove it either. Reddit's API rejects the Matrix leave endpoint on every
chat room:

```
403 M_FORBIDDEN  "You cannot leave this room"
```

It also implements no room tags and exposes no account data that marks a chat as
hidden, so there is nothing else to set. Whatever the web UI's hide button does,
it is not reachable through the API this tool uses. The `hide` command therefore
refuses to run and explains why, rather than failing on every conversation.

If you want conversations gone from your list, hide them in the Reddit UI. Their
contents will already be empty.

## Requirements and limitations

- **macOS and Linux.** Windows is not supported: the record file uses `fcntl`
  for locking, which does not exist there, so it fails on import.
- Hiding the browser window is macOS-only. Elsewhere it warns and stays visible.
- Reddit chat does not render in headless Chromium at all - verified in both the
  old headless shell and the new mode - which is why a real window is used for
  the token, hidden where the OS allows it.
- This uses an internal API that Reddit does not document and may change without
  notice.

## Tests

```bash
for t in tests/test_*.py; do ./.venv/bin/python "$t"; done
```

They cover the protected list (including a name added after scanning), what each
run kind selects, sequencing and resume, and concurrent writes to the record.
None of them touch Reddit.
