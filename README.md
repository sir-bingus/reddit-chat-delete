# reddit-chat-image-purge

Bulk-deletes **your own** image attachments across every conversation in Reddit chat.
It drives a real browser: enumerates your conversations, opens each one, scrolls all the
way back to the oldest message so Reddit lazy-loads the full history, and removes every
image you sent — leaving conversations with people on your protected list untouched.

**Dry run by default.** Nothing is deleted until you pass `--execute`.

Verified against the live site in September 2026.

## Setup

```bash
cd ~/Documents/reddit-chat-image-purge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium
```

## Use

1. List the people you want to protect in `skip_users.txt`, one per line
   (`u/` prefix optional, case-insensitive).

2. Dry run. A Chromium window opens; log into Reddit in it once — the session is saved
   in `.browser-profile/` and reused by every later run.

   ```bash
   .venv/bin/python main.py -v
   ```

3. When the dry-run report looks right:

   ```bash
   .venv/bin/python main.py --execute -v
   ```

**Deletion is permanent** — Reddit's own dialog says "removed for everyone in this chat,
you can't undo this."

A cautious first live run, capped at three deletions:

```bash
.venv/bin/python main.py --execute --max-deletes-total 3 --keep-open -v
```

### Scale and parallel runs

A conversation takes roughly **10–12 seconds** (open, scroll back through its history,
check each image). This account has **801 conversations**, so one window is about
2.5–3 hours.

To go faster, run several browser windows at once:

```bash
.venv/bin/python main.py --workers 4 -v
```

The coordinator takes a one-off census of your conversations, splits them round-robin by
conversation id, clones your logged-in profile once per worker (Chromium locks a profile
to one process), and runs them in parallel, printing combined progress every few seconds.
Four workers bring a full pass to roughly 45 minutes.

Sharding is by **conversation id, not position**. "Worker N skips the first 20×N rows"
looks simpler but the sidebar is virtualised and lazy-loaded, so a positional offset
means something different in each window and drifts as rows load — you would get
duplicated and missed conversations. Ids cannot drift; a test run confirmed the shards
were disjoint and every conversation was opened exactly once. Workers reach their rooms
directly at `reddit.com/chat/room/<id>`, so they never depend on sidebar position.

Be conservative with `--workers` when using `--execute`: several windows deleting at once
is more load on Reddit than a person clicking, and rate limiting is a real risk. Four is
a reasonable ceiling; start lower if you see failures.

Progress is saved after each conversation (`state.json`, or per-worker state files), so
an interrupted run resumes where it stopped. `--no-resume` revisits everything.

Single-window runs need no census at all — they take the first unprocessed conversation
already mounted in the sidebar and start immediately, scrolling for more only when they
run out.

## Options

| Flag | Effect |
| --- | --- |
| `--execute` | Actually delete. Without it, dry run. |
| `--skip-users FILE` | Protected-username file (default `./skip_users.txt`). |
| `--skip USER` | Protect one username inline; repeatable. |
| `--only TEXT` | Only conversations whose username contains this; repeatable. |
| `--max-rooms N` | Stop after N conversations. |
| `--max-deletes-per-room N` / `--max-deletes-total N` | Blast-radius limits. |
| `--delay MS` | Pause after each deletion (default 1200ms). |
| `--state FILE` / `--no-resume` | Progress file for resuming long runs. |
| `--headless` | No visible window. Note: Reddit's chat app does **not** render headless — use this only if you have verified it works for you. |
| `--keep-open` | Leave the browser open at the end. |
| `--workers N` | Run N browser windows in parallel on disjoint shards. |
| `--census-only FILE` | Write the full conversation list to FILE and exit. |
| `--inspect` | Dump live page structure and exit (see *When Reddit changes*). |
| `-v` | Debug detail on the console. The log file always has everything. |

## How it decides what to delete

* **Only your own messages.** Reddit's hover toolbar offers a **Delete** button on your
  messages and **Report** on other people's. The tool hovers each image message, reads
  that toolbar, and only proceeds when it finds Delete. It cannot delete other people's
  images and does not try.
* **Only images.** Detected structurally via `.image-message` / `rs-image` on the message
  body, not by guessing at image sizes. Text messages are never touched.
* **Protected users are checked twice.** First against the conversation's sidebar entry
  (`aria-label="Direct chat with <user>"`), before it is even opened. Then again from
  inside, against the usernames that actually posted — which also covers group chats and
  members not named in the sidebar label. The protected list is matched on whole
  username tokens, so `bob` does not protect `bobcat_99`, and never against message
  previews, so a preview mentioning someone cannot cause a wrong skip.
* **Fail-safe.** If a protected list is in force and the tool cannot identify *anyone*
  in a conversation, it skips that conversation rather than risk it.

## Logging

Every run writes `reports/<timestamp>/`:

| File | Contents |
| --- | --- |
| `run.log` | Full DEBUG trace: every conversation, scroll round, hover, toolbar reading, decision, plus browser console messages, page errors and failed requests. |
| `audit.jsonl` | One JSON record per image considered — conversation, participants, URL, outcome (`deleted`, `would-delete`, `not-deletable`, `no-toolbar`, `no-confirm-button`, `delete-unconfirmed`) — plus every room-level decision and skip reason. |
| `artifacts/*.png`, `*.html` | Screenshot + page HTML captured automatically whenever a step fails. |

```bash
# what would this dry run have deleted?
jq -r 'select(.event=="image" and .outcome=="would-delete") | "\(.user)\t\(.url)"' reports/*/audit.jsonl

# tally outcomes
jq -r 'select(.event=="image") | .outcome' reports/*/audit.jsonl | sort | uniq -c

# why was a conversation skipped?
jq -r 'select(.event=="room_skipped") | "\(.user)\t\(.reason)\t\(.matched // "")"' reports/*/audit.jsonl
```

## The DOM it drives

Reddit's chat is a set of custom elements using **shadow DOM**, with **two virtualised
lists** — only ~20 rows of each exist at any moment. Everything is therefore keyed on
server-side ids rather than DOM nodes:

```
rs-rooms-nav > rs-virtual-scroll                 <- sidebar scroller (virtualised)
  rs-rooms-nav-room[room="!id:reddit.com"]       <- one conversation (shadow DOM)
    a[aria-label="Direct chat with <username>"]

rs-timeline[room] > rs-virtual-scroll-dynamic    <- the timeline IS this element
  rs-timeline-event[data-id="$eventId"]          <- one message (shadow DOM)
    div.room-message.regular
      span.user-name                             <- sender
      div.room-message-body.image-message        <- image attachment
  rs-timeline-event-menu                         <- hover toolbar: …/Delete|Report
```

Three traps worth knowing if you maintain this:

* `rs-virtual-scroll-dynamic` is itself the scrolling element. Its wrapper reports
  `scrollHeight == clientHeight`, so driving the wrapper makes scroll-back a silent no-op.
* Every lookup must pierce shadow roots or it finds nothing.
* Navigation and paging are driven by **elements**, not pixel offsets:
  `element.scrollIntoView()` on the last sidebar row or the oldest message. Pixel
  arithmetic against a virtualised list skips windows that were never rendered — that bug
  made a single enumeration pass report anywhere from 387 to 653 of the true 801
  conversations, varying run to run.

## When Reddit changes its markup

All DOM knowledge is isolated in [`rcip/dom.js`](rcip/dom.js). If a run finds zero
conversations or zero images:

```bash
.venv/bin/python main.py --inspect
```

That prints and saves live element counts, scroller geometry and every `rs-*` tag on the
page. `tools/explore.py` goes further, finding repeated sibling structures without
knowing any class names — useful if the element names change wholesale.

## Tests

```bash
.venv/bin/python tests/test_smoke.py
```

Runs the real engine against `tests/fixture_chat.html`, which replicates Reddit's actual
markup — custom elements, open shadow roots, both virtualised scrollers, lazily-loaded
older history, the Delete/Report hover toolbar and the "Yes, Delete" modal. It checks
that the protected list is honoured (including protecting a group chat via its second
member), that lazily-loaded history is swept, that dry run deletes nothing, and that
execute deletes exactly the deletable images. No Reddit account involved.

## Layout

```
main.py               CLI, run setup, summary
rcip/browser.py       persistent Chromium profile, login gate, helper injection
rcip/purge.py         conversation walk, scroll-back, ownership check, delete flow
rcip/dom.js           all DOM knowledge (injected into the page)
rcip/enumerate.py     one-off conversation census (only used to shard parallel work)
rcip/parallel.py      coordinator: profile cloning, sharding, worker supervision
rcip/config.py        options, protected-user matching
rcip/logging_setup.py run log, audit JSONL, failure artifacts
tools/explore.py      structural recon for when the markup changes
tests/                fixture replicating the real DOM + end-to-end test
```

## Caveats

* Reddit's chat app does not render in headless Chromium — runs use a visible window.
* Conversations are enumerated by scrolling the sidebar; the count grows as it loads
  (this account showed 533 → 653 across runs as more of the list was reached).
* Deleting removes the message for everyone in the chat. Reddit may keep the underlying
  media on its CDN afterwards.
