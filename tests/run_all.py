#!/usr/bin/env python3
"""Run every test file and report. Works on any OS: python tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
failed = []
for test in sorted(HERE.glob("test_*.py")):
    result = subprocess.run([sys.executable, str(test)], capture_output=True, text=True)
    ok = result.returncode == 0
    print(f"  {'PASS' if ok else 'FAIL'}  {test.name}")
    if not ok:
        failed.append(test.name)
        print("\n".join("        " + line for line in (result.stdout + result.stderr).splitlines()[-15:]))
print(f"\n{len(failed)} failed" if failed else "\nall passed")
sys.exit(1 if failed else 0)
