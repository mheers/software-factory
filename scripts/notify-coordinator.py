#!/usr/bin/env python3
"""notify-coordinator — wake the coordinator when the fleet has something for it.

Runs from the same cron entry as `fleet-keepalive.py`, immediately after it. Split into its
own script so the wake-up can be tested, rate-limited and reasoned about on its own, and so
a failure in one does not take the other down.

## Why a prompt and not a notification

A desktop notification tells a human something happened. It does not make an agent session
resume. `herdr agent prompt` against the coordinator's pane is what actually puts the session
back to work — it arrives as user input, which is the only thing an idle session reacts to.

So: **prompt the pane**, and use a notification only as a redundant second channel for a human
who is looking at the screen.

## The label is not unique, and the cwd is the scope

`AGENTS.md` documents the convention: the coordinator's pane is labelled `AI · coordinator`. The
convention is *this repository's*, and **other projects on this machine use the same label** —
measured, three panes carry it (`wX:p2` project A, `w3A:p1` project B, `w1C:p1` project C). The
resolution loop used to return the first labelled pane *with a detected agent*, machine-wide, and
204 of 205 logged wake-ups landed on this workspace's pane — but only because `herdr pane list`
returns it first. **Ordering is luck, not a guard:** if this workspace's pane is not the first live
labelled one (its agent not detected, the workspace recreated under a later number, or the label
moved), the wake-up is typed into another project's coordinator session as user input. That is
measured, not hypothetical: another project's notifier carried the identical label-only match
and its first live run woke *this* coordinator pane.

So the match is label **and** cwd — the workspace root, or a directory under it — and a
labelled pane elsewhere is reported (`labelled elsewhere, not touched`) and never prompted. A
wrong wake-up is worse than a missing one, because it looks like it worked.

## Why it is rate-limited by the CONTENT, not by the clock

A cron job that wakes the coordinator every 30 minutes for the same unmerged branch is worse
than no cron job at all. The second time it fires the coordinator learns the alert is noise,
and a check you have learned to ignore is a check you stop reading — which is the cost that
matters, not the wake-up.

So the state file records a **hash of what is actionable**, not a timestamp. The coordinator is
woken when the set of ready-to-merge panes *changes*:

- something new became mergeable → wake, because there is new work;
- the same thing is still mergeable → stay quiet, because I already know;
- everything got merged and the fleet is idle → say so **once**, so "nothing happened" is
  distinguishable from "nobody looked".

That last case matters more than it sounds. Silence and "all clear" are different facts, and a
keepalive that cannot tell you which one it is reporting is not reporting.

## It never prompts about its own noise

Workers that the keepalive has already nudged are excluded by the same hash, so a wedged worker
does not generate a coordinator wake-up on every run. The keepalive's own log is where that
belongs.

## Why a *stall* is not that noise

The rule above suppresses **repeating an unchanged readiness**, and it is right to: a branch that
stays mergeable is a fact the coordinator already has. A worker stuck at zero commits is not
that, and hashing it as if it were is a false pass — the signature of a stalled pane is
byte-identical on every run, so it is suppressed forever, which is precisely the thing that
most needs a human.

That is this file's second instance of one bug. The first version filtered on `MERGEABLE` alone,
so "nothing is ready" read as "nothing needs you" and it announced all-clear while a worker sat
`GATE-RED`. That was fixed by widening the actionable set to `GATE-RED` / `TOOL-INCONSISTENT` /
`NO-GATE` — **by kind of failure, but not by duration of it.** The invariant is that silence and
all-clear are different facts, and a stall violates it in exactly the direction the first bug did.

So a stall is counted here, from this script's own state, and enters the signature with its check
count. A growing stall is new information on each cycle, which is what makes it different from a
branch that stays mergeable.

## Two things about counting that are easy to get wrong

**A run that stays quiet must still count.** If a quiet run returns without saving, the streaks
freeze at whatever they were, a pane below the threshold never reaches it, and the escalation
never fires — the same suppression, moved one function away. So every real run persists the
streaks, including the runs that report nothing.

**A dry run persists nothing at all.** Not the signature, not the streaks. `--dry-run` exists to
be looked at; a run that cannot send must not advance a counter that decides whether to send.

## The three things an all-clear must be true about

This script emits the most reassuring sentence the tool can produce, and on 2026-10-03 it emitted
it while **neither** of the two things it implies was true: a worker had never run a turn, and
`main` was red in GitLab. So the all-clear is now conditional on three things rather than one:

1. **every recorded worker was swept** (existing — `unaccounted_panes()`);
2. **no worker is `NOT-STARTED`** — a pane that has consumed no tokens and committed nothing is
   not running, and "not running" is the state most likely to be mistaken for health;
3. **every repository's pipeline on `main` was judged** — and `GREEN` is the only value that
   counts as judged. A pipeline `BLOCKED` because the runners have no compute minutes is **not a
   fault and not a pass**, and a pipeline that could not be read is not a pass either.

The body names what was checked, because an unexplained all-clear is indistinguishable from a
coordinator that stopped. And CI state that is not green is reported in its own section rather
than folded into a count, for the reason `gitlab_ci.py` exists: every pipeline in this family is
currently red because of billing, and a monitor that reports that as "everything is failing" is
one that gets switched off.

## It reads the declarations too, and declaring can only make this LOUDER

The declaration mechanism, and the gap it created in turn. `fleet-keepalive.py` reads
`.fleet-state/expected-ci.json` and relocates eleven declared repositories out of its noise. **This
script did not read it at all** — it read GitLab itself and emitted the same eleven repositories in
the same voice every thirty minutes, through the other door. So the report that was tidied is a log
nobody reads, and the component that *actually wakes the coordinator* kept the original alarm. A
mechanism that reached one half of a report and not the other has not removed the noise; it has
moved it.

So this script now reads the same state file, through the same reader (`scripts/expected_ci.py`),
and applies the same placement rule. **Relocate, never clear**: a matching declaration moves its
line into a `DECLARED` section of its own and nowhere else.

**And the direction of the whole thing is the safety property.** A declaration is a claim, and
nothing here can check a claim, so it *withholds* the all-clear rather than granting it. Declaring
can only ever make this report louder, never quieter. That is worth stating plainly because the
alternative is a silencer: a coordinator writes nine lines into a JSON file and the tool that wakes
a human goes quiet for a family in which no pipeline has run — which is worse than the noise it
replaced, because a silencer is trusted.

Which is why a red behind a stale declaration is reported in **both** places here, as loudly as in
the keepalive: a job that ran and failed is a code defect whatever a coordinator's file says, and
`notify` is the path that interrupts somebody.

## It reads the main sweep's last completed run too

`main_sweep.py` gates every repository's `origin/main` hourly from cron. Until its
findings lived only in `/tmp/opencode/main-sweep.cron.log`, which nothing reads — measured on
2026-10-06, `social-studio`'s main was red for three hours while the sweep gated it seven
times. A red that only reaches a log is a red nobody acts on, and this is the path that interrupts
a person, so the sweep's record is read here through `main_sweep_state.py` (the same module
`fleet-keepalive.py` uses) and it joins the same three decisions:

- a `RED` row is a **fault** and wakes the coordinator, carrying the repository, the commit and
  the sweep's own `first_useful_line` of the failure;
- a `TOOL-INCONSISTENT` row is a fault in the sweep itself, and the fix is above the repository;
- a missing, stale or unreadable record — or a `NO-GATE` row — is **not a pass**, and it withholds
  the all-clear exactly as an unreadable GitLab read does. `main` is the ref every branch is
  created from; "not swept" and "swept and green" must not read the same in the one message that
  a coordinator actually sees.

The record is read, never re-gated: the full sweep takes minutes and this script runs every thirty
minutes. And the sweep's placement joins the content hash, so a red appearing is news while an
unchanged one stays quiet — without that, the first appearance of a red would be suppressed as
"unchanged since the last wake-up", which is the sweep record restored through the mechanism built to fix it.

## The standing question, in every body (owner instruction, 2026-10-07)

Every wake-up this script sends — the all-clear, a red `main`, a ghost, a ready branch, a
stall — ends with the same question: *is there work available to schedule in parallel that is
not already in flight?* It is a question and never an answer: what to schedule is the
coordinator's judgement, and the policy lives in `AGENTS.md`. One constant
(`PARALLEL_QUESTION`) so the bodies cannot drift, and `test_notify_coordinator.py` asserts it
in every shape — the all-clear alone was the gap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
META = HERE.parent

# The shared declaration reader. Imported BY PATH rather than assumed on `sys.path`, because
# cron runs this file by absolute path with a bare `PATH` and no `PYTHONPATH` — a sibling import
# cannot be assumed there, and a failed import would take the whole wake-up down rather than
# skipping one feature.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import expected_ci  # noqa: E402  — after the sys.path insert above, which is the point
# The main sweep's record, through the same shared module fleet-keepalive.py uses, so
# the two halves of one report cannot place a sweep row differently.
import main_sweep_state  # noqa: E402  — after the sys.path insert above, which is the point
import factory_config  # noqa: E402  — the same seam the layout below resolves through

MANIFEST = Path(os.environ.get("FLEET_MANIFEST", str(factory_config.path("fleet_manifest"))))
STATE = Path(os.environ.get("FLEET_NOTIFY_STATE", str(factory_config.path("notify_state"))))
LOG = Path(os.environ.get("FLEET_KEEPALIVE_LOG", str(factory_config.path("keepalive_log"))))
WORKSPACE = str(factory_config.workspace_root())
# The convention in AGENTS.md, now actually resolvable. The label lives on the *pane*; the agent
# list does not carry it, which is why the cwd heuristic was the only thing available until now.
COORDINATOR_LABEL = "AI · coordinator"

# The standing question, sent with EVERY wake-up — the all-clear, a red `main`, a ghost, a
# ready branch, a stall (owner instruction, 2026-10-07). "Always ask whether there is work to
# schedule in parallel" is the operational half of the rule in AGENTS.md, and putting the
# QUESTION here rather than the answer keeps the division the family wrote down: this script
# reports facts and asks; deciding what to schedule is the coordinator's, because it means
# reading the tracker, knowing the capacity and judging interference. The phrase list in
# `test_notify_coordinator.py` (`test_the_body_carries_no_scheduling_policy`) is asserted
# absent from this text, so it must stay free of "dispatch", "ceiling" and the rest.
PARALLEL_QUESTION = (
    "One question is always worth asking, whatever this message says: is there work "
    "available to schedule in parallel that is not already in flight — an open issue "
    "nobody is on, or a slot that could carry one? `agent_status` will not tell you: an "
    "idle agent and a finished one look identical, so the worktrees and the issue tracker "
    "are the evidence, not this message.\n\n"
    "Nothing here is a decision. It is a reminder to go and look."
)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  notify: {msg}"
    print(line)
    try:
        with LOG.open("a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def sweep() -> list[dict]:
    out = subprocess.run([sys.executable, str(HERE / "fleet-watch.py"), "--json"],
                         capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        log(f"fleet-watch failed (exit {out.returncode})")
        return []
    try:
        return json.loads(out.stdout).get("panes", [])
    except json.JSONDecodeError as exc:
        log(f"unparseable sweep output: {exc}")
        return []


def running_agents() -> list[dict]:
    res = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True)
    if res.returncode != 0:
        log("herdr agent list failed")
        return []
    try:
        return json.loads(res.stdout[res.stdout.index("{"):])["result"]["agents"]
    except (ValueError, KeyError) as exc:
        log(f"could not read the agent list: {exc}")
        return []


def coordinator_pane_by_label(workers: set[str]) -> str | None:
    """The pane *labelled* `AI · coordinator` **at the workspace root**, or None.

    `herdr agent list` does not expose `label`; `herdr pane list` does. So the label is read from
    the pane list and joined to an agent by pane id, which is also how a labelled pane with no
    agent running is told apart from one that has a coordinator in it.

    **The label is not unique on this machine, so the cwd is part of the match.** Three
    panes carry `AI · coordinator` here — this workspace's and two others' — and
    `herdr pane list` returns them in creation order, which is luck rather than a guard. A pane
    labelled elsewhere is reported and never returned, so the caller falls back to its own cwd
    heuristic instead of prompting another project's coordinator session.

    Returns None rather than guessing, so the caller falls back to the cwd heuristic instead of
    reading "no label here" as "no coordinator".
    """
    res = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True)
    if res.returncode != 0:
        return None
    try:
        panes = json.loads(res.stdout[res.stdout.index("{"):])["result"]["panes"]
    except (ValueError, KeyError) as exc:
        log(f"could not read the pane list: {exc}")
        return None
    live = {a.get("pane_id") for a in running_agents()}
    elsewhere = []
    for p in panes:
        if (p.get("label") or "").strip() != COORDINATOR_LABEL:
            continue
        pid = p.get("pane_id")
        cwd = (p.get("cwd") or "").rstrip("/")
        if not (cwd == WORKSPACE or cwd.startswith(WORKSPACE + "/")):
            # Another project's coordinator. `herdr pane list` is machine-wide and the label is
            # a shared convention, so this is where a wrong wake-up would come from; it is
            # collected for the report below and never returned. A pane whose cwd cannot be
            # read lands here too — not ours is the safe reading, and a wrong wake-up is worse
            # than a missing one.
            elsewhere.append(f"{pid} ({cwd or 'cwd unknown'})")
            continue
        if pid in workers:
            # A worker cannot be the coordinator whatever it is labelled. Asking for the
            # coordinator by label must not be able to wake a worker's pane.
            log(f"pane {pid} is labelled {COORDINATOR_LABEL!r} but is a recorded worker — ignoring")
            continue
        if pid in live:
            return pid
    if elsewhere:
        # Say which labelled panes exist elsewhere; silence here is indistinguishable from
        # "no coordinator session is up at all", and the refusal must be a fact in the log
        # rather than the absence of one.
        log(f"no {COORDINATOR_LABEL!r} pane in {WORKSPACE} "
            f"({len(elsewhere)} labelled elsewhere, not touched: {', '.join(elsewhere)})")
    return None


def find_coordinator_pane() -> str | None:
    """The coordinator is a pane at the workspace root that is NOT a recorded worker.

    Resolved every run rather than hardcoded. A hardcoded pane id is a lie waiting for the
    next session: when this session ends and another takes its place, the id changes and a
    stale value would either wake nothing or wake the wrong session — and the second is worse,
    because it looks like it worked.
    """
    res = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True)
    if res.returncode != 0:
        log("herdr agent list failed")
        return None
    try:
        agents = json.loads(res.stdout[res.stdout.index("{"):])["result"]["agents"]
    except (ValueError, KeyError) as exc:
        log(f"could not read the agent list: {exc}")
        return None

    workers = set()
    try:
        workers = set(json.loads(MANIFEST.read_text()).values())
    except Exception:
        pass

    # **The label first, because the cwd heuristic is not a contract.**
    #
    # Measured 2026-10-05: three live agents sat at the workspace root, none of them a recorded
    # worker, and the nudge went to the right one *only because that pane was focused*. Focus is
    # a property of the next click, not a fact about the role. Two of those panes are other
    # opencode sessions that are not workers and never will be, so "not a worker" does not
    # narrow the field to one.
    #
    # `herdr agent list` carries no `label` field at all — the label lives on the *pane* — so the
    # documented convention (the coordinator's pane is labelled `AI · coordinator`) was not
    # resolvable from the data this function was reading. That is the whole reason a nudge can
    # silently go to the wrong session: the resolver looked at a field the contract does not
    # name. So it asks `pane list`, where the label exists.
    #
    # A wrong wake-up is worse than a missing one, because it looks like it worked.
    #
    # The cwd scope: the label is a *shared* convention, not this repository's alone, so the label match
    # itself is scoped by cwd. A labelled pane in another project returns None from here — which
    # is what lets this cwd fallback run instead of a foreign session being prompted.
    labelled = coordinator_pane_by_label(workers)
    if labelled:
        return labelled

    candidates = [a for a in agents
                  if (a.get("cwd") or "").rstrip("/") == WORKSPACE
                  and a.get("pane_id") not in workers
                  and a.get("agent_status") not in ("unknown", None)]
    if not candidates:
        return None
    # Prefer the focused one; otherwise the first, which is stable per tab ordering.
    candidates.sort(key=lambda a: (not a.get("focused"), a.get("pane_id", "")))
    return candidates[0]["pane_id"]


def expected_red_worktrees() -> set[str]:
    """Worktrees whose redness is the ticket, not a fault.

    A branch can be deliberately red: `media`'s golden path was reverted from main
    and rebuilt on a branch that fails on purpose, because the ticket *is* the conflict
    between two merged features. Without this, the keepalive reports that branch GATE-RED
    every thirty minutes for as long as the work takes — and a report that is always true
    is a report nobody reads.

    Declared in `.fleet-state/expected-red.json` as {"worktrees": ["<abs path>"]},
    written by the coordinator when the branch is created and removed when it lands.
    """
    try:
        return set(json.loads(Path(os.environ.get(
            "FLEET_EXPECTED_RED", str(HERE.parent / ".fleet-state" / "expected-red.json"))).read_text()).get("worktrees", []))
    except Exception:
        return set()


STALLED_VERDICTS = ("NOTHING", "IN-FLIGHT")
# A worker that never ran a turn is NOT a stalling worker — it is not running at all,
# and the stall counter's threshold (8 unchanged checks, four hours) is a long time to spend on a
# pane whose brief never registered. So it is actionable on sight, and it is a separate set from
# `stalling` because the action is different: read the pane and re-prompt, rather than wait.
NOT_STARTED_VERDICT = "NOT-STARTED"


def pipeline_verdicts() -> tuple[list[dict] | None, str]:
    """The family's pipeline verdicts, by running the configured CI reader — or `(None, reason)`.

    The reader is the flavor `factory_config.tracker_provider()` selects: `gitlab_ci.py`
    (the default) or `github_ci.py`. Read as JSON and never by scraping the table, for the same
    reason the sweep is: a wide table invites reading a number out of the wrong column, and that
    has produced a confident claim about a bug that did not exist in this project.

    `(None, reason)` is not an empty list, and the difference is the whole point of the third
    all-clear condition: "CI could not be read" and "CI says nothing is wrong" must not produce
    the same report.
    """
    # Under its REAL name, selected by the provider. The old call said `gitlab-ci.py`, so the CI
    # half of the all-clear never ran — the same defect as in fleet-keepalive.py, and invisible
    # because the test copied the file to the wrong name so the fixture agreed with the caller.
    # Both callers are fixed together for that reason.
    reader = "github_ci.py" if factory_config.tracker_provider() == "github" else "gitlab_ci.py"
    script = HERE / reader
    if not script.exists():
        return None, f"{script} is missing, so CI was not read at all"
    try:
        run = subprocess.run([sys.executable, str(script), "--json"],
                             capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, f"{reader} did not finish in time, so CI state is unknown"
    if run.returncode != 0:
        return None, (f"{reader} exited {run.returncode}: "
                      f"{(run.stderr or run.stdout).strip()[:200]}")
    try:
        return json.loads(run.stdout).get("repos", []), ""
    except json.JSONDecodeError as exc:
        return None, f"{reader} produced unparseable output: {exc}"


def ci_sections(rows: list[dict] | None, why: str, declared: dict | None = None,
                refusals: list[str] | None = None) -> dict:
    """Split pipeline verdicts into the sections this script reports separately.

    `GREEN` is the only pass. `BLOCKED` — the runners have no compute minutes — is neither a fault
    nor a pass, and it is kept apart from `RED` because every pipeline in this family is currently
    in that state: reported as red it would be permanently red, and a monitor that is always red
    is a monitor nobody reads. `NONE` and `UNKNOWN` both mean "main was not verified", which is
    all this script needs to know; the difference between a repository with no pipeline and one
    whose pipeline could not be read is preserved inside `gitlab_ci.py`, which is where it can be
    acted on.

    The declaration mechanism adds three more, and the placement itself comes from `expected_ci.place()` so the two
    scripts cannot disagree about which bucket a repository belongs in. The rule the caller depends
    on is **relocate, never clear**:

    | declared | observed | section |
    |---|---|---|
    | matching | same state | `declared` — reported in its own section, and it *withholds* the all-clear |
    | any | `RED` | `red`, **and** `stale`, **and** `unusable` — a red behind a claim is louder, not quieter |
    | any | `BLOCKED` | `blocked` — still reported, still not verified |
    | any | `GREEN` / `RUNNING` | the row goes quiet, but `stale` does not: the file is wrong |
    | any | `UNKNOWN` / `NONE` | `unverified` |

    A declaration naming a repository this run did not read lands in `unusable`, and a file that
    could not be read at all lands there too. Both are claims nothing can check, and both are
    reported — never swallowed into an empty declaration, which is how a typo in the state file
    would un-declare eleven repositories with nothing saying why.
    """
    out = {"red": [], "blocked": [], "unverified": [], "declared": [], "stale": [],
           "unusable": list(refusals or [])}
    if rows is None:
        out["unverified"].append(why or "GitLab CI could not be read")
        return out
    declared = declared or {}
    seen: set[str] = set()
    for row in rows:
        repo, verdict = row.get("repo"), row.get("verdict")
        seen.add(repo)
        line = f"{repo} {verdict} {row.get('sha') or ''} — {row.get('why','')}".strip()
        claim = declared.get(repo)
        placed = expected_ci.place(repo, verdict, claim)

        if placed["stale"]:
            out["stale"].append(f"{line}\n      DECLARED STALE: this was {placed['stale']}")
        if placed["unusable"]:
            out["unusable"].append(placed["unusable"])

        bucket = placed["bucket"]
        if bucket == expected_ci.DECLARED:
            out["declared"].append(f"{line}\n      declared: {claim['because']}")
        elif bucket == expected_ci.RED:
            out["red"].append(line)
        elif bucket == expected_ci.BLOCKED:
            out["blocked"].append(line)
        elif bucket == expected_ci.UNVERIFIED:
            out["unverified"].append(line)
        # `SILENT` — a `GREEN` or `RUNNING` row, and the row is genuinely quiet. The stale claim
        # above it is still reported, because a declaration that has stopped matching must not be
        # able to go quiet by way of a repository turning green.
    out["unusable"].extend(expected_ci.unseen(declared, seen))
    return out


def ci_placement_key(sections: dict) -> str:
    """A stable summary of how every repository was placed, for the wake-up's content hash.

    ## Why this is in the hash, and what breaks if it is not

    A declaration that turns into a real `RED` is the event this path exists to not lose, and it is
    a **change** — not a state. The content hash decides whether a run is news or a repeat, and
    without the CI placement a pane set that has not moved hashes identically, so the red's first
    appearance is reported as "unchanged since the last wake-up, staying quiet". The mechanism would
    report the red in the keepalive log every thirty minutes and interrupt nobody about it.

    So the placement is hashed, and **not** the line text: a `sha`, a pipeline id and a job count
    change on every commit, and hashing those would turn this into a wake-up per push — the exact
    alert the declaration mechanism was built to remove. Repository, bucket and declared state change only when the
    *situation* changes, which is what "news" means.
    """
    parts = []
    for key in ("red", "blocked", "unverified", "declared"):
        parts += [f"{key}:{line.split(' ', 1)[0]}" for line in sections.get(key) or []]
    for line in sections.get("stale") or []:
        parts.append(f"stale:{line.split(' ', 1)[0]}")
    for key in ("unusable",):
        parts += [f"{key}:{line.split(' ', 1)[0]}" for line in sections.get(key) or []]
    return "|".join(sorted(parts))


def row_signature(rec: dict) -> str:
    """What "changed" means for one row.

    Deliberately the same shape `fleet-keepalive.py` counts with (`signature()` there), so
    both scripts agree on when a worker stopped moving. Two scripts counting a stall with
    two different definitions would produce two different answers to "is it stuck?".
    """
    return f"{rec.get('verdict')}|{rec.get('ahead')}|{rec.get('dirty')}|{rec.get('status')}"


def below_threshold(panes: list[dict], streaks: dict, stalled: int) -> list[str]:
    """Panes that are stalling but not yet past the threshold, with their counts.

    Reported so a stay-quiet line is readable. Without this, a worker at 1 unchanged
    check and one at 7 look identical from the log, and the only way to tell that the
    escalation is *approaching* is to read the state file.
    """
    expected = expected_red_worktrees()
    out = []
    for r in panes:
        if r.get("worktree") in expected or r.get("verdict") not in STALLED_VERDICTS:
            continue
        streak = (streaks.get(r.get("pane") or "?") or {}).get("streak", 0)
        if streak < stalled:
            out.append(f"{r.get('pane')} {r.get('branch')} unchanged {streak} of "
                       f"{stalled} check(s) to a stall wake-up")
    return out


def unaccounted_panes(panes: list[dict]) -> list[str]:
    """Recorded workers the sweep did not walk at all.

    The sweep only walks worktrees that exist. A worker whose worktree was deleted — merged and
    cleaned up, or lost to a Herdr restart — therefore produces **no row and no error**, which is
    indistinguishable from "it finished and was reclaimed". `fleet-keepalive.py` reports these as
    "did not appear in the sweep"; this is the same fact, read from the same manifest, so the two
    components cannot disagree about whether the fleet is whole.

    Measured 2026-10-05: the keepalive logged `4 needing attention` and this script printed
    "all clear. No action needed" **0.2 seconds later**, from the same run. Two sentences that
    cannot both be true, and the coordinator was told by one of them that there was nothing to see.
    A pane nobody is watching is not a pane that is fine.
    """
    try:
        workers = json.loads(MANIFEST.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    swept = {r.get("pane") for r in panes}
    out = []
    for worktree, pane in sorted(workers.items(), key=lambda kv: str(kv[1])):
        if pane not in swept:
            out.append(f"{pane} {worktree}")
    return out


def actionable(panes: list[dict], streaks: dict, stalled: int
               ) -> tuple[str, list[str], list[str], list[str], dict, list[str]]:
    """What needs the coordinator, split by kind — including by *duration*.

    The first version filtered on `MERGEABLE` alone, which made the all-clear a statement
    about *readiness* while reading as a statement about *health* — so a worker sitting
    GATE-RED, and two workers with large uncommitted trees, were reported as "nothing is
    stalled, 8 panes and moving". That is precisely the failure this whole file exists
    against: a check reporting something other than the truth.

    So GATE-RED counts. It is a real failure with a first line of output attached, and it is
    invisible until someone reads a table. `TOOL-INCONSISTENT` counts for the same reason,
    more strongly: it means this tooling is lying and the fix is above everything else.

    `IN-FLIGHT` and `NOTHING` are *not* actionable on sight — a worker mid-edit or one that
    has simply not committed yet is the normal state of a worker that is working, and waking
    the coordinator for those would be a cry of wolf. But that is a claim about a moment, and
    a worker that is **still** in that state after `stalled` unchanged checks is a claim
    about a duration, and it is the duration that is the defect. Measured on 2026-10-05: a
    pane sat at zero commits for 8 consecutive checks, and because its row was byte-identical
    every run it never entered the hash at all — while the sweep and the keepalive, in the same
    run, both reported it.

    So the third set is the stalling panes, and each entry carries its check count, which is
    what makes a growing stall a changing signature. De-escalation is automatic: the streak
    resets the moment the row moves, so a worker that commits leaves the set and the
    signature goes quiet again. Escalation must not latch.
    """
    # A deliberately-red branch is not a wake-up. It is the ticket.
    expected = expected_red_worktrees()
    ready, broken, stalling, never_started = [], [], [], []
    next_streaks: dict = {}
    for r in panes:
        pane = r.get("pane") or "?"
        sig = row_signature(r)
        prev = streaks.get(pane) or {}
        streak = prev.get("streak", 0) + 1 if prev.get("sig") == sig else 1
        next_streaks[pane] = {"sig": sig, "streak": streak}

        if r.get("worktree") in expected:
            continue
        entry = f"{r['pane']} {r['branch']}"
        if r.get("verdict") == "MERGEABLE":
            ready.append(entry)
        elif r.get("verdict") == NOT_STARTED_VERDICT:
            # Actionable on sight and NOT a stall: a stall is a duration, and this
            # state has no duration to measure because nothing has begun. Its row is also
            # byte-identical on every run, so the content-hash that suppresses a repeat would
            # suppress it forever — the same false pass the stall dimension was added to fix,
            # one step further along.
            #
            # So it enters the signature without a check count, which means "the set is
            # non-empty" and nothing more: it is reported when the *set* of not-started panes
            # changes, exactly like `ready`, which is the right cadence for a state that either
            # gets fixed or does not.
            never_started.append(f"{entry} NOT-STARTED ahead={r.get('ahead')} dirty={r.get('dirty')} "
                                 f"— {r.get('progress_why', 'no tokens, no commits, no diff')}")
        elif r.get("verdict") in ("GATE-RED", "TOOL-INCONSISTENT", "NO-GATE"):
            # NO-GATE counts for the same reason as GATE-RED and for a sharper one: the
            # sweep could not verify anything, so "ready to merge" on that row would be a
            # claim nobody checked. It is not ready. It is unverified, and it needs a human
            # to run the deliverable by hand — which is a thing only the coordinator can do.
            broken.append(f"{entry} {r['verdict']}")
        elif r.get("verdict") in STALLED_VERDICTS and streak >= stalled:
            # The check count is IN the entry, not just in the set: a wake-up the
            # coordinator cannot act on is not a wake-up, and "unchanged 9 checks" is the
            # part it can act on.
            stalling.append(f"{entry} {r['verdict']} unchanged {streak} check(s) "
                            f"ahead={r.get('ahead')} dirty={r.get('dirty')}")
    sig = hashlib.sha256("|".join(ready + broken + stalling + never_started).encode()).hexdigest()[:16]
    return sig, ready, broken, stalling, next_streaks, never_started


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
        log(f"state write failed: {exc}")


def persist(state: dict, dry_run: bool) -> None:
    """Save, unless this run was only looking.

    A dry run exists to be looked at, and the signature it computes is the decision about
    whether to *send*. Letting it save would mean the next real run believes the wake-up
    was already delivered — so a `--dry-run` before a real run silences that real run.

    This is why the streaks are persisted through here too: a quiet run that returns
    without saving freezes the counter, a pane below the threshold never reaches it, and
    the escalation silently never fires. The bug would be identical to the one being fixed,
    one function further down.
    """
    if not dry_run:
        save_state(state)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what it would wake the coordinator for, send nothing")
    ap.add_argument("--force", action="store_true", help="wake regardless of whether it changed")
    ap.add_argument("--quiet", action="store_true",
                    help="stay silent unless something is ready — used for the 'all clear' repeat")
    ap.add_argument("--stalled", type=int, default=8,
                    help="unchanged checks before a stalling pane is worth a wake-up "
                         "(default 8, matching fleet-keepalive.py --stall)")
    args = ap.parse_args()

    panes = sweep()
    if not panes:
        log("no panes — nothing to wake for (is fleet-watch ok?)")
        return 0

    state = load_state()
    pane_sig, ready, broken, stalling, streaks, never_started = actionable(
        panes, state.get("streaks") or {}, args.stalled)
    previous = state.get("sig")
    sig = pane_sig
    state["ready"] = ready
    state["broken"] = broken
    state["stalling"] = stalling
    state["never_started"] = never_started
    state["streaks"] = streaks
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # Read GitLab CI *before* the all-clear decision, and let it veto it.
    #
    # Reading it afterwards would reproduce the exact defect: a reassuring sentence printed by a
    # script that had, moments earlier, been handed the fact it was reassuring about. Measured on
    # 2026-10-03 — "all clear. No worker is ready to merge and none is failing" while
    # `media` main was red in GitLab and its repair worker had never run a turn.
    ci_rows, ci_why = pipeline_verdicts()
    declared, decl_refusals = expected_ci.read_declarations()
    ci = ci_sections(ci_rows, ci_why, declared, decl_refusals)
    state["ci"] = {k: len(v) for k, v in ci.items()}
    # How much was actually *verified*. Counted from the survey itself rather than derived from the
    # other buckets, so it stays correct no matter how a row is filed — including by a declaration,
    # which can move a row but must never make it count as verified. The same figure
    # `fleet-keepalive.py` prints, and for the same reason: a declaration that empties three
    # counters leaves a tally that reads exactly like health.
    ci_surveyed = len(ci_rows) if ci_rows else 0
    ci_verified_green = sum(1 for row in (ci_rows or []) if row.get("verdict") == "GREEN")

    # The main sweep's last completed run, through the shared module. Read here for exactly
    # the reason GitLab CI is read here, and not after the decision below: the reassuring sentence
    # must not be printed by a script that was handed, moments earlier, the fact it is reassuring
    # about. A RED row is a fault; the reds and the sweep's own self-contradiction join the
    # wake-up path, and an unverified record (missing, stale, unreadable, NO-GATE) is not a pass.
    ms = main_sweep_state.classify_run()
    ms_red = list(ms["red"])
    ms_inconsistent = list(ms["inconsistent"])
    ms_unverified = list(ms["unverified"])
    state["main_sweep"] = {"red": len(ms_red), "inconsistent": len(ms_inconsistent),
                           "unverified": len(ms_unverified), "swept": ms["swept"],
                           "verified_green": ms["verified_green"]}

    # The declaration joins the content hash, so a *change* to it is news and an unchanged one
    # is a repeat. Without this the eleven declared repositories would relay verbatim into the body
    # of every wake-up — the every-30-minutes alert the mechanism was built to remove, arriving on the other
    # door — and, worse, a declared state turning into a real `RED` would be suppressed as
    # "unchanged" on the path whose whole job is not losing reds.
    #
    # The sweep record folds in for the same reason, and it is load-bearing in the same
    # direction: a run whose only change is a RED appearing must hash differently, or its first
    # appearance is suppressed as "unchanged" and the mechanism built to surface it is the one
    # that hides it.
    sig = hashlib.sha256(
        f"{sig}|ci={ci_placement_key(ci)}|declared={sorted(declared)}"
        f"|main_sweep={main_sweep_state.placement_key(ms)}".encode()).hexdigest()[:16]
    # Stored only AFTER the CI placement has been folded in. Assigning it next to the pane hash
    # would persist the pane-only value, so the next run would compare a composite signature
    # against a partial one, find them unequal on every run, and wake the coordinator forever —
    # which is exactly the alert the rate limit exists to prevent, produced by the fix for it.
    state["sig"] = sig

    ghosts = unaccounted_panes(panes)
    # Four conditions, and the two new ones are the ticket. A `never_started` pane is not running,
    # and a red or unreadable `main` is not healthy — so neither can coexist with this sentence.
    # The declaration mechanism adds a fifth: a DECLARED state. **A declaration withholds this sentence; it never grants
    # it.** The direction is the safety property — a report an unchecked JSON file can switch on is
    # a silencer, and a silencer is worse than the noise it replaced, because it is trusted.
    # The sweep record adds the sixth: a recorded main sweep that is missing, stale, unreadable or red.
    ci_vetoed = bool(ci["red"] or ci["unverified"])
    declared_pending = len(ci["declared"]) + len(ci["stale"]) + len(ci["unusable"])
    ms_vetoed = bool(ms_red or ms_inconsistent or ms_unverified)
    if not ready and not broken and not stalling and not ghosts and not never_started \
            and not ci_vetoed and not declared_pending and not ms_vetoed:
        # "All clear" is a fact, and it is worth saying once per transition into it.
        # Silence and all-clear are different, and a log that says nothing cannot tell the
        # coordinator which one it is looking at.
        #
        # The wording deliberately does NOT promise to be the last one. It promised that
        # once and was wrong: the set went ready → empty → ready → empty as tickets were
        # merged and dispatched, and each transition is legitimately news.
        if previous != "ALL-CLEAR" and not args.dry_run and not args.quiet:
            pane = find_coordinator_pane()
            if pane:
                # A reminder, not a plan. This script reports what it can see; it does not
                # decide what work exists, what the capacity is, or what to dispatch next.
                # An earlier version spelled the policy out here — "dispatch in this same
                # turn", "a merge frees a slot" — which turned a reminder into a coordinator,
                # and put scheduling policy in the place that runs it every 30 minutes rather
                # than in AGENTS.md, which is where a convention belongs and where a person
                # can read and change it.
                #
                # So: state the facts, and point at the question. Answering it is the
                # coordinator's, and answering it is a judgement no script can make.
                # The body names what it checked. An unexplained all-clear is
                # indistinguishable from a stopped coordinator — which is precisely how the
                # 2026-10-03 one was read, and it was wrong.
                blocked_note = ""
                if ci["blocked"]:
                    blocked_note = (f"\n\nNote, and it is NOT a fault: {len(ci['blocked'])} "
                                    f"repository pipeline(s) are BLOCKED because the shared runners "
                                    f"have no compute minutes, so no job ever started. Nothing in "
                                    f"them was verified. That is billing — do not go looking for a "
                                    f"code defect, and do not treat it as a reason to stop:\n"
                                    + "\n".join(f"  {line}" for line in ci["blocked"][:12]))
                body = (f"fleet keepalive: all clear. Nothing is ready to merge, nothing is "
                        f"failing, nothing is stalled, no worker has failed to start, and every "
                        f"recorded worker was swept. The fleet is {len(panes)} pane(s).\n\n"
                        f"What was actually checked, so this is not a bare reassurance:\n"
                        f"  - {len(panes)} recorded worker(s) swept, every one with a live agent\n"
                        f"  - no pane with zero tokens, zero commits and zero changes (a "
                        f"pane that never ran a turn reads as an idle one, which is what this "
                        f"check exists to separate)\n"
                        f"  - every repository's pipeline on main, read from GitLab and "
                        f"classified. None is RED and none is unreadable.\n"
                        f"  - the latest recorded main sweep ({ms['swept']} record(s), read from "
                        f"the sweep's own record and not re-gated here): no repository's "
                        f"origin/main is RED, and the record is current.\n"
                        f"{blocked_note}\n\n"
                        f"{PARALLEL_QUESTION}")
                subprocess.run(["herdr", "agent", "prompt", pane, body],
                               capture_output=True, text=True)
                log(f"woke {pane} with the all-clear")
                state["sig"] = "ALL-CLEAR"
                persist(state, args.dry_run)
                return 0
        state["sig"] = "ALL-CLEAR"
        persist(state, args.dry_run)
        # The declared count is in the tally, because relocating rows out of `blocked` and
        # `unverified` empties both — and `0 red / 0 blocked / 0 unreadable` is what a healthy
        # summary looks like. That is the quietest version of a silencer: nothing was removed from
        # the report, but the one line a reader quotes stopped describing reality.
        log(f"{len(panes)} pane(s), all clear — none ready, none failing, none stalled, "
            f"none never-started, none unaccounted, none DECLARED; CI {len(ci['red'])} red / "
            f"{len(ci['blocked'])} blocked / {len(ci['unverified'])} unreadable; main sweep "
            f"{len(ms_red)} red / {len(ms_unverified)} unverified")
        log(f"main verified on {ci_verified_green} of {ci_surveyed} repository pipeline(s) — a "
            f"declared state is never a verification, so it does not count here")
        log(f"main swept and green on {ms['verified_green']} of {ms['swept']} repository "
            f"verdict(s) in the last recorded main sweep")
        return 0

    # A never-started worker, a red/unreadable `main`, or a DECLARED state, when nothing else is
    # actionable. Given its own section rather than folded into `broken`, because the response is
    # different in every case and a reader who is told "1 failing" reaches for the wrong one: a
    # pane that never ran a turn needs a re-prompt, a red `main` needs a gate re-run, and a
    # declaration needs the file to be updated or deleted. The sweep record adds one more: a
    # RED row needs a worker on a branch, and a missing/stale one needs the sweep looked at.
    if never_started or ci["red"] or ci["unverified"] or declared_pending \
            or ms_red or ms_inconsistent or ms_unverified:
        # The phrasing is deliberate and load-bearing: "NOT clear" rather than "not all clear", so
        # the sentence cannot contain the substring `all clear`. A reader — or a test, or a
        # grep — checking whether this body claimed an all-clear must not be satisfied by a
        # sentence that denies one. The first draft of this said "NOT all clear" and a test
        # asserting its absence found it.
        #
        # The count of things is computed rather than written as "Two", because the declaration mechanism added a third
        # and a hardcoded "Two" is a sentence that becomes false without anybody noticing — the
        # reader is told two things were checked and is looking at three.
        withheld = (len(never_started) > 0) + (len(ci["red"]) + len(ci["unverified"]) > 0) \
            + (declared_pending > 0) \
            + (len(ms_red) + len(ms_inconsistent) + len(ms_unverified) > 0)
        body_lines = [f"fleet keepalive: the fleet is quiet but NOT clear. {withheld} thing(s) "
                      f"this run checked that a clear report would have implied were fine:"]
        if never_started:
            body_lines += ["", f"{len(never_started)} worker(s) have NEVER RUN A TURN:"]
            body_lines += [f"  {line}" for line in never_started]
            body_lines += [
                "",
                "A pane with zero tokens, zero commits and zero changes is not a finished worker "
                "and not a stalled one — it is a worker whose brief never registered. Read the pane "
                "before assuming anything about it, then re-prompt. The keepalive has already sent "
                "one re-prompt on its cooldown.",
            ]
        if ci["red"]:
            body_lines += ["", f"{len(ci['red'])} repository pipeline(s) on main are RED — a job "
                           f"ran and failed, so this IS a code defect:"]
            body_lines += [f"  {line}" for line in ci["red"]]
            body_lines += ["", "A local `make check` being green does not contradict this: the "
                           "workstation has the sibling checkout the pipeline could not obtain. "
                           "That is the whole reason this check exists."]
        if ci["unverified"]:
            body_lines += ["", f"{len(ci['unverified'])} repository pipeline(s) could not be "
                           f"judged — NOT a pass:"]
            body_lines += [f"  {line}" for line in ci["unverified"]]
        # The main sweep's record, in the wake-up body and not in a log. A RED row here is a
        # repository whose origin/main fails its own gate at a known commit, with the sweep's first
        # useful line of the failure, so the coordinator can dispatch a worker without reading
        # /tmp/opencode/main-sweep.cron.log — which is where these rows used to die.
        if ms_red:
            body_lines += ["", f"{len(ms_red)} repository(ies) on main are RED in the latest "
                           f"recorded main sweep — the sweep gates `origin/main` itself, so this "
                           f"IS a code defect:"]
            body_lines += [f"  {line}" for line in ms_red]
            body_lines += ["", "Fix on a branch; the sweep re-gates origin/main hourly and its "
                               "next recorded run will say whether it went green."]
        if ms_inconsistent:
            body_lines += ["", f"{len(ms_inconsistent)} main-sweep row(s) are TOOL-INCONSISTENT — "
                           f"the sweep contradicted itself, so fix the sweep before acting on "
                           f"its rows:"]
            body_lines += [f"  {line}" for line in ms_inconsistent]
        if ms_unverified:
            body_lines += ["", f"{len(ms_unverified)} main-sweep state(s) are unverified — "
                           f"NOT a pass:"]
            body_lines += [f"  {line}" for line in ms_unverified]
            body_lines += ["", "`main` is the ref every branch is created from, so 'not swept' is "
                               "not the same fact as 'swept and green'. Check the sweep (its cron "
                               "runs hourly at :17, dry-run, recording every completed run) before "
                               "concluding anything about main."]
        # The declared section, and the reason this report is not clear. Three parts, and each
        # is load-bearing.
        if declared_pending:
            body_lines += [
                "",
                f"CI DECLARED — {declared_pending} state(s) are DECLARED rather than verified, and a "
                f"declaration is NOT a pass:",
            ]
            if ci["declared"]:
                body_lines += ["",
                               f"{len(ci['declared'])} repository pipeline(s) match a declaration a "
                               f"coordinator wrote down — relocated out of the noise above, and "
                               f"reported rather than cleared:"]
                body_lines += [f"  {line}" for line in ci["declared"]]
            if ci["stale"]:
                body_lines += ["",
                               f"{len(ci['stale'])} declaration(s) no longer match what GitLab says, "
                               f"so the file is wrong. Remove or update them:"]
                body_lines += [f"  {line}" for line in ci["stale"]]
            if ci["unusable"]:
                body_lines += ["",
                               f"{len(ci['unusable'])} declaration(s) could not be checked at all — "
                               f"the file itself is the problem:"]
                body_lines += [f"  {line}" for line in ci["unusable"]]
            body_lines += [
                "",
                "Declaring can only make this report LOUDER, never quieter: a declaration is a "
                "coordinator's claim, nothing here can check it, and `main` was not verified in "
                "those repositories. It is why this run is NOT clear rather than why it is clear. "
                "A declaration that GitLab has outgrown is reported as stale, and one that turned "
                "into a real RED is a fault in its own right — a stale claim must never be the "
                "place a red goes to hide.",
            ]
        if ci["blocked"]:
            body_lines += ["", f"For context, {len(ci['blocked'])} more are BLOCKED on CI minutes "
                           f"rather than failing — reported so the red count above means "
                           f"something:"]
            body_lines += [f"  {line}" for line in ci["blocked"][:10]]
        body_lines += ["", "Full detail in .fleet-state/keepalive.log.", "",
                       PARALLEL_QUESTION]
        body = "\n".join(body_lines)
        if sig == previous and not args.force:
            log(f"{len(never_started)} never started, {len(ci['red'])} CI red, "
                f"{len(ci['unverified'])} CI unreadable, {declared_pending} declared, "
                f"{len(ms_red)} main-sweep red, {len(ms_unverified)} main-sweep unverified — "
                f"unchanged, staying quiet")
            persist(state, args.dry_run)
            return 0
        pane = find_coordinator_pane()
        if pane and not args.dry_run:
            subprocess.run(["herdr", "agent", "prompt", pane, body], capture_output=True, text=True)
            log(f"woke {pane}: {len(never_started)} never started, {len(ci['red'])} CI red, "
                f"{declared_pending} declared, {len(ms_red)} main-sweep red")
        else:
            log(body)
        persist(state, args.dry_run)
        return 0

    if ghosts:
        # A recorded worker the sweep never walked. Reported in its own section, never folded
        # into "all clear": the pane is either merged-and-unreclaimed or lost, and both need a
        # decision only the coordinator can make (reclaim the worktree, or prune the manifest).
        body_lines = [
            f"fleet keepalive: {len(ghosts)} recorded worker(s) did not appear in the sweep — "
            f"their worktree is gone, or was merged and cleaned up without the manifest being "
            f"pruned.",
            "",
        ]
        for g in ghosts:
            body_lines.append(f"  {g}")
        body_lines += [
            "",
            "A pane nobody is watching is not a pane that is fine. Check whether each branch is "
            "merged into main; if it is, run the reclaim sweep "
            "(`scripts/cleanup-workers.sh --yes`, coordinator only) and prune the manifest. "
            "This is also why the fleet can never be reported as all-clear while it is true.",
            "",
            PARALLEL_QUESTION,
        ]
        body = "\n".join(body_lines)
        if sig == previous and not args.force:
            log(f"{len(ghosts)} unaccounted pane(s), unchanged — staying quiet")
            return 0
        pane = find_coordinator_pane()
        if pane and not args.dry_run:
            subprocess.run(["herdr", "agent", "prompt", pane, body],
                           capture_output=True, text=True)
            log(f"woke {pane}: {len(ghosts)} unaccounted pane(s)")
        else:
            log(body)
        state["ghosts"] = ghosts
        persist(state, args.dry_run)
        return 0

    if sig == previous and not args.force:
        # "Nothing changed" and "nothing to say" are different facts, and a log line that
        # reads the same for both cannot tell the coordinator which one it is looking at.
        # So this line always carries the counts, and a pane that is *building towards* the
        # stall threshold is named even though it is not actionable yet — otherwise a worker
        # on its way to 8 checks is indistinguishable from a worker that is at 0.
        log(f"{len(ready)} ready, {len(broken)} failing, {len(stalling)} stalling, "
            f"{len(never_started)} never started, CI {len(ci['red'])} red / "
            f"{len(ci['unverified'])} unreadable, main sweep {len(ms_red)} red / "
            f"{len(ms_unverified)} unverified — unchanged since the last wake-up, "
            f"staying quiet")
        for r in below_threshold(panes, streaks, args.stalled):
            log(f"    not yet actionable: {r}")
        persist(state, args.dry_run)
        return 0

    pane = find_coordinator_pane()
    if not pane:
        log(f"{len(ready)} ready / {len(broken)} failing / {len(stalling)} stalling but no "
            f"coordinator pane found — cannot wake it")
        # The streaks still advance: the run was a real observation, and freezing them here
        # would stop a pane ever reaching the threshold because the coordinator's pane
        # happened to be missing on the checks before it.
        persist(state, args.dry_run)
        return 0

    parts = []
    if stalling:
        parts.append(
            f"{len(stalling)} worker(s) have STOPPED MOVING — unchanged across "
            f"{args.stalled}+ consecutive checks, which is a fact about duration rather than "
            f"readiness, and it is why these are here despite neither being mergeable nor "
            f"failing.\n\n"
            + "\n".join(f"  {r}" for r in stalling)
            + "\n\nThe keepalive has been nudging them on a cooldown and they have not moved, "
              "so read the pane before assuming the worker is alive. Since the never-started rule, a worker that "
              "never started is reported separately and on sight, so these are workers that "
              "have run a turn and stopped; a worker with a dirty tree is still "
              "indistinguishable from no work at all.")
    if ready:
        parts.append(
            f"{len(ready)} worker(s) finished and are ready to merge.\n\n"
            + "\n".join(f"  {r}" for r in ready)
            + "\n\nEach was clean-tree with the gate green at the last check.")
    if broken:
        parts.append(
            f"{len(broken)} worker(s) are FAILING their gate — a clean tree with a red gate, "
            f"which is the case a worker's own sign-off would not tell you about.\n\n"
            + "\n".join(f"  {r}" for r in broken)
            + "\n\nRead the first line of the error before assuming the worker knows. "
              "A GATE-RED on a pane that is still working is often mid-edit.")
    closing = ("Full detail in .fleet-state/keepalive.log. Run the gate yourself, merge with -F, "
               "confirm with `git merge-base --is-ancestor <branch> HEAD` against the ref you "
               "actually merged, then close. A merge frees a slot — dispatch in the same turn.")
    if ready or broken:
        body = ("fleet keepalive: " + "\n\n".join(parts) + "\n\n" + closing
                + "\n\n" + PARALLEL_QUESTION)
    else:
        # A stall-only wake-up must not end with merge instructions: there is nothing to
        # merge, and telling the coordinator to `git merge` a branch with zero commits is
        # advice that cannot be followed.
        body = ("fleet keepalive: " + "\n\n".join(parts)
                + "\n\nFull detail in .fleet-state/keepalive.log. Nothing here is ready to "
                  "merge — the question is whether these workers are alive at all."
                + "\n\n" + PARALLEL_QUESTION)

    if args.dry_run:
        log(f"would wake {pane}: {len(ready)} ready, {len(broken)} failing, "
            f"{len(stalling)} stalling, {len(never_started)} never started, "
            f"CI {len(ci['red'])} red / {len(ci['blocked'])} blocked / "
            f"{len(ci['unverified'])} unreadable, main sweep {len(ms_red)} red / "
            f"{len(ms_unverified)} unverified")
        for r in ready:
            log(f"    ready     {r}")
        for r in broken:
            log(f"    FAILING   {r}")
        for r in stalling:
            log(f"    STALLED   {r}")
        for r in never_started:
            log(f"    NEVER RAN {r}")
        for r in ms_red:
            log(f"    SWEEP RED {r}")
        if not ready and not broken and not stalling and not never_started:
            log("    (nothing)")
        return 0

    res = subprocess.run(["herdr", "agent", "prompt", pane, body],
                         capture_output=True, text=True)
    if res.returncode != 0:
        log(f"waking {pane} FAILED: {res.stdout.strip()[:200]} {res.stderr.strip()[:200]}")
    else:
        log(f"woke {pane}: {len(ready)} ready, {len(broken)} failing, {len(stalling)} stalling")
    persist(state, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())