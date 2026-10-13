#!/usr/bin/env python3
"""The tracker adapter — the one surface the factory reads issues and merge requests through.

Two implementations, selected by `factory_config.tracker_provider()`:

- **gitlab** (the shipped default) — `glab`
- **github** (the other flavor) — `gh`

Both inherit the caller's environment. Sourcing the workspace profile is the caller's job —
`cleanup-workers.sh` sources `glab-env.sh` before its checks — because a profile loader that
guesses where a credential lives is the failure `glab-env.sh` exists to avoid.

A CLI that is missing, or an answer that does not parse, is **not** a verdict: the functions
report `None`/`unknown`, and the CLI exits 2 — *cannot tell* is never a pass.

The CLI is what the shell side reads:

    python3 scripts/tracker.py state --iid 42              # opened | closed | unknown
    python3 scripts/tracker.py mr-count --branch issue-42-fix   # <n> | unknown

The Python side imports the functions directly (`main_sweep.py` files red-main issues through
`file_issue`). Operations that resolve a repository from the working directory (`state`,
`mr-count` — the shapes `gh issue view` and `glab api projects/:id` support) keep doing so, and
the caller runs inside the repository, exactly as before.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import factory_config  # noqa: E402

TIMEOUT_SECONDS = 60
UNKNOWN = "unknown"


def _cli(name: str) -> str | None:
    """The binary, from `PATH` and then from where it is actually installed.

    Cron's `PATH` is `/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin` while `go install` puts
    binaries in `~/go/bin` — so a tool that is installed can still be "not on PATH", and
    "did not look properly" is not the same fact as "the tool is absent".
    """
    found = shutil.which(name)
    if found:
        return found
    for candidate in (Path.home() / "go/bin" / name, Path.home() / ".local/bin" / name,
                      Path("/usr/local/bin") / name, Path("/usr/bin") / name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _tool(name: str) -> str | None:
    return _cli(name)


# ------------------------------------------------------------------------- reading issues


def open_issues(repo: str) -> list[dict]:
    """Every issue (open and closed) of `repo`, for the dedupe key.

    An empty list is also what a failure returns — the caller's dedupe then cannot find the key
    and the *filing* call it makes runs into the same outage. Preserved from the GitLab flavor,
    which shipped this behavior.
    """
    full = factory_config.repo_full_name(repo)
    if factory_config.tracker_provider() == "gitlab":
        tool = _tool("glab")
        if not tool:
            return []
        run = _run([tool, "-R", full, "issue", "list", "--all", "--per-page", "100",
                    "--output", "json"])
    else:
        tool = _tool("gh")
        if not tool:
            return []
        run = _run([tool, "issue", "list", "--repo", full, "--state", "all", "--limit", "100",
                    "--json", "title"])
    if run is None or run.returncode != 0:
        return []
    try:
        data = json.loads(run.stdout)
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def file_issue(repo: str, title: str, body: str, labels: list[str]) -> str | None:
    """One issue in `repo`. Returns the URL, or None when the tracker could not be reached."""
    full = factory_config.repo_full_name(repo)
    if factory_config.tracker_provider() == "gitlab":
        tool = _tool("glab")
        if not tool:
            return None
        argv = [tool, "-R", full, "issue", "create", "--title", title]
        for label in labels:
            argv += ["--label", label]
        argv += ["--description", body]
    else:
        tool = _tool("gh")
        if not tool:
            return None
        argv = [tool, "issue", "create", "--repo", full, "--title", title, "--body", body]
        for label in labels:
            argv += ["--label", label]
    run = _run(argv)
    if run is None or run.returncode != 0 or not run.stdout.strip():
        return None
    return run.stdout.strip().splitlines()[-1]


# ------------------------------------------------------------------------ issue state, MRs


def issue_state(iid: str) -> str:
    """`opened` / `closed`, or `unknown`. Resolved from the working directory, like before."""
    if factory_config.tracker_provider() == "gitlab":
        tool = _tool("glab")
        if not tool:
            return UNKNOWN
        run = _run([tool, "api", f"projects/:id/issues/{iid}"])
    else:
        tool = _tool("gh")
        if not tool:
            return UNKNOWN
        run = _run([tool, "issue", "view", str(iid), "--json", "state"])
    if run is None or run.returncode != 0:
        return UNKNOWN
    try:
        state = (json.loads(run.stdout) or {}).get("state")
    except Exception:  # noqa: BLE001
        return UNKNOWN
    if not state:
        return UNKNOWN
    # `gh` spells them OPEN/CLOSED; the factory and `glab` spell them opened/closed.
    return {"open": "opened", "closed": "closed"}.get(str(state).lower(), str(state).lower())


def mr_count(branch: str) -> int | None:
    """Open merge requests / pull requests whose source is `branch`, or None (`unknown`)."""
    if factory_config.tracker_provider() == "gitlab":
        tool = _tool("glab")
        if not tool:
            return None
        run = _run([tool, "mr", "list", "--source-branch", branch, "-F", "json"])
    else:
        tool = _tool("gh")
        if not tool:
            return None
        run = _run([tool, "pr", "list", "--head", branch, "--state", "open", "--json", "number"])
    if run is None or run.returncode != 0:
        return None
    try:
        data = json.loads(run.stdout)
    except Exception:  # noqa: BLE001
        return None
    return len(data) if isinstance(data, list) else None


# ---------------------------------------------------------------------------------- the CLI


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tracker.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="op", required=True)
    state = sub.add_parser("state", help="one issue's state, from the working directory")
    state.add_argument("--iid", required=True)
    mr = sub.add_parser("mr-count", help="open MRs/PRs for one branch")
    mr.add_argument("--branch", required=True)
    sub.add_parser("provider", help="print the configured provider")
    args = parser.parse_args(argv)

    if args.op == "provider":
        print(factory_config.tracker_provider())
        return 0
    if args.op == "state":
        answer = issue_state(args.iid)
        print(answer)
        return 0 if answer != UNKNOWN else 2
    count = mr_count(args.branch)
    print(UNKNOWN if count is None else count)
    return 0 if count is not None else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
