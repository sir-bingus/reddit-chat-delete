#!/usr/bin/env python3
"""One-time setup. Works the same on macOS, Linux and Windows.

    macOS / Linux:   python3 install.py
    Windows:         py install.py

Creates a virtual environment in .venv, installs the dependencies, downloads
the Chromium build Playwright uses (~150 MB), and creates an empty
skip_users.txt. Safe to run again; it only does what is missing.
"""

import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
WINDOWS = os.name == "nt"
PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")

SKIP_TEMPLATE = """\
# People whose chats are never touched: nothing deleted, nothing hidden.
# One Reddit username per line. "u/" is optional and case does not matter.
# Anything after a # is ignored.
"""


def run(*args) -> None:
    print("   ", " ".join(str(a) for a in args))
    subprocess.run([str(a) for a in args], check=True)


def main() -> int:
    if sys.version_info < (3, 9):
        print(f"Python 3.9 or newer is required; this is {sys.version.split()[0]}.")
        return 1

    if not PY.exists():
        print("==> creating the virtual environment (.venv)")
        try:
            venv.EnvBuilder(with_pip=True).create(VENV)
        except Exception as exc:
            print(f"could not create .venv: {exc}")
            if sys.platform.startswith("linux"):
                print("On Debian/Ubuntu you may need:  sudo apt install python3-venv")
            return 1
    else:
        print("==> .venv already exists")

    print("==> installing Python packages")
    run(PY, "-m", "pip", "install", "--quiet", "--upgrade", "pip")
    run(PY, "-m", "pip", "install", "--quiet", "-r", HERE / "requirements.txt")

    print("==> installing Chromium for Playwright (first time only, ~150 MB)")
    run(PY, "-m", "playwright", "install", "chromium")

    skip = HERE / "skip_users.txt"
    if not skip.exists():
        skip.write_text(SKIP_TEMPLATE, encoding="utf-8")
        print("==> created skip_users.txt")

    main_py = "main.py"
    python = r".venv\Scripts\python" if WINDOWS else ".venv/bin/python"
    activate = r".venv\Scripts\activate" if WINDOWS else "source .venv/bin/activate"
    print(f"""
Done.

Run commands with the environment's Python:

    {python} {main_py} scan

or activate it once per terminal and just use "python":

    {activate}
    python {main_py} scan

The first command opens a browser window. Log in to Reddit there; it is only
needed once. See README.md for everything else.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
