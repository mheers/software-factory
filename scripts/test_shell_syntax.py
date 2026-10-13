#!/usr/bin/env python3
"""Every script parses — shell and Python alike.

A syntax error in a subject is invisible to the rest of the suite: the tests that would run it
stage copies, stub binaries around it, or simply never invoke it (`setup-gitlab.sh` has no test
at all), so a file that cannot parse can sit in the repository until somebody runs it for real.
That happened: a `${VAR:?word}` message containing an apostrophe broke `setup-gitlab.sh`'s
parse, and the whole gate stayed green because nothing asked the file to parse.

This asks. `bash -n` for every `scripts/*.sh` (the parser only — nothing executes), and
`compile()` for every `scripts/*.py`, so an unparseable script is a failing test with the file
named, rather than a surprise on the day it is needed.

Run: `python3 scripts/test_shell_syntax.py`. No fixtures, no network.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    problems: list[str] = []

    shells = sorted(HERE.glob("*.sh"))
    for path in shells:
        run = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        if run.returncode != 0:
            problems.append(f"{path.name}: bash -n failed:\n{(run.stderr or run.stdout).strip()}")

    pythons = sorted(p for p in HERE.glob("*.py") if "__pycache__" not in str(p))
    for path in pythons:
        try:
            compile(path.read_text(), str(path), "exec")
        except SyntaxError as exc:
            problems.append(f"{path.name}: {exc}")

    print(f"parsed {len(shells)} shell script(s) and {len(pythons)} python script(s)")
    if problems:
        print()
        for problem in problems:
            print(problem)
        return 1
    print("OK — every script parses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
