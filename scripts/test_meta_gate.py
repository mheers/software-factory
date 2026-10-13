#!/usr/bin/env python3
"""The gate this repository documents is the gate it runs.

Parses the first ```bash block after the heading `### This repository's own gate` in AGENTS.md and
runs every command **as written**, from the repository root. Not checked for plausibility — run.

What it holds:

1. every command in the block exists and exits 0 — a renamed or deleted test turns this red;
2. every script path named in the block is a file, so a failure says which name is wrong;
3. every `scripts/test_*.py` on disk is named in the block, so a test that exists is a test that
   runs. Two are deliberately exempt and are named here rather than inferred:
   this file (it would recurse) and `test_gitlab_ci.py`, whose one environment-dependent case
   asserts the *workspace's* GitLab profile resolves — documented next to the block in AGENTS.md.

Run: `python3 scripts/test_meta_gate.py`. No test runner, no network, no fixtures.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
AGENTS = ROOT / "AGENTS.md"
HEADING = "### This repository's own gate"

# Deliberately not in the block — see the docstring and AGENTS.md.
NOT_IN_BLOCK = {"test_meta_gate.py", "test_gitlab_ci.py"}


def parse_block(text: str) -> list[str]:
    if HEADING not in text:
        raise SystemExit(f"AGENTS.md: no `{HEADING}` heading — the gate block moved or was renamed")
    tail = text[text.index(HEADING):]
    match = re.search(r"```bash\n(.*?)```", tail, re.DOTALL)
    if not match:
        raise SystemExit(f"AGENTS.md: no ```bash block after `{HEADING}`")
    commands = []
    for raw in match.group(1).splitlines():
        line = raw.split(" #", 1)[0].strip()  # commands carry trailing comments
        if line:
            commands.append(line)
    return commands


def named_scripts(command: str) -> list[str]:
    argv = shlex.split(command)
    return [arg for arg in argv if arg.startswith("scripts/")]


def main() -> int:
    text = AGENTS.read_text()
    commands = parse_block(text)
    if not commands:
        print("the gate block parsed to nothing, which is not a gate")
        return 1

    named: set[str] = set()
    problems: list[str] = []
    for command in commands:
        scripts = named_scripts(command)
        named.update(Path(s).name for s in scripts)
        missing = [s for s in scripts if not (ROOT / s).is_file()]
        if missing:
            problems.append(f"{command}\n    -> {' '.join(missing)} does not exist")
            continue
        print(f"$ {command}")
        result = subprocess.run(shlex.split(command), cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            problems.append(
                f"{command}\n    -> exit {result.returncode}\n"
                + "\n".join(
                    line for line in (result.stdout + result.stderr).splitlines()[-25:]
                )
            )

    on_disk = {p.name for p in (ROOT / "scripts").glob("test_*.py")}
    absent = on_disk - named - NOT_IN_BLOCK
    for name in sorted(absent):
        problems.append(
            f"scripts/{name} exists and is not in the gate block — a test that exists and is not "
            f"in the block is a test nothing runs. Add it to AGENTS.md, or name it in "
            f"NOT_IN_BLOCK here with the reason."
        )

    print()
    if problems:
        print(f"FAILED — {len(problems)} problem(s):\n")
        for problem in problems:
            print(problem + "\n")
        return 1
    print(f"OK — {len(commands)} command(s) in the documented gate, all green; "
          f"{len(on_disk)} test file(s) accounted for")
    return 0


if __name__ == "__main__":
    sys.exit(main())
