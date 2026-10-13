#!/usr/bin/env python3
"""fleet-keepalive — keep workers alive, and notice when they are not.

Designed to run from cron every 30 minutes.

## What it will never do

**It will never merge, never close an issue, and never dispatch a new ticket.** Those are the
coordinator's, and every one of them needs judgement that a timer does not have:

- a merge needs the gate re-run, a merge message written for the reasoning, and
  `git merge-base --is-ancestor` confirming the ref that was actually merged;
- a close needs the merge confirmed first, in that order;
- a dispatch needs the interference check, and the ceiling is not a target.

A timer that merges unattended is how a branch lands without anyone having read it. So this script's
worst outcome is a worker nudged to commit, or a line in a log saying something is ready.

## What it does

| Condition | Action |
|---|---|
| pane recorded but no agent detected | restart the agent in that pane |
| `NOT-STARTED` — no tokens, no commits, no diff | re-prompt it. It never ran a turn, so waiting cannot help. |
| 0 commits, idle, unchanged for `--stall` checks | ask the worker whether it is blocked, or nudge it to begin |
| dirty tree, unchanged for `--stall` checks | ask it to commit — an uncommitted tree is indistinguishable from no work |
| `MERGEABLE` / `GATE-RED` | **report only.** A human merges. |
| a repository's `main` pipeline is `RED` | **report only.** A job ran and failed — that is a code defect, and only a human decides what to do about it. |
| a repository's `main` pipeline is `BLOCKED` | reported once, under its own heading, as **not a code defect** — see below. |
| a row in the last recorded main sweep is `RED` | **report only**, and it withholds the all-clear. `origin/main` fails its own gate; a worker fixes it on a branch. |
| the main sweep's record is missing, stale or unreadable | **not a pass.** `main` is the ref every branch is created from; "not swept" must not read like "swept and green". |

## It reads GitLab CI, and the two reds are different facts

This script used to run local gates only, which is right for a worker mid-edit and
useless for the question actually being asked — *is the pipeline green?* The failure this family
spent a day on was **only ever visible in GitLab**: `make check` was green on the workstation,
because the workstation has the sibling checkout the pipeline could not obtain. A verdict of
`GREEN` for a repository whose pipeline is red is not caution; it is answering a different
question and reporting it as this one.

**Right now every pipeline in this family is red, and none of it is a code defect.** The shared
runners have no compute minutes, so every job is rejected before it starts: `failure_reason:
ci_quota_exceeded`, `started_at: null`, `duration: null`. That is billing, not infrastructure, and
the signature is *every job, every repository, at once*. A keepalive that reported that as red
would be permanently red, and **a monitor that is always red is a monitor nobody reads** — the
same failure as an always-green one with the opposite cause.

So `gitlab-ci.py` classifies the two apart and this script renders them in different sections:
`BLOCKED` is reported, because "nothing was verified" is a real fact, and it is never counted as
a fault. `UNKNOWN` — GitLab could not be read, or the fields do not separate the two — is also
never a pass, because `GREEN` is the only value that is.

## It reads the main sweep's last completed run

`main_sweep.py` gates every repository's `origin/main` hourly from cron, and until the record existed, its
findings lived only in `/tmp/opencode/main-sweep.cron.log` — a file nothing reads. Measured on
2026-10-06: `social-studio`'s main was red for three hours while the sweep gated it
**seven times**, every row at a current head, in a log no person opened.

So this script reads the sweep's recorded run through `main_sweep_state.py` — the same module
`notify-coordinator.py` uses, so the two halves of the report cannot file one run into different
states. A `RED` row joins NEEDS ATTENTION with its repository, commit and the sweep's own
`first_useful_line` of the failure; an unverified record (missing, stale, unreadable, `NO-GATE`)
gets its own heading; and **neither can coexist with the all-clear.**

The record is read, not re-gated: the full sweep takes minutes and this script runs every thirty.
The classification, the staleness limit and the "absence is not a pass" rule live in the module.

## Declared states, and why declaring can only ever make this louder

The paragraph above separates two reds by hand, and it does not solve the problem: the
same eleven repositories were reported at every run for as long as the work took, so **a report
that is always true is one nobody reads**. That is the argument in `_expected_red`'s docstring,
written for branches and never applied to CI, which is the generalisation this ticket makes.

`FLEET_EXPECTED_CI` is a state file listing repositories whose state is *the answer rather than
a gap* — written when known, removed when fixed. A declared state gets a section of its own and
stays in the counts.

**It never clears the report.** A declaration is a claim, and nothing here can check a claim, so
a declared state *withholds* the all-clear instead of granting it. The direction is the whole
safety property: **declaring can only ever make this report louder, never quieter.** Nine
`BLOCKED` repositories are clear today, because the never-started rule refused to let a billing problem make this
tool permanently non-clear — and a coordinator who then declares all nine gets a *less* clear
report than one who declared nothing. That reads as backwards and is right. The convenient
implementation, `if repo in declared: skip`, is a monitor that goes quiet when someone edits a
JSON file, which is a **silencer**, and a silencer is worse than noise because it is trusted.

**A declaration is checked against reality, never trusted.** A mismatch escalates:

| declared | observed | reported as |
|---|---|---|
| matching | same state | `CI DECLARED` — reported, not cleared |
| any | `RED` | **a fault, twice**: the red, and the stale declaration. A mechanism that lets a coordinator hide a red behind a stale declaration loses that red for good. |
| any | `GREEN`, or the other declarable state | `DECLARED STALE` — the file is wrong. Not a fault, or the mechanism would punish the fix it exists to accommodate. |

Only `NONE` and `BLOCKED` are declarable. `GREEN` is the one that matters: honouring it would
let one line of this file switch the whole monitor off, and it is refused loudly because the
attempt is itself the finding. A file that cannot be read, or naming a state or a repository
that cannot be resolved, is reported under NEEDS ATTENTION — never swallowed the way
`_expected_red`'s `except Exception: return set()` swallows.

A declared repository is still classified under the verdict GitLab actually gives it, even when
the declaration is stale, because dropping it from its own section would make the report quieter
every time a declaration went out of date. A declaration may add a section; it may never remove
one. And the summary states how many pipelines actually verified `main`, because relocating
rows out of `blocked` and `unverifiable` empties both tallies and three zeros read as a pass in
a family where nothing ran.

**And `notify-coordinator.py` now does the same thing**, which it did not when this was merged.
The first version of this mechanism reached one half of one report: the log got tidy, the wake-up
body did not, and eleven repositories kept arriving every thirty minutes through the other door.
The reader now lives in `scripts/expected_ci.py` and both scripts call it, so the two halves of one
report cannot file the same repository into different sections — which matters because `notify` is
the half that interrupts a human, and a red filed in one report and hidden in the other is a red
lost.

## An "all clear" here means all clear

`fleet-keepalive.py` and `notify-coordinator.py` are two halves of one report and this script's
word is the shared one. Neither may print a reassuring sentence while a recorded worker is
`NOT-STARTED`, a repository's `main` is `RED`, or the main sweep's record says `main` was not
verified — missing, stale, unreadable or `NO-GATE` — and when it does print one it names
what it checked — an unexplained all-clear is indistinguishable from a stopped coordinator.

Every action is rate-limited to one per pane per `--cooldown` minutes, so a wedged worker generates one
message every cooldown rather than one every 30 minutes.

## Why it reads --json

It consumes `fleet-watch.py --json`, never the printed table. A wide table invites reading a number out
of the wrong column, and that has already produced a confident claim about a bug that did not exist.
The table is for eyes; this is for a program.

Exit status is 0 whenever the sweep ran, so cron does not mail on every run. The log is the signal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
META = HERE.parent
WATCH = HERE / "fleet-watch.py"
# The workspace layout and the state paths come from `factory_config` — the environment first
# (cron sets the state overrides), then factory.json, then the shipped defaults.
sys.path.insert(0, str(HERE))
import factory_config  # noqa: E402

STATE = Path(os.environ.get("FLEET_KEEPALIVE_STATE", str(factory_config.path("keepalive_state"))))

def _expected_red() -> set[str]:
    """Worktrees whose red gate is the ticket rather than a fault.

    `media`'s golden path was reverted from main and rebuilt on a branch that
    fails on purpose, because the ticket IS the conflict between two merged features.
    Without this the keepalive reports the same GATE-RED every thirty minutes for as long
    as the work takes, and a report that is always true is one nobody reads.

    Declared as {"worktrees": ["<abs path>"]} in FLEET_EXPECTED_RED, written when the
    branch is created and removed when it lands.
    """
    path = Path(os.environ.get("FLEET_EXPECTED_RED", str(factory_config.path("expected_red"))))
    try:
        return set(json.loads(path.read_text()).get("worktrees", []))
    except Exception:
        return set()

EXPECTED_RED = _expected_red()

# The reader, the declarable states, the refusals and the placement rule all moved to
# `scripts/expected_ci.py`, because `notify-coordinator.py` needs exactly the same four and **two
# declaration readers that disagree is the failure this repository keeps hitting** — each script
# correct about its own fixture and neither able to see the other's answer. The mechanism reached
# one half of one report; the fix for that has to be a shared reader, not a second copy.
#
# A copy would also have been the faster mistake. `_expected_ci()`, `DECLARABLE` and `REFUSED` were
# right here, correct, and already tested by `test_fleet_keepalive.py`; the two names of
# `repositories` and `DECLARABLE` would have been retyped into `notify-coordinator.py` and drifted
# on the first edit. Break the module and both suites go red — which is what shows a mechanism has
# one reader rather than two that happen to agree today. Nothing is re-exported here: there are no
# remaining callers of the moved names, and an alias kept "for compatibility" with a version that
# never shipped is just a second name to drift.
#
# Imported BY PATH, because cron runs this file by absolute path with a bare `PATH` and no
# `PYTHONPATH`: a sibling import cannot be assumed there, and a failed import would take the whole
# sweep down rather than skipping one section of its report.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import expected_ci  # noqa: E402  — after the sys.path insert above, which is the point
# The main sweep's record, read through the same shared module notify-coordinator.py
# uses, so the two halves of one report place a sweep row identically.
import main_sweep_state  # noqa: E402  — after the sys.path insert above, which is the point


def _expected_ci() -> tuple[dict[str, dict], list[str]]:
    """CI states a coordinator has declared, by repository, and any refusal that reading it hit.

    A thin name over `expected_ci.read_declarations()`, kept because this file calls it and
    renaming the call sites would be churn with no gain. The behaviour and the whole argument for
    it live in the module — read that docstring rather than this one.
    """
    return expected_ci.read_declarations()


MANIFEST = Path(os.environ.get("FLEET_MANIFEST", str(factory_config.path("fleet_manifest"))))
LOG = Path(os.environ.get("FLEET_KEEPALIVE_LOG", str(factory_config.path("keepalive_log"))))


def git(cwd: str, *args: str) -> str:
    """Run git in a worktree and return stdout, or "" on any failure.

    Empty rather than raising, because every caller is asking a question where "could not
    find out" and "the answer is no" lead to the same next step — and a rebuild that dies
    halfway is worse than one that leaves a worktree out.
    """
    res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)
    return res.stdout.strip() if res.returncode == 0 else ""


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # the log must never be the reason the sweep fails
        print(f"  (log write failed: {exc})")


def sweep() -> list[dict]:
    out = subprocess.run([sys.executable, str(WATCH), "--json"],
                         capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        log(f"fleet-watch --json failed (exit {out.returncode}): {out.stderr.strip()[:300]}")
        return []
    try:
        return json.loads(out.stdout).get("panes", [])
    except json.JSONDecodeError as exc:
        log(f"fleet-watch --json produced unparseable output: {exc}")
        return []


def agent_alive(pane: str) -> bool:
    """Is there an agent in this pane at all?

    A pane recorded in the manifest whose agent is gone is the one failure the coordinator
    cannot see from a sweep, because the sweep only walks worktrees that exist.
    """
    res = subprocess.run(["herdr", "agent", "get", pane], capture_output=True, text=True)
    if res.returncode != 0:
        return False
    try:
        blob = res.stdout[res.stdout.index("{"):]
        agent = json.loads(blob).get("result", {}).get("agent") or {}
    except (ValueError, KeyError):
        return False
    return bool(agent.get("agent_status")) and agent["agent_status"] != "unknown"


def restart_agent(pane: str, worktree: str) -> bool:
    log(f"  no agent in {pane} — restarting one in {worktree}")
    res = subprocess.run(["herdr", "agent", "start", f"keepalive-{pane.split(':')[0]}",
                          "--kind", "opencode", "--pane", pane, "--timeout", "180000"],
                         capture_output=True, text=True)
    if res.returncode != 0:
        log(f"  restart failed for {pane}: {res.stdout.strip()[:200]} {res.stderr.strip()[:200]}")
        return False
    # A restarted agent knows nothing. Tell it what it was doing rather than leaving an
    # empty prompt box, which is indistinguishable from a worker that never started.
    time.sleep(20)
    ask = ("Coordinator keepalive: your agent session was restarted, so you have no earlier "
           "context. Re-read AGENTS.md, then run `git log --oneline origin/main..HEAD` and "
           "`git status --porcelain` in your worktree to recover where you were. If you had "
           "uncommitted work it is still on disk — commit it. Then continue the ticket.\n\n"
           f"Worktree: {worktree}")
    subprocess.run(["herdr", "agent", "prompt", pane, ask], capture_output=True, text=True)
    log(f"  agent restarted in {pane} and told to recover from git")
    return True


def rebuild_manifest() -> int:
    """Reconstruct the manifest from what is on disk.

    The manifest is the record of which worktrees count as live, and nothing else can
    rebuild it — a sweep walks worktrees it already knows about, so a lost manifest makes
    every worker invisible rather than merely unknown.

    Two things this has to get right, both learned the hard way:

    - **A worker mid-rebase is invisible to `git branch --show-current`.** During a rebase
      HEAD is detached, so the branch comes back empty and the worktree looks unowned.
      That is the worst possible moment to lose sight of one, so the rebase's own
      `head-name` is read instead. Found when a social-studio rebase vanished from the
      fleet during exactly the conflict resolution it was doing.
    - **A pane whose agent died is still a live worktree**, and the restart path above is
      what brings it back — so it belongs in the manifest even with no usable agent yet.
    """
    out = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True)
    if out.returncode != 0:
        log("herdr agent list failed; cannot rebuild")
        return 1
    try:
        agents = json.loads(out.stdout[out.stdout.index("{"):])["result"]["agents"]
    except (ValueError, KeyError) as exc:
        log(f"could not read the agent list: {exc}")
        return 1

    workspace = str(Path(__file__).resolve().parent.parent.parent)

    # Panes by worktree, so a live worktree whose agent has died can still be found.
    pane_for = {}
    for a in agents:
        cwd = (a.get("cwd") or "").rstrip("/")
        if cwd.startswith(workspace + "/"):
            pane_for[cwd] = a["pane_id"]

    # The worktrees are the source of truth, not the agent list. Enumerating agents alone
    # loses every worktree whose agent never started or has died — which is exactly the
    # case the restart path exists to fix, so those are the ones that must be visible.
    # Found when a freshly dispatched worker with no agent yet stayed invisible.
    manifest = {}
    for repo in sorted(Path(workspace).iterdir()):
        candidates = [repo] + sorted(p for p in repo.iterdir() if p.is_dir()) if repo.is_dir() else []
        for r in candidates:
            if not (r / ".git").exists() and not (r / "HEAD").is_file():
                continue
            listing = git(str(r), "worktree", "list", "--porcelain")
            if not listing:
                continue
            current = None
            for line in listing.splitlines() + [""]:
                if line.startswith("worktree "):
                    current = line[len("worktree "):].strip()
                elif line.startswith("branch ") and current:
                    branch = line[len("branch "):].strip().removeprefix("refs/heads/")
                    if not branch.startswith(factory_config.branch_prefix()):
                        current = None
                        continue
                    wt = Path(current)
                    if not wt.is_dir():
                        current = None
                        continue
                    # Mid-rebase: HEAD is detached, so read the name the rebase restores.
                    resolved = git(str(wt), "branch", "--show-current") or branch
                    manifest[str(wt)] = pane_for.get(str(wt).rstrip("/"))
                    current = None

    if not manifest:
        log(f"rebuild found no {factory_config.branch_prefix()}* worktree with a pane "
            f"— check herdr, not the manifest")
        return 1
    # Union both sources. `git worktree list` finds a worktree whose agent has died; the
    # agent list finds a worktree that is mid-rebase, where HEAD is detached and the
    # listing reports no branch at all. Each misses exactly what the other catches, and
    # between them they found every worker during the rebuild that followed a wiped /tmp.
    for a in agents:
        cwd = (a.get("cwd") or "").rstrip("/")
        if cwd.startswith(workspace + "/") and os.path.isdir(cwd):
            branch = git(cwd, "branch", "--show-current")
            if not branch:
                gitdir = git(cwd, "rev-parse", "--git-dir")
                head = Path(gitdir) / "rebase-merge" / "head-name" if gitdir else None
                if head and head.exists():
                    branch = head.read_text().strip().removeprefix("refs/heads/")
            if branch.startswith(factory_config.branch_prefix()):
                manifest.setdefault(cwd, a["pane_id"])

    known = {k: v for k, v in manifest.items() if v}
    orphans = sorted(k for k, v in manifest.items() if not v)
    if not known:
        log(f"rebuild found no {factory_config.branch_prefix()}* worktree with a pane "
            f"— check herdr, not the manifest")
        return 1
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(known, indent=1))
    log(f"rebuilt {MANIFEST} from {len(known)} worktree(s) with a pane")
    for wt, pane in sorted(known.items()):
        log(f"  {wt.replace(workspace + '/', ''):<32} {pane}")
    for wt in orphans:
        log(f"  ORPHAN (no pane): {wt.replace(workspace + '/', '')} — "
            "reopen it with `herdr worktree open`, or remove it if the ticket is done")
    return 0


def prompt(pane: str, text: str) -> None:
    subprocess.run(["herdr", "agent", "prompt", pane, text], capture_output=True, text=True)


COMMIT_NUDGE = (
    "Coordinator keepalive: your tree has been dirty and unchanged across several checks. An "
    "uncommitted tree is indistinguishable from no work and one `git gc` from gone. Please "
    "`git add -A && git commit && git push`, then keep going. Small commits are free; an "
    "uncommitted hour is not recoverable. If you are blocked, say what on."
)

STALL_NUDGE = (
    "Coordinator keepalive: you have no commits and your tree has not moved. Either you are "
    "blocked, or you have not started. If blocked, name the blocker in one line — a blocked "
    "worker is a scheduling problem the coordinator can solve. If you have the brief, begin "
    "and commit early and often."
)

# Deliberately NOT the stall nudge, and the difference is the point.
#
# A stalled worker has been *working* and stopped; it needs a nudge. This one has consumed no
# tokens and has committed nothing, so it has never run a turn — its brief never registered.
# Measured 2026-10-03: the worker whose only job was to fix a red pipeline sat at `ahead=0
# dirty=0` with a pane showing `3 MCP failed`, and the keepalive said "no worker is ready to
# merge and none is failing". Nothing was going to change on its own, because nothing had begun.
#
# So this prompt carries the brief itself. A worker that never started does not have one to act
# on, and telling a worker that has no brief to begin is a message to nobody — which is how this
# state can sit through eight stall checks and then be reported as health.
NOT_STARTED_NUDGE = (
    "Coordinator keepalive: you have produced nothing since dispatch — no commits, no changes and "
    "no tokens consumed — so the brief that was sent to this pane never registered and you have "
    "not run a turn. Nothing is wrong with your worktree; there is no work yet.\n\n"
    "Do this, in order:\n"
    "{ticket}"
    "2. Re-read `AGENTS.md` in your worktree.\n"
    "3. Begin, and commit early and often: `git add -A && git commit && git push` as soon as "
    "there is anything working.\n\n"
    "If you were started and then lost your context, say so in one line rather than waiting — a "
    "blocked worker is a scheduling problem the coordinator can solve, and a silent one is a "
    "fleet slot going to waste."
)

# How a worker reads its ticket. Provider-specific because the CLI is: `gh` carries its own
# credential store, `glab` needs the workspace profile sourced first.
_TICKET_STEPS = {
    "gitlab": "1. `glab issue view <iid>` for your ticket — the brief is in the issue, not here. "
              "If glab answers 403, `source scripts/glab-env.sh` first.\n",
    "github": "1. `gh issue view <iid>` for your ticket — the brief is in the issue, not here.\n",
}


def not_started_prompt(branch: str) -> str:
    """The never-started re-prompt, naming the ticket read off the branch.

    The iid is taken from `issue-<iid>-<slug>` rather than carried in the manifest, because the
    manifest is a worktree-to-pane map and this is the one thing derivable from data that is
    already there. When the branch does not carry one the message says so rather than printing
    a bare `<iid>` the worker would have to guess at — a prompt with an unsubstituted placeholder
    in it reads as a broken tool and is ignored, which is the state this whole path exists to
    escape.
    """
    m = re.match(rf"{re.escape(factory_config.branch_prefix())}(\d+)", branch or "")
    if not m:
        iid = ("(your branch name carries no issue number, so ask the coordinator which "
               "ticket this is)")
    else:
        iid = m.group(1)
    ticket = _TICKET_STEPS.get(factory_config.tracker_provider(), _TICKET_STEPS["gitlab"])
    return NOT_STARTED_NUDGE.replace("{ticket}", ticket).replace("<iid>", iid)


def pipeline_verdicts(names: list[str] | None = None) -> tuple[list[dict] | None, str]:
    """The family's pipeline verdicts, by running the configured CI reader as a script.

    The reader is the flavor `factory_config.tracker_provider()` selects — `gitlab_ci.py`
    (the default) or `github_ci.py`. Read as JSON and never by scraping the table: a wide table
    invites reading a number out of the wrong column, which has produced a confident claim about
    a bug that did not exist in this project. A failure to run is `(None, reason)` and is never
    an empty list — "CI could not be read" and "CI says nothing is wrong" must not print the same
    way, and the first is what `UNKNOWN` exists for.
    """
    # Under its REAL name, selected by the provider. The old call said `gitlab-ci.py`: a caller
    # that resolves a path that does not exist reports "could not be read" forever while a test
    # that *renames* the file on copy passes — a test that reconstructs the filename is a test
    # asserting itself.
    reader = "github_ci.py" if factory_config.tracker_provider() == "github" else "gitlab_ci.py"
    script = HERE / reader
    if not script.exists():
        return None, f"{script} is missing, so CI was not read at all"
    cmd = [sys.executable, str(script), "--json"]
    if names:
        cmd += [arg for name in names for arg in ("--repo", name)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=60 + 20 * (len(names) if names else 12))
    except subprocess.TimeoutExpired:
        return None, f"{reader} did not finish in time, so CI state is unknown"
    if run.returncode != 0:
        return None, f"{reader} exited {run.returncode}: {(run.stderr or run.stdout).strip()[:200]}"
    try:
        return json.loads(run.stdout).get("repos", []), ""
    except json.JSONDecodeError as exc:
        return None, f"{reader} produced unparseable output: {exc}"


def signature(rec: dict) -> str:
    return f"{rec['verdict']}|{rec['ahead']}|{rec['dirty']}|{rec['status']}"


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    except Exception as exc:
        log(f"  (state write failed: {exc})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stall", type=int, default=2,
                    help="unchanged checks before a worker counts as stalled")
    ap.add_argument("--cooldown", type=int, default=45,
                    help="minutes before the same pane is acted on again")
    ap.add_argument("--dry-run", action="store_true", help="report only, send nothing")
    ap.add_argument("--rebuild", action="store_true",
                    help="reconstruct the manifest from herdr and the worktrees, then exit")
    args = ap.parse_args()

    if args.rebuild:
        return rebuild_manifest()

    panes = sweep()
    state = load_state()
    now = time.time()

    if not panes:
        log("sweep returned nothing — either every worker is gone, or fleet-watch failed")
        log(f"  check {MANIFEST}; the manifest is what makes a worktree count as live")
        return 0

    log(f"fleet: {len(panes)} pane(s) recorded in the manifest")
    acted = 0
    ready: list[str] = []
    problems: list[str] = []
    expected_lines: list[str] = []
    # The two reds, in their own sections, because collapsing them is the defect.
    ci_blocked: list[str] = []
    ci_red: list[str] = []
    ci_unverifiable: list[str] = []
    # Three more buckets, all fed by the same rule and none of them optional.
    ci_declared: list[str] = []      # a declared state that still matches what GitLab says
    ci_declared_stale: list[str] = []  # a declaration GitLab has outgrown — remove or update it
    ci_declared_broken: list[str] = []  # the declaration file itself: unreadable or unresolvable
    # How many pipelines actually verified `main`. Counted from the survey itself rather than
    # derived from the other buckets, because it must stay correct no matter how a row is filed —
    # including by a declaration, which can move a row but must never make it count as verified.
    ci_verified_green = 0

    declared, decl_errors = _expected_ci()
    for err in decl_errors:
        ci_declared_broken.append(err)
        problems.append(f"declared CI state: {err}")
    if declared:
        log(f"declared CI states: {len(declared)} (from {os.environ.get('FLEET_EXPECTED_CI', 'the default path')})")

    # Read GitLab CI *before* the all-clear decision below, and let it veto it. Reading it after
    # would produce the exact sentence this ticket exists to remove: a reassuring line printed by
    # a script that had, moments earlier, been handed the fact it was reassuring about.
    ci_rows, ci_why = pipeline_verdicts()
    if ci_why or ci_rows is None:
        ci_unverifiable.append(f"GitLab CI could not be read at all: {ci_why} — so this run "
                               f"knows NOTHING about whether main is green. That is not a pass")
        log(f"gitlab CI: UNVERIFIABLE — {ci_why}")
    else:
        seen_repos: set[str] = set()
        for row in ci_rows:
            repo, verdict = row["repo"], row["verdict"]
            seen_repos.add(repo)
            if verdict == "GREEN":
                ci_verified_green += 1
            line = f"{repo:<26} {verdict:<7} {(row.get('sha') or '-'):<9} {row['why']}"
            claim = declared.get(repo)

            # THE DECLARATION IS CHECKED, NEVER TRUSTED — and the order is the whole point.
            #
            # The convenient implementation is `if claim: relocate(line); continue`, and that is
            # exactly what would swallow a red: `social-studio` is declared `NONE` and
            # gains a pipeline later, the declaration is stale, and a red lands behind it. So a
            # declaration only ever relocates a row **while it still matches**, and the mismatch is
            # reported in three escalating shapes rather than one.
            if claim:
                if verdict == claim["state"]:
                    ci_declared.append(
                        f"{line}\n      declared: {claim['because']}")
                    continue
                # It does not match. Which way it moved decides how loudly it is reported, and
                # "worse" has to be louder than the same state undeclared — a mechanism that lets
                # a coordinator hide a red behind a stale declaration loses that red for good.
                stale = (f"{line}\n      DECLARED STALE: this was declared {claim['state']} "
                         f"({claim['because']}), and GitLab now says {verdict}")
                if verdict == "RED":
                    # Worse. A job ran and failed behind a declaration that said nothing was
                    # running, so it is a fault AND the file is wrong. Both halves go to
                    # NEEDS ATTENTION, and neither is optional: reporting only the red leaves the
                    # coordinator believing their declaration still works, so they fix the code,
                    # leave the file, and the next red arrives behind an unchanged claim.
                    ci_red.append(line)
                    problems.append(f"main pipeline RED: {line}")
                    problems.append(stale)
                    ci_declared_broken.append(
                        f"{repo}: declared {claim['state']} and is now RED — the declaration is "
                        f"stale AND a job ran and failed. This is worse than no declaration, "
                        f"because it hides the red behind a claim: {claim['because']}")
                else:
                    # Better, or merely different: not a fault. A declaration that went stale
                    # favourably still has to be reported, because the file is now lying — and the
                    # next transition could go the other way. Punishing it as a fault would make
                    # the mechanism fight the fix it exists to accommodate.
                    ci_declared_stale.append(stale)
                    # …AND the repository is still reported under the verdict GitLab actually
                    # gives it. This is the quiet version of the same trap: reporting a stale
                    # `BLOCKED` only under DECLARED STALE would drop it out of the CI BLOCKED
                    # section *and* out of the blocked count, so the report would get quieter
                    # every time a declaration went stale. A declaration may add a section; it may
                    # never remove one. So the real verdict is classified as if nothing had been
                    # declared, and the stale file is reported on top of it.
                    #
                    # The bucket itself comes from `expected_ci.place()`, the same call
                    # `notify-coordinator.py` makes, so the two halves of one report cannot file the
                    # same row in different sections. That is not tidiness: `notify` is what wakes
                    # a human, and two readers disagreeing about which bucket a row belongs in is a
                    # red filed in one report and hidden in the other.
                    placed = expected_ci.place(repo, verdict, claim)
                    if placed["bucket"] == expected_ci.BLOCKED:
                        ci_blocked.append(line)
                    elif placed["bucket"] == expected_ci.UNVERIFIED:
                        # `NONE` and `UNKNOWN` both mean main was not verified, and so does a
                        # verdict this reader does not recognise — dropping an unrecognised one
                        # would be silence. Kept apart inside gitlab-ci.py, which knows why.
                        ci_unverifiable.append(line)
                continue

            placed = expected_ci.place(repo, verdict, None)
            if placed["bucket"] == expected_ci.RED:
                ci_red.append(line)
            elif placed["bucket"] == expected_ci.BLOCKED:
                ci_blocked.append(line)
            elif placed["bucket"] == expected_ci.UNVERIFIED:
                # `NONE` — no pipeline on main — is grouped with `UNKNOWN` here because both mean
                # the same thing for a decision: main was not verified. They are kept apart inside
                # gitlab-ci.py, which knows the difference; this report only needs to know whether
                # to trust the pipeline.
                ci_unverifiable.append(line)

        # A declaration naming a repository this run did not read is also unchecked, and it is the
        # one nothing else can catch: the survey is the registry, so a renamed or removed repository
        # leaves a declaration pointing at nothing and no row to compare it against.
        for line in expected_ci.unseen(declared, seen_repos):
            ci_declared_broken.append(line)
        if ci_red:
            # A real code defect. `problems`, not a section of its own: unlike BLOCKED this is a
            # fault, and the existing response to a fault is "NEEDS ATTENTION".
            for line in ci_red:
                if not any(line in p for p in problems):
                    problems.append(f"main pipeline RED: {line}")
            log(f"gitlab CI: {len(ci_red)} repository pipeline(s) RED — a job ran and failed")
        if ci_blocked:
            log(f"gitlab CI: {len(ci_blocked)} repository pipeline(s) BLOCKED — the shared runners "
                f"have no compute minutes, so no job ever started. Not a code defect.")
        if ci_unverifiable:
            log(f"gitlab CI: {len(ci_unverifiable)} repository pipeline(s) unverifiable")
        if ci_declared:
            log(f"gitlab CI: {len(ci_declared)} declared and still true — reported, not cleared")
        if ci_declared_stale:
            log(f"gitlab CI: {len(ci_declared_stale)} declaration(s) GitLab has outgrown")

    # The main sweep's last completed run, read from its record — never re-gated here. The
    # full sweep takes minutes and this script runs every thirty, so the report consumes what the
    # sweep already decided. The classification (and the "absence is not a pass" rule) lives in
    # the shared module, which is also what notify-coordinator.py calls.
    ms = main_sweep_state.classify_run()
    ms_inconsistent = list(ms["inconsistent"])
    ms_unverified = list(ms["unverified"])
    # A RED row is a fault, in the same shape as a CI RED: it goes to NEEDS ATTENTION and into
    # the tally, not into a section a reader has to know to look for.
    for line in ms["red"]:
        problems.append(f"main sweep RED: {line}")
    for line in ms_inconsistent:
        problems.append(f"main sweep TOOL-INCONSISTENT (fix the sweep, not the row): {line}")
    if ms["red"] or ms_inconsistent:
        log(f"main sweep: {len(ms['red'])} repository(ies) RED on origin/main, "
            f"{len(ms_inconsistent)} self-inconsistent")
    if ms_unverified:
        log(f"main sweep: {len(ms_unverified)} state(s) unverified — not a pass")

    # The sweep walks worktrees that exist. A worker whose worktree was deleted therefore
    # vanishes from the report with no row and no error — the one disappearance that looks
    # identical to "it finished". So compare the manifest against the sweep, and say so
    # when they disagree. This is the check that the thing which should still be there is.
    try:
        manifest = json.loads(MANIFEST.read_text())
    except Exception:
        manifest = {}
    if manifest:
        swept = {r.get("pane") for r in panes if r.get("pane")}
        missing = sorted(p for p in manifest.values() if p and p not in swept)
        if missing:
            problems.append(
                f"{len(missing)} pane(s) in the manifest did not appear in the sweep — "
                f"worktree gone or branch merged and cleaned up without the manifest "
                f"being pruned: {', '.join(missing)}")

    for rec in panes:
        pane = rec.get("pane")
        wt = rec.get("worktree", "")
        if not pane:
            continue
        key = pane
        sig = signature(rec)
        st = state.get(key, {})

        st["same"] = st.get("sig") == sig
        st["streak"] = st.get("streak", 0) + 1 if st.get("sig") == sig else 1
        st["sig"] = sig
        state[key] = st

        cooldown_left = args.cooldown * 60 - (now - st.get("last_action", 0))
        may_act = cooldown_left <= 0 and not args.dry_run

        # A recorded pane whose agent is gone is invisible to the sweep, which only walks
        # worktrees that exist. This is the failure a coordinator reading a table misses.
        if not agent_alive(pane):
            if may_act:
                acted += 1
                st["last_action"] = now
                restart_agent(pane, wt)
            else:
                problems.append(f"{pane}: no agent in the pane (restart in {int(cooldown_left)}s)")
            continue

        # A branch can be red on purpose — the ticket may BE a conflict between two merged
        # features. Reporting that every run makes the report always-true, and an
        # always-true report is one nobody reads.
        if rec.get("worktree") in EXPECTED_RED:
            expected_lines.append(f"{pane}  {rec['branch']}  red ON PURPOSE "
                                  f"(declared expected-red): {rec.get('detail','')[:120]}")
            continue

        if rec["verdict"] in ("MERGEABLE", "GATE-RED", "NO-GATE"):
            line = (f"{pane}  {rec['branch']}  {rec['verdict']}  "
                    f"ahead={rec['ahead']} dirty={rec['dirty']}  {rec.get('why','')}")
            # detail carries the gate error, which matters for GATE-RED, carries the
            # instruction for NO-GATE, and only repeats the `why` for MERGEABLE.
            if rec["verdict"] in ("GATE-RED", "NO-GATE") and rec.get("detail"):
                line += f"\n      -> {rec['detail']}"
            (ready if rec["verdict"] == "MERGEABLE" else problems).append(line)
            continue

        if rec["verdict"] == "TOOL-INCONSISTENT":
            problems.append(f"{pane}: fleet-watch is self-inconsistent — fix the script, not the row")
            continue

        stalled = st["streak"] >= args.stall

        # The never-started rule, and the branch that used to fall through to the stall rule below.
        #
        # `NOT-STARTED` is not a long stall, it is an absent worker, and the two are separated
        # here for the reason the verdict exists: a worker with no commits has been ambiguous
        # between "finished" and "never began", and this loop's stall rule — which needs
        # `--stall` *unchanged checks* — is built for a worker that was doing something. Applied
        # to a pane that never ran a turn it waits two checks to send a nudge that a third
        # message would have fixed immediately, and in between it reports nothing at all.
        #
        # So this path does not wait. It reports on the first sighting and re-prompts on the
        # cooldown like every other action, so a wedged worker produces one message per cooldown
        # rather than one per run.
        if rec["verdict"] == "NOT-STARTED":
            problems.append(f"{pane}: {rec['branch']} — NEVER STARTED: "
                            f"{rec.get('progress_why', 'no tokens, no commits, no diff')}")
            if may_act:
                acted += 1
                st["last_action"] = now
                prompt(pane, not_started_prompt(rec["branch"]))
                log(f"  {pane}: re-prompted — it never ran a turn")
        elif rec["ahead"] == 0 and rec["status"] in ("idle", "done") and stalled:
            problems.append(f"{pane}: {rec['branch']} — 0 commits, unchanged {st['streak']} checks")
            if may_act:
                acted += 1
                st["last_action"] = now
                prompt(pane, STALL_NUDGE)
        elif rec["dirty"] > 0 and stalled:
            problems.append(f"{pane}: {rec['branch']} — {rec['dirty']} uncommitted, "
                            f"unchanged {st['streak']} checks")
            if may_act:
                acted += 1
                st["last_action"] = now
                prompt(pane, COMMIT_NUDGE)

    save_state(state)

    if ready:
        log("")
        log("READY TO MERGE — a human does this. Never this script:")
        for line in ready:
            log(f"  {line}")
    if ci_blocked:
        log("")
        log("CI BLOCKED — red because the runners have no compute minutes, NOT a code defect.")
        log("  Nothing in these repositories was verified. This is billing; fixing code will not")
        log("  change it, and reading this as a red pipeline would send someone to the wrong place.")
        for line in ci_blocked:
            log(f"  {line}")
    if ci_unverifiable:
        log("")
        log("CI UNVERIFIABLE — not a pass:")
        for line in ci_unverifiable:
            log(f"  {line}")
    if ci_declared_stale:
        log("")
        log("DECLARED STALE — GitLab and the declaration file disagree, so one of them is wrong.")
        log("  Not a fault and not good news: the file is out of date, and the next change in this")
        log("  repository could go the other way. Update the declaration or remove it.")
        for line in ci_declared_stale:
            log(f"  {line}")
    if ci_declared:
        log("")
        log("CI DECLARED — a coordinator wrote this state down, so it is not new information.")
        log("  It is still NOT a pass: main was not verified in these repositories, and this file")
        log("  is a claim this script cannot check. It is reported, not cleared.")
        for line in ci_declared:
            log(f"  {line}")
    if expected_lines:
        log("")
        log("RED ON PURPOSE — not a fault, not waking anyone:")
        for line in expected_lines:
            log(f"  {line}")
    if ci_declared_broken:
        log("")
        log("DECLARED STATE UNUSABLE — the declaration file itself is the problem here:")
        for line in ci_declared_broken:
            log(f"  {line}")
    if ms_unverified:
        log("")
        log("MAIN SWEEP UNVERIFIED — not a pass:")
        log("  The latest recorded run of the main sweep did not verify these, and `main` is the")
        log("  ref every branch is created from — so this is not the same fact as 'swept and green':")
        for line in ms_unverified:
            log(f"  {line}")
    if problems:
        log("")
        log("NEEDS ATTENTION:")
        for line in problems:
            log(f"  {line}")

    # The all-clear, and the two things that must be true before it can be printed at all.
    #
    # The never-started rule measured the failure: the most reassuring sentence this tool can emit was true of
    # neither thing that mattered — a worker that had never run a turn, and a red `main`. Both are
    # now loaded into `problems` above, so this branch is only reachable when they are empty. The
    # `unverifiable` half is the subtler one and is why it is a separate condition: a *fault* is
    # something to fix, whereas "GitLab could not be read" is something to *know*, and a script
    # that silently drops it is back to answering a question nobody asked.
    if not ready and not problems and not acted:
        # The declaration mechanism, and THE TRAP. A declaration is a claim, so it is one more reason this report is not
        # clear — never one fewer.
        #
        # The asymmetry is deliberate and it is the safety property: **declaring can only ever make
        # this report louder, never quieter.** Nine `BLOCKED` repositories are clear today, because
        # The rule refused to let a billing problem make this tool permanently non-clear. A coordinator
        # who then declares all nine gets a *less* clear report than one who declared nothing — and
        # that is right. Undeclared `BLOCKED` is a fact this script read and judged not to be a
        # fault; a declared one is an assertion in a JSON file that nothing can check, and a
        # monitor that has been silenced by a claim is worse than a noisy one, because it is
        # trusted.
        #
        # So the declaration buys tidiness and nothing else. If a future change makes declaring
        # *clear* the report, that is this ticket's failure being reintroduced, and the two tests
        # named `test_declaring_every*_is_not_a_clear_report` in `test_fleet_keepalive.py` exist to
        # catch it — one for each declarable state, because they fail for different reasons.
        declared_pending = len(ci_declared) + len(ci_declared_stale) + len(ci_declared_broken)
        if ci_unverifiable or declared_pending or ms_unverified:
            log("")
            # "NOT clear" rather than "NOT all clear", for the same reason as in notify-coordinator.py: the
            # longer phrasing contains the substring `all clear`, so anything checking whether
            # this log claimed an all-clear — a test, a grep, a person — is satisfied by the
            # sentence that denies one.
            if declared_pending:
                log(f"NOT clear: {declared_pending} CI state(s) are DECLARED rather than verified. "
                    f"A declaration is a coordinator's claim, not a pass — declaring a state "
                    f"relocates it out of the noise, and it never clears this report.")
            if ci_unverifiable:
                log(f"NOT clear: {len(ci_unverifiable)} repository pipeline(s) could not be "
                    f"judged, so 'no job is failing' is not a fact this run can support")
            if ms_unverified:
                log(f"NOT clear: {len(ms_unverified)} main-sweep state(s) are not verified — the "
                    f"latest recorded run is missing, too old to trust, unreadable, or could not "
                    f"verify a repository's main. `main` is the ref every branch is created from, "
                    f"so that is not a pass.")
        else:
            log("")
            log("all workers healthy and moving; nothing to do")
            log("  checked: every recorded worker swept, its pane's agent alive, its tree's gate "
                "state, every repository's pipeline on main, and the latest recorded main sweep")
            if ci_blocked:
                log(f"  note: {len(ci_blocked)} pipeline(s) are BLOCKED on CI minutes — that is "
                    f"not a fault, but nothing there was verified")

    log(f"acted on {acted} pane(s); {len(ready)} ready, {len(problems)} needing attention; "
        f"CI {len(ci_red)} red / {len(ci_blocked)} blocked / {len(ci_unverifiable)} unverifiable"
        f" / {len(ci_declared)} declared / {len(ci_declared_stale)} declared-stale"
        f" / {len(ci_declared_broken)} declared-unusable; "
        f"main sweep {len(ms['red'])} red / {len(ms_inconsistent)} inconsistent / "
        f"{len(ms_unverified)} unverified")

    # The number the tally above stopped implying, printed as its own figure.
    #
    # The declaration mechanism relocated eleven repositories into `CI DECLARED`, and that emptied both `blocked` and
    # `unverifiable` — so the summary read `0 red / 0 blocked / 0 unverifiable` for a family in
    # which no pipeline had run at all. Three zeros is what a healthy summary looks like. That is
    # the quietest version of the silencer this ticket is about: nothing was removed from the
    # report, but the one line a reader is most likely to quote stopped describing reality.
    #
    # So the count that used to be implied is stated. `GREEN` is the only verdict that verifies
    # `main`, and a declared one never is — so this figure is the honest denominator and it is
    # the one number here that cannot be inflated by a declaration.
    surveyed = len(ci_rows) if ci_rows else 0
    if surveyed:
        log(f"main verified on {ci_verified_green} of {surveyed} repository pipeline(s)"
            f" — a declared state is never a verification, so it does not count here")
    if ms["swept"]:
        # The sweep's own denominator: rows it actually gated in the last completed run.
        # Printed even when a row is RED — "7 of 10 green" is the honest figure, not a pass.
        log(f"main swept and green on {ms['verified_green']} of {ms['swept']} repository "
            f"verdict(s) in the last recorded main sweep — read from the sweep's record, not "
            f"re-gated here")
    return 0


if __name__ == "__main__":
    sys.exit(main())