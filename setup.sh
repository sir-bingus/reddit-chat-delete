#!/usr/bin/env bash
# One-time setup: virtualenv, dependencies, and the browser Playwright drives.
#
#   ./setup.sh            set everything up
#   source .venv/bin/activate   then use `python main.py ...` directly
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo "WARNING: only macOS and Linux are supported. Windows will fail on import." ;;
esac

echo "==> creating virtualenv in .venv"
[ -d .venv ] || python3 -m venv .venv

echo "==> installing Python dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo "==> installing Chromium for Playwright (~150MB, first run only)"
./.venv/bin/playwright install chromium

if [ ! -f skip_users.txt ]; then
  echo "==> creating skip_users.txt (conversations to leave alone)"
  cat > skip_users.txt <<'TXT'
# Conversations with these people are never touched.
# One username per line; "u/" optional, case does not matter.
# Lines starting with # are ignored.
TXT
fi

cat <<'DONE'

Setup complete.

Next:
  1. Add anyone you want to protect to skip_users.txt
  2. Take stock (changes nothing):   ./.venv/bin/python main.py scan
  3. See what is there:              ./.venv/bin/python main.py status

The first command opens a browser so you can log into Reddit. That happens
once; the session is remembered afterwards.
DONE
