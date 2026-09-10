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

Scans, deletes every message you have ever sent, then hides every conversation.
It asks you to type `DELETE EVERYTHING` first. Add `--keep-conversations` to
delete but leave them visible in your list.

---

## Commands

| Command | What it does |
| --- | --- |
| `scan` | Reads every conversation and records what is there. Changes nothing. |
| `images` | Deletes attachments you sent: images, video, files, audio. |
| `messages` | Deletes every message you sent, attachments included. |
| `hide` | Hides conversations that have nothing of yours left. |
| `status` | Prints what is known and what remains. No network calls. |
| `nuke` | `scan` then `messages` then `hide`, behind a confirmation prompt. |

They share one record of what has been done, so they compose freely:

```bash
./.venv/bin/python main.py images   --execute    # attachments first
./.venv/bin/python main.py messages --execute    # then the text
./.venv/bin/python main.py hide     --execute    # then tidy them out of the list
```

`images` and `messages` accept `--then-hide` to hide each conversation as soon
as it is clean. `hide` on its own takes `--require images|messages` to say what
must already be gone (default: everything you sent).

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

## What a second run does

Everything is recorded in `chat-records.json`, and every command reads it
before acting. That is what makes reruns cheap and safe to repeat: a second run
picks up exactly what is unfinished and leaves the rest alone.

Each of your messages carries one of four states:

| State | Meaning | Retried on a rerun? |
| --- | --- | --- |
| `pending` | Recorded by a scan, not yet deleted | **Yes** |
| `failed` | We tried and it did not work - a timeout, say | **Yes** |
| `deleted` | We deleted it | No |
| `gone` | It was already absent when we looked | No |

So a failed deletion is not lost: rerun the same command and it goes straight
back to the `pending` and `failed` ones. Nothing that already succeeded is
touched again.

At the conversation level:

- **Already scanned** conversations are not re-read from the network. Use
  `--rescan` to force it, or `--rescan-after HOURS` to treat old scans as
  stale. Rescan when you have used Reddit since - new messages you sent will
  not be noticed otherwise.
- **Already hidden** conversations are not hidden again.
- **Protected** conversations are skipped, and re-judged every run: remove a
  name from `skip_users.txt` and that conversation is reconsidered next time;
  add one and it is protected from then on, even if it was scanned earlier.
- **Unidentifiable** conversations (participants unknown while a protected list
  is in force) are skipped, and reconsidered if a later scan works out who is
  in them.

A run that ends early - Ctrl-C, a crash, a closed laptop - loses nothing beyond
the conversation in flight. Progress is written as it goes, so the same command
resumes from where it stopped.

To start over completely, delete `chat-records.json`. That discards what has
been done, so the next run rescans everything and re-attempts anything Reddit
still shows as present.

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

## Hiding conversations

Emptying a conversation does not remove it from your chat list; `hide` does
that, exactly the way the Reddit UI's hide button does:

```
PUT /_matrix/client/v3/user/{you}/rooms/{room}/account_data/com.reddit.hidden_chat
{"hidden": true}
```

That is per-room account data, not leaving the room - Reddit rejects the Matrix
leave endpoint outright (`403 "You cannot leave this room"`). Two useful
consequences: hiding is **private to you**, so the other person sees nothing,
and it is **reversible** - unhide from the Reddit UI, or set `hidden` false.

`hide` only touches conversations with nothing of yours left, and never
protected ones.

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
