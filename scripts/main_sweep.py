#!/usr/bin/env python3
"""main-sweep.py — gate every repository's `origin/main`, and file an issue when one is red.

Run:

    python3 scripts/main_sweep.py --dry-run     # report only, file no issues (still recorded)
    python3 scripts/main_sweep.py               # file an issue per red main
    python3 scripts/main_sweep.py --repo identity    # one repository (not recorded)

## Why this exists

`identity`'s `main` was red from 09:00 UTC on 2026-10-03 until the coordinator
merged into it that afternoon. The cause was a fixture that hardcoded

    DelegationExpiresAt: time.Date(2026, 10, 3, 9, 0, 0, 0, time.UTC)

and compared it against `time.Now()`. Correct when written, expired on a clock nobody watched.

Nothing noticed, and the reason is structural rather than accidental. **Every other check in
this family looks at a worker's branch.** `fleet-watch.py` gates branches that have commits
ahead; `cleanup-workers.sh` reclaims branches already merged. Both are blind to `main`
itself — which is the ref every future branch is created from, and the one a
customer-facing prototype is demoed from. A red `main` is the worst possible failure for this
project specifically, because the product is a golden prototype shown to a customer, and a
fixture that expires at 09:00 is not a cosmetic defect.

It surfaced only by luck: the coordinator merged a branch and read the output. Filed in the
repository it happened in and in the meta.

## The three things this will not do

**1. Report green because it ran nothing.** `gate()` returns GREEN / RED / **NONE**. A
repository with no runnable gate returns `NONE`, reported as `NO-GATE` and never as a pass.
This is the defect `fleet-watch.py` shipped for a day: its `gate()` returned a `bool`, so a
repository with no `go.mod` and no `Makefile` came back `True` with the reason *"nothing to
run"*, and `assess()` turned that into `MERGEABLE ... gate green`. `meta` — this
repository — has no `go.mod`; its real gate is `scripts/check-agents-md.sh`. A sweep that
"verified" meta's `main` by running nothing would be the same lie, repeated.

**2. File an issue every thirty minutes.** `dedupe_key()` is derived from the repository and
the **failing commit**, and an existing open issue carrying that key suppresses a new one. A
new red commit produces a new key, so a fix that regresses is still reported.

**3. Trust itself.** `assess()` re-derives the verdict from what is on disk and prints
`TOOL-INCONSISTENT` when the two disagree, because a gate that claims GREEN where no gate
exists is exactly the failure mode above. If you see `TOOL-INCONSISTENT`, fix the script and
do not act on the row.

## The record, and why this is not only a log

Every **complete** run is recorded in `.fleet-state/main-sweep.json`, including a `--dry-run` —
which is what cron runs. `fleet-keepalive.py` and `notify-coordinator.py` read that record through
`main_sweep_state.py`, so a `RED` row arrives in the owner-facing report with its repository,
commit and first useful line instead of dying in `/tmp/opencode/main-sweep.cron.log`.

Measured on 2026-10-06: `social-studio`'s main was red from 10:00 UTC, the sweep gated
it seven times in a row, and every row sat in a log no person opened until a worker tripped over
the failure three hours later. A monitor whose findings are only ever written to a file nobody
reads has not monitored anything.

A `RED` row also keeps the **full output of the failing step** beside the record
(`main-sweep-red-<repo>-<commit>.log`, named by the row's `log` field, the red log). The summary is enough
to see the failure and not enough to prove it: `identity`'s panic was sighted three
times without yielding its message, and portal's `errno` was unprovable from the record. Both
modes write the log — cron runs the sweep dry — and a green run writes nothing.

A partial run (`--repo`) is deliberately **not** recorded: a record holding only the repositories
a partial run read is indistinguishable from a complete run in which every other repository was
green, which is a silencer — and a silencer is worse than the noise it removes, because it is
trusted.

## Destructive by design, in one narrow way

Running a gate on `origin/main` needs the code, so the sweep uses `git worktree add` at the
**remote** ref rather than the local `main`. It never checks out the local `main`, never
resets it and never writes to a repository. The temporary worktree is removed afterwards
unless `--keep-worktrees` says otherwise. A red `main` is reported; fixing it is a worker's
job on a branch.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# `first_useful_line` lives in a sibling module and is imported, not copied. It was in here
# AND in `fleet-watch.py`, and the two had drifted: this copy capped the **joined** report
# rather than each diagnostic, so a `t.Logf` longer than the cap pushed the real `t.Fatalf`
# out of the report entirely — the original defect through a different route, live in the
# script that gates every repository's `main`. `fleet-keepalive.py` renders `GATE-RED` with
# this line as its `->` detail, so two copies meant two answers to "what failed".
#
# `width=200` and `empty=` are this caller's own presentation, not a second implementation:
# its line is a standalone report rather than a table cell. Which line IS the failure is
# decided in the shared module, once.
#
# The directory comes from `__file__`, not from the caller's cwd, so this resolves the same
# way cron runs it (`cd <repo> && python3 scripts/main_sweep.py`), the same way a person runs
# it from anywhere, and the same way `test_main_sweep.py` loads it with `sys.path` untouched.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import first_useful_line as _ful  # noqa: E402
# The record this run leaves behind, and the reader both report scripts use. Imported by
# path for the same reason: cron runs this file by absolute path with no `PYTHONPATH`.
import main_sweep_state  # noqa: E402
import factory_config  # noqa: E402
import tracker  # noqa: E402

# This sweep's own presentation, bound once. `width=200` because its detail is a standalone
# report rather than a table cell, and it must not be NARROWER than the other caller's — a
# truncated report is how the real `t.Fatalf` went missing in the first place.
first_useful_line = functools.partial(_ful.first_useful_line, width=200,
                                      empty="gate failed with no output")

# The layout comes from `factory_config` — one seam: the environment, then factory.json, then
# the shipped template defaults. See `scripts/factory_config.py`.
WORKSPACE = factory_config.workspace_root()
META = factory_config.meta_root()  # the repository this script lives in
APPS = factory_config.registry_path()


# Gate command per repository shape, tried in order. The first that exists is the gate, and
# the sweep says which one it used — a report that does not name its own command cannot be
# checked by the reader.
GO_GATE = "go-gate"


def git(args: list[str], cwd: str | Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()




def has_go_gate(wt: str) -> bool:
    return (Path(wt) / "go.mod").exists()


def has_make_gate(wt: str) -> bool:
    return (Path(wt) / "Makefile").exists()


def gate(wt: str) -> tuple[str, str, str]:
    """Run the repository's real gate. Three outcomes, never two.

    `NONE` is the outcome that matters. "I ran nothing" is not a pass, and a two-valued gate
    cannot say so — which is precisely how this workspace once reported a repository as
    verified by a monitor that had verified nothing.

    `GOPROXY=off` is not optional: a green run that reached the network is a run that will
    fail in CI, and a gate that only fails when the network does is not a gate.

    The third element is the failing step's **full output**, which `write_run()` keeps beside the
    record for a RED row. It is empty for GREEN and NONE on purpose: a green run must not
    grow storage, and `NONE` ran nothing to keep. The summary in the second element remains the
    row's report; the output is what a reader opens when the summary is not enough.
    """
    env = {**os.environ, "GOPROXY": "off"}
    # Decide the gate's shape BEFORE running anything. Running `go vet` first and only then
    # noticing this is TypeScript reports a failure that is not one.
    if has_go_gate(wt):
        v = subprocess.run(["go", "vet", "./..."], cwd=wt, capture_output=True, text=True, env=env)
        if v.returncode != 0:
            out = v.stderr or v.stdout
            return "RED", first_useful_line(out, skip=("#",)) or "go vet failed", out
        t = subprocess.run(["go", "test", "./...", "-count=1"], cwd=wt,
                           capture_output=True, text=True, env=env)
        if t.returncode != 0:
            out = t.stdout + t.stderr
            return "RED", first_useful_line(out, skip=("FAIL", "ok  ")) or "go test failed", out
        return "GREEN", "go vet + go test -count=1 passed (GOPROXY=off)", ""

    if has_make_gate(wt):
        c = subprocess.run(["make", "check"], cwd=wt, capture_output=True, text=True, env=env)
        out = (c.stdout or c.stderr).strip()
        if c.returncode == 0:
            last = out.splitlines()[-1][:160] if out else ""
            return "GREEN", f"make check passed (GOPROXY=off){': ' + last if last else ''}", ""
        return "RED", first_useful_line(out, skip=("make:",)) or "make check failed", out

    # A prose repository has no gate. Saying so is the whole point of this branch.
    if (Path(wt) / "scripts" / "check-agents-md.sh").exists():
        c = subprocess.run(["bash", "scripts/check-agents-md.sh"], cwd=wt,
                           capture_output=True, text=True, env=env)
        out = c.stdout + c.stderr
        if c.returncode == 0:
            return "GREEN", "scripts/check-agents-md.sh passed", ""
        return "RED", first_useful_line(out) or "check-agents-md.sh failed", out

    return "NONE", ("no go module, no Makefile and no check-agents-md.sh, so there is no gate "
                    "to run against main. This repository is UNVERIFIED by this sweep — do not "
                    "read the absence of an issue as the absence of a problem"), ""


def dedupe_key(repo: str, commit: str) -> str:
    """Identity of one red main: the repository plus the commit that is red.

    Keyed on the commit, not the repository, because a fix that regresses is a new problem
    and must be able to speak. Keyed on the repository too, because two repositories red at
    the same commit are two problems, and one issue cannot carry both.
    """
    return f"{repo}@{commit[:12]}"


def should_file(key: str, open_issues: list[dict]) -> bool:
    """File only when no open issue already carries this key.

    Silence is the success case. A sweep that opens an issue per tick is a sweep whose output
    gets filtered, and the one real regression then goes unread along with the noise.
    """
    return not any(key in (i.get("title") or "") for i in open_issues)


def gate_exists_on_disk(wt: str) -> bool:
    """Whether a gate is present, re-derived from the filesystem.

    This is deliberately not `gate()`'s own return value. A gate that lies agrees with
    itself, so checking it against itself is circular — which is the mistake that made
    `fleet-watch.py`'s first self-check worthless until the mutation test caught it.
    """
    p = Path(wt)
    return (p / "go.mod").exists() or (p / "Makefile").exists() or (p / "scripts" / "check-agents-md.sh").exists()


def assess(wt: str, ref: str = "main", state: str | None = None) -> dict:
    """One repository's verdict, and a self-check that can contradict it."""
    if state is None:
        state, detail, output = gate(wt)
    else:
        _, detail, output = gate(wt)
    present = gate_exists_on_disk(wt)
    verdict = {"GREEN": "GREEN", "RED": "RED", "NONE": "NO-GATE"}[state]

    row = {"worktree": wt, "ref": ref, "gate": state, "verdict": verdict,
           "detail": detail, "gate_on_disk": present,
           # The failing step's full output, in memory and not in the record: `write_run()` moves
           # it to a file beside the record for a RED row and puts the path in the row, because a
           # record carrying the output would be the log the record points at.
           "output": output}

    # The self-check. A GREEN with no gate on disk is the fleet-watch lie, caught.
    if state == "GREEN" and not present:
        row["verdict"] = "TOOL-INCONSISTENT"
        row["why"] = ("the gate reported GREEN but there is no go.mod, Makefile or "
                      "check-agents-md.sh in this tree, so it verified nothing — this row is "
                      "not to be trusted, fix the script")
    elif state == "NONE":
        row["why"] = "no gate could run, so this main is UNVERIFIED — not green"
    else:
        row["why"] = {"GREEN": "main passes its own gate",
                      "RED": "main fails its own gate"}[state]
    return row


# ----------------------------------------------------------------- repositories


def repos_from_apps() -> list[tuple[str, Path]]:
    """Every repository named in apps.json, plus this one.

    apps.json is the workspace's registry, so the sweep's repository list cannot drift from
    it. `meta` is added explicitly because it is not an app — it is the repository holding
    apps.json.
    """
    out: list[tuple[str, Path]] = []
    try:
        data = json.loads(APPS.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"  warning: cannot read {APPS}: {exc}", file=sys.stderr)
        data = []
    apps = data if isinstance(data, list) else data.get("apps", [])
    for app in apps:
        repo = app.get("repo")
        if not repo:
            continue
        path = factory_config.workspace_root() / repo
        if path.is_dir():
            out.append((repo, path))
    if not any(r == META.name for r, _ in out):
        out.append((META.name, META))
    return out


def sweep_repo(repo: str, path: Path, keep: bool = False) -> dict:
    """Gate `origin/main` for one repository, in a throwaway worktree at the remote ref.

    The local `main` is never touched. Checking out the remote ref in a temporary worktree
    is what makes it safe to run this on a cron against thirteen repositories while workers
    are committing on branches beside them.
    """
    subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=str(path),
                   capture_output=True, text=True)
    commit = git(["rev-parse", "--short", "origin/main"], path)
    if not commit:
        return {"repo": repo, "verdict": "NO-REMOTE", "why": "no origin/main to gate",
                "gate": "NONE", "detail": "fetch or rev-parse failed", "commit": ""}

    # The worktree goes INSIDE the workspace, never in /tmp, and that is load-bearing.

    # Every app consumes shared code by a path relative to its own module:

    #     replace example.com/go => ../shared/go

    # so `../shared/go` resolves against the *module's* directory. A worktree in
    # /tmp has no `../shared/`, and every gate then fails with

    #     replacement directory ../shared/go does not exist

    # The first version of this sweep used `tempfile.mkdtemp()`, and it reported **nine of
    # ten repositories RED** — all nine with that same line, and all nine entirely caused by
    # the sweep's own harness. Nine filed issues would have been nine lies, and the least
    # defensible kind: a monitor whose first act is to blame nine healthy repositories.

    # So the worktree is a sibling of the repository, which is also why AGENTS.md insists
    # worker worktrees live inside the workspace.
    wt = WORKSPACE / f".mainsweep-{repo.replace('/', '-')}"
    try:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=str(path), capture_output=True, text=True)
        subprocess.run(["git", "worktree", "prune"], cwd=str(path), capture_output=True, text=True)
        r = subprocess.run(["git", "worktree", "add", "--detach", "-q", str(wt), "origin/main"],
                           cwd=str(path), capture_output=True, text=True)
        if r.returncode != 0:
            return {"repo": repo, "verdict": "NO-GATE", "gate": "NONE", "commit": commit,
                    "why": "could not materialise origin/main in a worktree",
                    "detail": first_useful_line(r.stderr or r.stdout)}

        # The harness check. If the shared-module replacement cannot be resolved, the gate is
        # about to fail for a reason that has nothing to do with the repository, and filing
        # that as "main is red" is precisely the lie this sweep exists to avoid.
        bad = harness_broken(str(wt))
        if bad:
            return {"repo": repo, "verdict": "NO-GATE", "gate": "NONE", "commit": commit,
                    "key": dedupe_key(repo, commit),
                    "why": "THE SWEEP'S OWN HARNESS IS BROKEN, so this repository was NOT tested",
                    "detail": bad}

        row = assess(str(wt), "origin/main")
        row["repo"] = repo
        row["commit"] = commit
        row["key"] = dedupe_key(repo, commit)
        row["why"] = f"origin/main at {commit} — {row['why']}"
        return row
    finally:
        if not keep:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                           cwd=str(path), capture_output=True, text=True)
            subprocess.run(["git", "worktree", "prune"], cwd=str(path), capture_output=True, text=True)


def harness_broken(wt: str) -> str | None:
    """Whether the sweep's own environment would make any gate here fail spuriously.

    Replaces resolve against the module's directory, so this is a fact about where the
    worktree was put. Reported as `NO-GATE` — *not tested* — rather than RED, because a
    repository that was never tested has not failed anything.
    """
    p = Path(wt)
    gomod = p / "go.mod"
    if not gomod.exists():
        return None
    for line in gomod.read_text().splitlines():
        line = line.strip()
        if not line.startswith("replace "):
            continue
        target = line.split("=>")[-1].strip()
        if not target.startswith(".") and not target.startswith("/"):
            continue  # a module-version replace, resolved by the proxy, not the filesystem
        resolved = (p / target).resolve()
        if not resolved.is_dir():
            return (f"the sweep put this worktree at {wt}, where the module's own "
                    f"`replace ... => {target}` cannot resolve — the worktree must be a "
                    f"sibling inside {WORKSPACE} so relative replaces work")
    return None


# ---------------------------------------------------------------------- tracker


def open_issues(repo: str) -> list[dict]:
    """Every issue of `repo`, through the tracker adapter — GitHub or GitLab, per configuration."""
    return tracker.open_issues(repo)


def file_issue(repo: str, row: dict) -> str | None:
    """File one issue for a red main. Returns the URL, or None if it could not be filed."""
    body = f"""`origin/main` of `{repo}` fails its own gate at commit `{row.get('commit', '?')}`.

    {row.get('detail', '')}

## Why this is an issue and not a log line

Every other check in this family looks at a **worker's branch**: `fleet-watch.py` gates
branches with commits ahead, `cleanup-workers.sh` reclaims merged branches. Both are blind to
`main` itself — the ref every future branch is created from, and the one a customer-facing
prototype is demoed from.

This happened for real: `identity`'s `main` was red from 09:00 UTC on 2026-10-03
until the coordinator merged into it, because a fixture hardcoded
`time.Date(2026, 10, 3, 9, 0, 0, 0, time.UTC)` and compared it against `time.Now()`. It
surfaced by luck. Filed in the repository it happened in and in the meta; this issue is that sweep's output.

## What is owed

1. Reproduce against `origin/main`, not a working tree that happens to be clean.
2. Fix on a branch with a test that fails first, and check whether the fixture's clock is the
   real defect — a time bomb that is correct on the day it is written is a defect that
   reaches production.
3. Add whatever guard stops the *class*: anything comparing against `time.Now()` with a
   hardcoded deadline will expire again, and a guard that only this one fixture satisfies
   does not stop the next one.

## Notes

- Deduplicated on `{row.get('key', '')}` — a sweep that files on every tick is a sweep
  nobody reads. A **new** failing commit produces a new key, so a regression after a fix is
  still reported.
- Raised by `scripts/main_sweep.py`.
"""
    url = tracker.file_issue(
        repo,
        title=f"main is red: {row.get('key', repo)} — gate fails on origin/main",
        body=body,
        labels=["type::bug", "sev::high", factory_config.label("todo")],
    )
    if url is None:
        print(f"  could not file in {repo}: the tracker did not accept the issue")
    return url


# ----------------------------------------------------------------------- state


# `load_state`/`save_state` lived here and wrote a per-repository verdict map that nothing read —
# one writer and no consumer. The record reader is the consumer: `main_sweep_state.write_run()` records the
# completed run in the same file, in a shape the report scripts read, and it is written by **both**
# modes. See the record section in the module docstring.


# ------------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, file no issues (the run is still recorded for the report chain)")
    ap.add_argument("--repo", action="append", default=[], help="limit to these repositories")
    ap.add_argument("--keep-worktrees", action="store_true", help="leave temp worktrees for inspection")
    args = ap.parse_args()

    targets = repos_from_apps()
    if args.repo:
        wanted = set(args.repo)
        targets = [(r, p) for r, p in targets if r in wanted]
        if not targets:
            print(f"no match for {sorted(wanted)} in apps.json", file=sys.stderr)
            return 1

    print(f"main-sweep — {'DRY RUN' if args.dry_run else 'APPLYING'} — {len(targets)} repositories")
    print()
    rows = []
    for repo, path in targets:
        row = sweep_repo(repo, path, keep=args.keep_worktrees)
        rows.append(row)
        flag = "!!" if row["verdict"] == "RED" else ("??" if row["verdict"] not in ("GREEN",) else "ok")
        print(f"  {flag} {repo:26} {row['verdict']:18} {row.get('commit', '')}")
        print(f"       {row.get('why', '')}")
        if row["verdict"] not in ("GREEN", "NO-REMOTE", "NO-GATE"):
            print(f"       detail: {row.get('detail', '')[:150]}")

    reds = [r for r in rows if r["verdict"] == "RED"]
    nongate = [r for r in rows if r["verdict"] == "NO-GATE"]
    inconsistent = [r for r in rows if r["verdict"] == "TOOL-INCONSISTENT"]

    print()
    print(f"summary: {len(rows) - len(reds) - len(nongate) - len(inconsistent)} green, "
          f"{len(reds)} RED, {len(nongate)} NO-GATE, {len(inconsistent)} TOOL-INCONSISTENT")

    if inconsistent:
        print()
        print("TOOL-INCONSISTENT means this script contradicted itself. Fix the script; "
              "do not act on those rows.")

    if nongate:
        print()
        print("NO-GATE is not a pass. Those repositories are unverified by this sweep:")
        for r in nongate:
            print(f"  {r['repo']}: {r.get('detail', '')[:120]}")

    filed: list[tuple[str, str | None]] = []

    if reds and not args.dry_run:
        print()
        print("filing:")
        for row in reds:
            existing = open_issues(row["repo"])
            if not should_file(row["key"], existing):
                print(f"  {row['repo']}: an open issue already carries {row['key']} — silent")
                continue
            url = file_issue(row["repo"], row)
            filed.append((row["repo"], url))
            print(f"  {row['repo']}: {'filed ' + url if url else 'FILED NOTHING (see above)'}")
    elif reds:
        print()
        print("dry run: would file one issue per red main, deduplicated on the failing commit")

    # The record. A `--dry-run` still writes it, and that is the whole mechanism: cron runs
    # this sweep dry, so a record written only in applying mode would leave the report chain —
    # `fleet-keepalive.py` and `notify-coordinator.py` — with nothing to read, and a RED back in
    # a log. "File nothing" is about issues, not about observations.
    #
    # A partial run is deliberately NOT recorded. `--repo X` writes nothing, because a record
    # holding only the repositories it read is indistinguishable from a complete run in which
    # every other repository was green — a silencer, and a silencer is worse than the noise it
    # removes because it is trusted.
    if args.repo:
        print()
        print(f"partial run (--repo), so {main_sweep_state.path()} is left alone — a record of "
              f"a partial sweep would read as a complete one whose unread repositories were green")
    else:
        try:
            where = main_sweep_state.write_run(rows, dry_run=args.dry_run)
            print()
            print(f"recorded in {where} — fleet-keepalive.py and notify-coordinator.py read it, "
                  f"so a RED here reaches the owner's report without anyone reading this log")
            # The record alone must make a red readable, and that means the full output and
            # its path are both named here — a file nobody knows about is the discarded output
            # again, one indirection later. Only printed for a red whose output was kept, so
            # this never names a file that was not written.
            for row in rows:
                if row.get("verdict") == "RED" and (row.get("output") or "").strip():
                    print(f"  full output: {main_sweep_state.red_log_path(row['repo'], row['commit'])}")
        except OSError as exc:  # the sweep's job is the gate; a record failure must not fail it
            print(f"could not record the run in {main_sweep_state.path()}: {exc}", file=sys.stderr)

    return 1 if (reds or inconsistent) else 0


if __name__ == "__main__":
    sys.exit(main())