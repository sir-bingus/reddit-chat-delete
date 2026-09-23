# reddit-chat-delete

A command-line tool that deletes what you've sent in Reddit chat. It can remove
just your attachments (images, videos, files, audio) or every message you've
sent, and it can hide the emptied chats from your chat list afterwards. Anyone
you put on a protected list is left completely alone.

It works through the same API the Reddit chat website uses, so it's fast: on an
account with about 1,000 chats, a full scan took around 12 minutes and deleting
about 1,000 attachments took 4.

> **Deleting can't be undone.** A deleted message is removed for everyone in the
> chat, not just for you. Every command does a dry run unless you add
> `--execute`, so you can always see what would happen first.

## Quick start

You need Python 3.9 or newer. From the project folder:

**macOS / Linux**

```bash
python3 install.py
source .venv/bin/activate
```

**Windows** (Command Prompt or PowerShell)

```bat
py install.py
.venv\Scripts\activate
```

Then, on any OS:

1. Open `skip_users.txt` and add anyone whose chats you want left alone, one
   username per line.
2. Scan your chats. This reads them and records what you've sent. It
   doesn't change anything.

   ```
   python main.py scan
   ```

   The first time, a browser window opens on Reddit's login page. Log in there
   and the scan continues by itself.
3. Look at what it found:

   ```
   python main.py status
   ```

4. Do a dry run of the deletion, then the real thing:

   ```
   python main.py images
   python main.py images --execute
   ```

Swap `images` for `messages` to delete everything you've sent, not only
attachments. Run `python main.py hide --execute` afterwards to clear the empty
chats out of your list.

That's the whole workflow: **scan, check, dry run, execute.**

## Installing

### macOS and Linux

Python 3.9+ is usually already installed (`python3 --version` to check). Then:

```bash
python3 install.py
```

On Debian or Ubuntu, if it complains about `venv`, run
`sudo apt install python3-venv` first.

### Windows

1. Install Python from [python.org](https://www.python.org/downloads/). On the
   first screen of the installer, tick **Add python.exe to PATH**. The
   installer also adds the `py` command used below.

   Don't rely on the `python` that ships with Windows. On a fresh install it is
   often just a shortcut that opens the Microsoft Store.
2. Open Command Prompt or PowerShell in the project folder and run:

   ```bat
   py install.py
   ```

### What the installer does

`install.py` creates a private Python environment in `.venv`, installs the two
packages the tool needs (Playwright and psutil), downloads the Chromium browser
Playwright drives (about 150 MB, once), and creates an empty `skip_users.txt`.
Running it again is harmless; it skips whatever is already done.

### Running commands

Either activate the environment once per terminal window and use `python`:

| OS | Activate |
| --- | --- |
| macOS / Linux | `source .venv/bin/activate` |
| Windows, Command Prompt | `.venv\Scripts\activate` |
| Windows, PowerShell | `.venv\Scripts\Activate.ps1` |

or skip activation and call the environment's Python directly:

| OS | Command |
| --- | --- |
| macOS / Linux | `.venv/bin/python main.py scan` |
| Windows | `.venv\Scripts\python main.py scan` |

The rest of this README uses `python main.py ...` and assumes the environment
is active.

If PowerShell refuses to run `Activate.ps1` with an error about scripts being
disabled, either use the direct form above or allow local scripts once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Logging in

The tool needs your Reddit session. It gets it by opening a real browser with
its own separate profile (stored in `.browser-profile`), letting Reddit's chat
page load, and reading the session token from a request the page makes. The
token is then saved in `.session-token.json` and reused, so most runs don't
open a browser at all.

- **First run:** a browser window opens on Reddit's login page. Log in normally.
  The tool waits up to five minutes and carries on as soon as you're in. This
  profile is separate from your everyday browser, so logging in here doesn't
  affect it and it doesn't see your other browsing.
- **Later runs:** the saved token is used. If Reddit rejects it, the tool opens
  the browser again to get a new one, hidden on macOS and briefly visible on
  Windows and Linux.
- **Logged out?** If a hidden attempt gets nowhere after 45 seconds, the window
  is shown so you can log in again.

Useful flags: `--show-window` to always see the browser, `--fresh-token` to
ignore the saved token and fetch a new one.

## Protecting people

Put usernames in `skip_users.txt`, one per line:

```
some_friend
u/AnotherPerson   # the u/ is optional
# lines starting with # are ignored
```

Case doesn't matter and whole names are matched, so `bob` won't also protect
`bobcat_99`. Nothing in a protected chat is deleted or hidden.

A few details worth knowing:

- The list is checked again on every run. Add a name later and that chat is
  protected from then on, even if it was scanned earlier. Remove a name and the
  chat is included again next run.
- If the tool can't work out who is in a chat, and you have anyone on your list,
  it skips that chat rather than take the risk.
- You can also protect someone for a single run without editing the file:
  `--skip username` (repeat it for more people).
- The file can be saved from any editor. Notepad's UTF-8 and "Unicode" options
  both work.

## Commands

Every command except `status` needs your Reddit session. Everything except
`scan` and `status` is a dry run unless you add `--execute`.

### `scan`

```
python main.py scan
```

Reads your chats and records who's in each one and every message you've sent
in it, with its type. Nothing is changed. The results go in
`chat-records.json`, and every other command works from that file.

It records all your messages, not just attachments. Reading a chat's history is
the slow part and costs the same either way, so one scan is enough for an
`images` run, a `messages` run, and `hide`.

A chat that's already been scanned isn't read again. Use `--rescan` to re-read
everything, or `--rescan-after 24` to re-read anything scanned more than 24
hours ago. Rescan if you've used Reddit chat since the last scan, otherwise
messages you've sent since won't be known about.

**If you have more than 1,000 chats:** Reddit only ever lists 1,000 of them at a
time (see [Limitations](#limitations)). Any command automatically scans chats
that weren't in the list before, so running your usual command again later
picks up older chats as they come into view.

### `status`

```
python main.py status
```

Prints what's recorded: how many chats are known, scanned, protected and
hidden, and how many of your attachments and messages are pending, deleted,
already gone, or failed. It reads `chat-records.json` only and makes no network
requests, so it's instant and safe to run any time, including while another
command is running.

### `images`

```
python main.py images              # dry run: shows what it would delete
python main.py images --execute    # deletes
```

Deletes attachments you sent: images, videos, files and audio. Your text
messages are left alone. If a chat hasn't been scanned yet it's scanned first,
so this also works on its own without a separate `scan`.

Add `--then-hide` to hide each chat as soon as your attachments in it are gone.
Note that this hides the chat even if your text messages are still in it.

### `messages`

```
python main.py messages
python main.py messages --execute
```

Deletes every message you sent, attachments included. Otherwise it behaves
exactly like `images`, including `--then-hide`, which here waits until
everything of yours in the chat is gone.

Expect roughly 250 to 300 deletions a minute. An account with 15,000 messages
takes about an hour.

### `hide`

```
python main.py hide
python main.py hide --execute
```

Hides chats that have nothing of yours left in them, the same way the hide
button in Reddit does. It's a setting on your own account: the other person
isn't told and sees no change, and you can unhide a chat from Reddit later.
Hiding doesn't delete anything.

By default a chat is only hidden once every message of yours is gone. Use
`--require images` to hide chats once just your attachments are gone.

`hide` only acts on chats that have been scanned, so it can't hide a chat it
knows nothing about. Protected chats are never hidden.

### `nuke`

```
python main.py nuke              # dry run of the whole thing
python main.py nuke --execute
```

Runs `scan`, then `messages`, then `hide`: everything you've ever sent is
deleted and the emptied chats are hidden. Protected chats are still skipped.
With `--execute` it asks you to type `DELETE EVERYTHING` before starting.

- `--keep-conversations`: delete everything but don't hide the chats.
- `--yes`: skip the typed confirmation, for unattended runs.

## Options

These work with every command.

| Option | What it does |
| --- | --- |
| `--execute` | Actually make changes. Without it, nothing is deleted or hidden. |
| `--max-deletes N` | Stop after N deletions. Good for a careful first run. |
| `--max-rooms N` | Stop after N chats. |
| `--skip USER` | Protect a username for this run only. Can be repeated. |
| `--skip-users FILE` | Use a different protected list (default `skip_users.txt`). |
| `--rescan` | Re-read chats that were already scanned. |
| `--rescan-after HOURS` | Re-read chats scanned more than this many hours ago. |
| `--show-window` | Show the browser whenever it's used. |
| `--fresh-token` | Get a new session token instead of the saved one. |
| `--min-interval SEC` | Wait at least this long between requests to Reddit. |
| `-v` | More detail on screen. The log file always has everything. |
| `-q` | Only show warnings and errors. |
| `--store FILE` | Where the record is kept (default `chat-records.json`). |
| `--profile DIR` | Browser profile folder (default `.browser-profile`). |
| `--token-cache FILE` | Where the token is saved (default `.session-token.json`). |
| `--reports DIR` | Where logs go (default `reports`). |

A careful first real run:

```
python main.py images --execute --max-deletes 3
```

Then check those three are gone in Reddit before running without the limit.

## Running it again

Every command reads and updates `chat-records.json`, so you can stop at any
point and run the same command again. It carries on from where it was, and
nothing that already worked is repeated.

Each of your recorded messages is in one of four states:

| State | Meaning | Retried next run? |
| --- | --- | --- |
| `pending` | Found by a scan, not deleted yet | Yes |
| `failed` | A deletion was attempted and didn't work, for example it timed out | Yes |
| `deleted` | Deleted by this tool | No |
| `gone` | Already missing when the tool tried to delete it | No |

So if a run reports failures, run the same command again and it goes straight
back to them.

For chats:

- A scanned chat isn't scanned again unless you use `--rescan` or
  `--rescan-after`.
- A hidden chat isn't hidden again.
- Protected chats are skipped, and the protected list is re-checked every run.
- Chats where nobody could be identified are skipped and looked at again next
  time.

If a run is interrupted (Ctrl+C, a crash, the laptop going to sleep), at most
the chat it was working on needs redoing, and that happens automatically.

To start completely fresh, delete `chat-records.json`. The next command will
scan everything again.

## Files it creates

All of these stay in the project folder and are excluded from git.

| File | What it is |
| --- | --- |
| `.venv/` | The Python environment made by `install.py`. |
| `.browser-profile/` | The browser's profile, including your Reddit login. |
| `.session-token.json` | Your saved session token. Only readable by you. |
| `skip_users.txt` | Your protected list. |
| `chat-records.json` | What's been scanned, deleted and hidden. |
| `reports/` | A folder of logs for every run. |

`.browser-profile` and `.session-token.json` give access to your Reddit
account. Don't share them or copy them anywhere public.

## Logs

Every run writes a folder `reports/<date>-<time>/` containing:

- `run.log`: everything the run did, in detail.
- `audit.jsonl`: one line of JSON per decision (each chat scanned or skipped,
  each message deleted or failed, each chat hidden), for when you want to check
  exactly what happened.

For a quick overview, `python main.py status` is usually easier.

## Troubleshooting

**It sits at "still waiting for a session token".** It's usually reloading
Reddit's chat page to get it to make a request. If nothing happens after a
minute or so, the browser window is shown; check it for a login page. Running
with `--show-window` lets you watch from the start.

**"no session token after 300s".** Nobody logged in within five minutes. Run the
command again and log in when the window appears.

**"a browser from an earlier run still holds this profile".** A browser from a
run that was killed was still open. The tool closes it and continues. If it
can't, close any "Google Chrome for Testing" windows yourself and try again.

**"rate-limit waits" in the summary.** Reddit asked the tool to slow down and it
did. Nothing to fix. If you'd rather keep well clear of that, add
`--min-interval 1`, which caps it at one request per second.

**Some deletions failed.** Usually timeouts. Run the same command again and
only the failed ones are retried.

**"Reddit refused to delete".** Reddit sometimes won't delete a particular
message. It's marked as failed and the run moves on; the next run tries it once
more.

**A later run finds chats and messages you thought were done.** If you have more
than 1,000 chats, older ones come into Reddit's list as others drop out, and
they get scanned when they appear. Run the same command again to deal with
them.

**macOS: the browser window shows even without `--show-window`.** Hiding it uses
macOS's System Events, which may need permission. Allow your terminal app under
System Settings, Privacy & Security, Accessibility. The tool still works without
it; the window is just visible for a few seconds.

**Windows: `py` isn't recognised.** Python isn't installed, or was installed
without the launcher. Reinstall from python.org with the PATH box ticked.

## How it works

Reddit chat is built on Matrix, and the website talks to
`matrix.redditspace.com`. This tool talks to the same service directly:

- It reads each chat's history about 100 messages per request.
- It knows which messages are yours because each one carries its sender.
- It deletes messages one at a time. There's no bulk-delete option on Reddit's
  side.
- When Reddit asks it to slow down, it waits exactly as long as Reddit says.
- Hiding sets the same per-chat flag the hide button sets
  (`com.reddit.hidden_chat`). Leaving a chat isn't possible: Reddit rejects it.

The browser is only used to get the session token. Everything else is plain
HTTP requests.

```
main.py               commands and options
install.py            setup
rcip/runner.py        scanning, deleting and hiding
rcip/store.py         chat-records.json
rcip/targets.py       what counts as an attachment or a message
rcip/api.py           requests to Reddit
rcip/auth.py          getting and saving the session token
rcip/browser.py       the browser used for the token
rcip/config.py        the protected list
rcip/filelock.py      file locking that works on every OS
rcip/logging_setup.py logs
tests/                tests (no Reddit account needed)
tools/capture.py      records what the Reddit website sends, for working out
                      how it does something when Reddit changes
```

## Limitations

- It uses an internal Reddit API that isn't documented or supported. Reddit can
  change it at any time and this tool may stop working until it's updated.
- Reddit lists at most 1,000 of your chats at once, and there's no way to ask
  for the rest. If you have more, the tool can only reach the ones currently
  listed. Which chats are listed shifts over time, so repeated runs reach more
  of them, but there's no guarantee a single run sees every chat. Reddit's own
  chat sidebar has the same limit.
- Reddit may keep copies of deleted images and files on its servers for a
  while; deleting removes them from the chat.
- It's been used for real on macOS. Windows and Linux run the same code, and
  the tests cover the parts that differ between systems, but they haven't been
  run end to end against a real account.
- Reddit's chat doesn't load in a headless browser, so a real browser window is
  used when a token is needed. Only macOS lets the tool hide it.

## Tests

```
python tests/run_all.py
```

They check the protected list, what each command selects, resuming, error
handling, file locking and file encodings. None of them touch Reddit.

## Uninstalling

Delete the project folder. That removes everything including your saved login.
Playwright's copy of Chromium lives outside the folder; delete it too if you
want the space back:

- macOS: `~/Library/Caches/ms-playwright`
- Windows: `%USERPROFILE%\AppData\Local\ms-playwright`
- Linux: `~/.cache/ms-playwright`

Deleting the tool doesn't undo anything it did. Deleted messages stay deleted,
and hidden chats stay hidden until you unhide them in Reddit.
