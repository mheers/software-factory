#!/usr/bin/env python3
"""merge-gate — the merge gate, as a program rather than a sequence somebody types.

    python3 scripts/merge-gate.py <worktree> [--branch <name>]
    python3 scripts/merge-gate.py --self          # this repository, at HEAD

Exit status is the evidence, and it is 0 only when every step ran and passed:

    0   the gate is green, and `showboat verify demo.md` was green over what it records
    1   at least one step failed, and the first line of its reason is on stdout
    2   at least one step could not be run — the branch is UNVERIFIED, not red
    3   this gate is broken (a missing dependency, an unparseable checkout)

## Why this exists

`AGENTS.md` documented the merge gate as four numbered steps and the coordinator typed them. On
2026-10-05 that cost five manual `showboat verify demo.md` invocations across four repositories,
and twice the transcript was the **only** thing red while `make check`, `ci lint` and
`ci web-build` were all green. A step that only exists in a document is a step that will be
skipped — and this one is skipped exactly when it is expensive, because it is the slowest of the
four and the only one that re-executes the product's own workflow.

So the gate is a program. `demo.md` is its third step, alongside `go vet`/`go test`, and it runs
**before** the merge rather than after.

## What it will not do

- **It never merges, never closes an issue, never pushes.** Those need judgement this does not
  have: a merge message written for its reasoning, and `git merge-base --is-ancestor` against
  the ref that was actually merged. This program's whole output is the gate.
- **It never re-records a transcript block.** The transcript rule's second obligation — a merge that changes a
  repository with a `demo.md` re-records the affected blocks in the same merge — is a discipline
  for whoever merges. A tool that guessed which blocks a merge invalidates would re-record the
  wrong ones *confidently*, which is worse than not offering. This program says so when a
  transcript is stale, and leaves the recording to a person.
- **It never wraps `showboat verify` in `timeout`.** The recorded blocks start servers and bind
  ports (`APP=29087; OPA=29187` in `agent-hub/demo.md`), so a timeout that fires
  mid-run leaves a port bound and the next run fails on the port rather than on the code. There
  is a recorded instance of that wedging for eight hours.

## The states, and why there are four

Reused from `fleet-watch.py`'s `demo_verdict` rather than re-invented — two vocabularies for one
question is the pair of implementations that disagree, which this repository's own notes name
twice. So the demo step is `VERIFIED`, `STALE`, `NO-DEMO` or `NOT-VERIFIED`, and this program
folds them **the other way round from the sweep**:

| demo state     | sweep (`fleet-watch`) | merge gate (here) |
|----------------|-----------------------|-------------------|
| `VERIFIED`     | GREEN                 | 0                 |
| `NO-DEMO`      | GREEN, says so        | 0, says so        |
| `STALE`        | RED                   | 1                 |
| `NOT-VERIFIED` | GREEN, says so        | **2**             |

That divergence is deliberate and it is the point of the ticket. The sweep's job is to tell the
coordinator where to look; a sweep that reported every pane as unverified because `showboat` was
not installed would be a sweep nobody reads. This program's job is to authorise one merge, and a
merge authorised on a transcript it could not check is the exact defect the transcript rule exists for. Two folds
over one verdict, and `scripts/test_merge_gate.py` asserts the divergence so neither can drift
without a test going red.

`NOT-VERIFIED` includes the case the exit status alone cannot see: a `demo.md` that records
**zero** exec entries. `showboat verify` exits 0 on it, in silence, having compared nothing.
Measured on showboat 0.4.0 — see `recorded_exec_entries` in `fleet-watch.py`.

## What it reuses

`gate_steps()` in `fleet-watch.py`, so the sequence — shared sources first, then the repository's
own gate, then `demo.md` — exists once. The ordering is load-bearing rather than tidy:
`go vet` and `showboat verify` both read `../shared/`, and `make check` creates
`web/node_modules`, so provisioning *after* gating is a `pnpm install` in a directory that already
has one, which waits for a `Proceed?` no non-terminal can answer. That trap cost two false
`GATE-RED` rows and is why this program does not have its own copy of the sequence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_sweep():
    """`fleet-watch.py`, loaded by path — by `importlib`, because the filename carries a hyphen.

    The same awkwardness `check_family_registry.py` records for `check-shared-ui-tarballs.py`:
    Python cannot import a name a person types as a command, so the reuse is real and it is
    awkward, and both facts are recorded rather than one of them. It raises rather than falling
    back to a private copy of the sequence, because a missing dependency for a gate is a broken
    gate — and a second copy of "what the merge gate is" is exactly how the sweep and the merge
    gate would come to disagree about whether `demo.md` is in it.
    """
    path = HERE / "fleet-watch.py"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing, and this gate runs its sequence from there rather than keeping "
            f"a second copy that could disagree about whether demo.md is part of the merge gate")
    spec = importlib.util.spec_from_file_location("fleet_watch", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} could not be loaded, so the merge gate cannot be assembled")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fw = _load_sweep()

# `RED`, `NONE` and `GREEN` are the sweep's own state names and are reused rather than renamed, so
# a row read in `fleet-keepalive.log` and a line read here carry the same words for the same facts.
# The demo step's two extra states are `demo_verdict`'s, also reused — and `STALE` in particular
# is what the whole ticket is about, so it must not arrive here under a name of its own.
#
# FAILED is the one name this file adds, and it exists so the demo step's failure folds onto the
# *same* verdict the code gate's failure does. Without it, `STALE` is neither `RED` (so it reads as
# unverified) nor `NONE` (so it reads as unverified *and* not a failure) — and a transcript that
# does not verify would exit 0. That is the defect, so the mapping is explicit and asserted.
FAILED = ("RED", "STALE")
UNVERIFIABLE = ("NONE", "REFUSED", "UNPARSEABLE", "NOT-VERIFIED")


def verify(wt: str) -> tuple[int, list[dict]]:
    """Run the merge gate on one worktree. Returns `(exit status, one record per step)`.

    Every step gets a record even when it is not reached, so the report can say what did not run
    rather than omitting it. An absent line reads as a pass; a line that says `not-run` does not.
    """
    steps = fw.gate_steps(wt)
    records: list[dict] = []
    reached = {name for name, _, _ in steps}
    for name, state, detail in steps:
        records.append({"step": name, "state": state, "detail": detail})
    for name in ("shared sources", "repository gate", "demo.md"):
        if name not in reached:
            records.append({"step": name, "state": "not-run",
                            "detail": f"{name} was not reached, because an earlier step did not "
                                      f"finish — nothing here is evidence about it"})

    states = [r["state"] for r in records if r["state"] != "not-run"]
    if any(s in FAILED for s in states):
        return 1, records
    if any(s in UNVERIFIABLE for s in states):
        return 2, records
    # Every step ran. `NO-DEMO` is the one outcome that is green without having verified a
    # transcript, and it is printed rather than folded away — see the table in the module
    # docstring for why it is not an error.
    return 0, records


def demo_verdict_sentence(demo: dict | None) -> str:
    """What the green verdict line may claim, derived from the demo step's recorded state.

    One function rather than a branch inside `report`, so the claim is written down once and can be
    tested on its own — and because a verdict sentence that is assembled in two places is a place
    where one of them goes stale. `None` (the demo step is missing from `records` altogether, which
    would be a bug rather than an outcome) is handled rather than crashed on: a gate that raises
    while formatting its verdict has verified nothing at all.

    The rule it encodes: **say verified only when `VERIFIED`.** Every other green outcome says what
    was green and what was not verified, so no green can claim a check that did not run.
    """
    if demo is None:
        return ("the gate is green and no demo step was recorded, so no transcript was verified. "
                "This should not happen; the gate is reporting on an incomplete run")
    if demo["state"] == "VERIFIED":
        # Says "demo.md verified" **and** the count. The phrase is the one this program's green
        # verdict is allowed to use, so it is stated here explicitly rather than left to arrive by
        # accident inside a detail string — a claim asserted in prose and produced by luck is a
        # claim that disappears on the next edit.
        return (f"gate green and **demo.md verified** — {demo['detail']}. Nothing about this merge "
                f"invalidated the recorded blocks, so there is nothing to re-record")
    if demo["state"] == "NO-DEMO":
        return ("gate green, and this repository has no demo.md — so **no transcript was verified "
                "here**, which is not the same claim as a verified one. There is nothing to "
                "re-record for this merge")
    if demo["state"] == "not-run":
        return ("gate green, and the demo step did not run, so no transcript was verified here. "
                "This should not happen on a green gate; treat the green with suspicion")
    # Any other green state — currently none, since `STALE` is a failure and `NOT-VERIFIED` is
    # unverifiable. Named generically rather than asserted away, so a state added tomorrow cannot
    # fall through into "demo.md verified".
    return (f"gate green, and the demo step reported {demo['state']} — {demo['detail']}. Not a "
            f"verified transcript, and this verdict does not claim one")


def report(records: list[dict], branch: str | None, wt: str) -> str:
    """The whole gate as text, one line per step, plus the verdict.

    A verdict on its own line with the steps above it, never instead of them: the earlier defect
    this program exists to remove is a merge called green while the transcript was the only thing
    red, so the transcript's line has to be *in* the report a reader skims.
    """
    lines = [f"merge gate — {wt}" + (f" ({branch})" if branch else "")]
    width = max((len(r["step"]) for r in records), default=8)
    for r in records:
        lines.append(f"  {r['step']:<{width}}  {r['state']:<14} {r['detail']}")
    states = [r["state"] for r in records if r["state"] != "not-run"]
    demo = next((r for r in records if r["step"] == "demo.md"), None)
    if any(s in FAILED for s in states):
        lines.append("  VERDICT: RED — do not merge. The first failing step is above.")
    elif any(s in UNVERIFIABLE for s in states):
        unverified = [r for r in records if r["state"] in UNVERIFIABLE]
        lines.append("  VERDICT: UNVERIFIED — this is not a pass and not a failure. "
                     + "; ".join(f"{r['step']}: {r['detail']}" for r in unverified))
    else:
        # **Derived from the recorded state, not from the fact that nothing failed.**
        #
        # The first version of this line was a constant on the `else` branch — "gate green and
        # demo.md verified, over what it records" — and it printed on a repository with no
        # `demo.md`. The step row said `NO-DEMO` one line above and the verdict contradicted it,
        # which is this program's own thesis applied to itself: a green that verified nothing and
        # a green that verified everything ended in the same sentence.
        #
        # It is worth being precise about why the step row did not save it. A *missing* signal can
        # be noticed; an actively false one cannot, and this was the second kind. The last line is
        # the one a tired coordinator reads, and it told them a transcript had been verified when
        # none existed. The exit status was right throughout — a repository that genuinely has no
        # `demo.md` is not a failure, and folding `NO-DEMO` into `UNVERIFIED` would make this
        # repository's own merge permanently exit 2, which is the noise the earlier fix removed.
        #
        # Every green outcome is therefore named from what the demo step actually reported, so the
        # verdict cannot claim more than the rows above it. `NOT-VERIFIED` cannot reach this branch
        # — it is in `UNVERIFIABLE` — but it is handled anyway rather than assumed away, because
        # "it cannot happen" is exactly the kind of claim that stops being true when someone adds a
        # state, and the cost of it having gone stale would be this same false sentence.
        lines.append(f"  VERDICT: GREEN — {demo_verdict_sentence(demo)}")
    lines.append("")
    lines.append("  Re-record any transcript block this merge invalidates, in this same merge: a "
                 "transcript is a snapshot of a run, and a merge that moves the thing it "
                 "snapshots has to move it too. No tool here decides which blocks those are.")
    return "\n".join(lines)


def current_branch(wt: str) -> str | None:
    r = subprocess.run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True)
    name = r.stdout.strip()
    return name or None if r.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("worktree", nargs="?", help="the checkout to gate; defaults to --self")
    ap.add_argument("--self", action="store_true",
                    help="gate this repository at HEAD (its own scripts, no demo.md here)")
    ap.add_argument("--branch", help="label the report with this branch name")
    ap.add_argument("--json", action="store_true", help="one JSON object on stdout, for scripts")
    args = ap.parse_args()

    if args.self:
        wt = str(HERE.parent)
    elif args.worktree:
        wt = str(Path(args.worktree).resolve())
    else:
        ap.error("name a worktree to gate, or pass --self")

    if not Path(wt).is_dir():
        print(f"merge-gate: {wt} is not a directory, so there is nothing to gate", file=sys.stderr)
        return 3

    branch = args.branch or current_branch(wt)
    status, records = verify(wt)

    if args.json:
        print(json.dumps({"worktree": wt, "branch": branch, "exit": status,
                          "steps": records}, indent=1, sort_keys=True))
        return status

    print(report(records, branch, wt))
    # The exit status is printed as well as returned. `showboat verify` exits 0 in silence on
    # success, so "I ran it and saw nothing" is not evidence that anything passed — and a person
    # reading a transcript of this run needs the number, not the absence of complaints.
    print(f"  exit status: {status}")
    return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, ImportError) as exc:
        # A gate whose own dependency is missing must not report anything about the branch it was
        # asked about. Exit 3 is this program's "I am broken", distinct from 2's "I could not
        # tell about the branch".
        print(f"merge-gate: {exc}", file=sys.stderr)
        sys.exit(3)
