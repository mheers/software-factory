#!/usr/bin/env python3
"""fleet-watch — the only completion signal the coordinator should trust.

WHY THIS EXISTS
---------------
`herdr agent wait <pane> --until done` does not fire when a worker finishes by going
`idle`, and in this project most workers finish that way: the agent signs off with its
session line and the TUI sits idle. Eight completions went unnotified during the first
day of coordination; every one was caught by accident, during a status sweep that was
running for some other reason.

So `--until done` misses most completions. Adding `--until idle` does not fix it,
because a pane is idle for a moment right after a prompt lands and an agent passes
*through* blocked while thinking, so both produce false positives that are
indistinguishable from a real delivery. And a watch that returns `timeout` is a third
thing again — it looks like a clean return unless the payload is read.

The root problem is that `agent_status` is not evidence. It has meant, at different
times: finished and green; finished with 353 lines uncommitted; finished with 11 files
dirty; finished with code that does not compile; and waiting for a go-ahead on two
findings. No single status distinguishes those.

EVIDENCE IS THE SIGNAL
----------------------
A pane is actionable when all three of these hold, and none of them is a status:

  1. it has commits ahead of its base branch,
  2. its working tree is clean,
  3. the gate is green, with the shared sources it reads already beside the worktree.

(3) is not one command. It is `gate_steps()`, and it is the same sequence for **every**
repository shape — a Go module gets `go vet` + `go test` with `GOPROXY=off`, a repository
with a Makefile gets `make check`, and then *either* shape has its `demo.md` verified.
The demo step is not Go-only and never was; the fix was filed against a dead wrapper that
made it look so, and what it actually took was a guard, because restoring the asymmetry
cost nothing to do. "Only the Go path checks the transcript" was a false belief, so it is
stated here as false rather than left to be re-derived.

That triple has no ambiguous reading. A worker that crashed has none of them. A worker
mid-edit has (2) or (1) missing. A worker that reported `done` with uncompilable code
fails (3). A worker that finished perfectly passes all three.

So this script ignores status for its verdict and reports the verdict instead. It
prints one line per pane whose *verdict changed* since the last check, so a stable pane
is silent and the coordinator is resumed exactly once per real event. That is what
`herdr agent wait` could not do.

USAGE
-----
    fleet-watch.py --once              # sweep now, print the table, exit 0
    fleet-watch.py --watch [--every N] # block until a pane becomes actionable, then exit
    fleet-watch.py --watch --also-stalled

Exit code is 0 either way; what matters is the printed lines. `--watch` exits as soon
as something is actionable, which is the callback. `--stalled` is for the case where a
worker has not moved for several checks: a stall is a real outcome and needs a decision
rather than more waiting.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# `first_useful_line` lives in a sibling module and is imported, not copied. It was in here
# AND in `main_sweep.py`, and the two drifted: this file capped each diagnostic while that one
# capped the joined string, so a long `t.Logf` dropped the real `t.Fatalf` from `main`'s report
# while a worker's branch was reported correctly. `fleet-keepalive.py` renders `GATE-RED` with
# this line as its `->` detail, so two copies meant two answers to "what failed".
#
# The directory is taken from `__file__` rather than from `os.getcwd()`, so the import resolves
# the same way this script is actually run: by absolute path from cron and from other
# repositories, by relative path from a checkout, and by `importlib` with `sys.path` untouched
# (`merge-gate.py` loads this file that way). The cost is that this script copied WITHOUT its
# sibling cannot run — which is the right way to fail, loudly, rather than silently reverting
# to a private copy. See `scripts/first_useful_line.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from first_useful_line import DIAG_WIDTH, first_useful_line  # noqa: E402
import factory_config  # noqa: E402

WORKSPACE = factory_config.workspace_root()
STATE_FILE = Path(os.environ.get("FLEET_WATCH_STATE", str(factory_config.path("watch_state"))))
# The pane a worktree runs in is written here at dispatch time. herdr's own
# `worktree list` is scoped to the *current* repository, so there is no fleet-wide
# query to ask — and an explicit manifest doubles as the record of what is live.
MANIFEST = Path(os.environ.get("FLEET_MANIFEST", str(factory_config.path("fleet_manifest"))))

# The width one row's detail column has carried since this script was written is `DIAG_WIDTH`,
# imported above from the shared parser. It bounds ONE diagnostic, and the point of importing it
# rather than redeclaring `160` here is that the two were the same number by coincidence before:
# `main_sweep.py` carried 200 in the same position, and that drift is what the shared parser removes.
# One number, named once, in the module that uses it.

# Every repository that consumes its siblings from the group does it through
# `scripts/fetch-shared-sources.sh`, and every copy of that script in the family opens with
# the same line:
#
#     SHARED="${CI_PROJECT_DIR:-$(pwd)}/../shared"
#
# So the shared sources are a SIBLING of the checkout, not a directory inside it. Since worker
# worktrees are all siblings under one workspace root, that is **one** shared directory for
# the whole fleet rather than one per worktree — which is what makes `provision_shared_sources`
# reuse rather than fetch, and it is stated there in full.
SHARED_DIRNAME = "shared"
FETCH_SCRIPT = Path("scripts") / "fetch-shared-sources.sh"
# The `for repo in a b c; do` line is the single statement of what a repository provisions,
# and it is what the family guards read (see `TestTheFetchScriptFetchesBothSharedSourcesFromTheGroup`
# in opponent-brief's `internal/guards`). Bound to that line rather than to the file:
# searching the whole file for a name finds it in the loop, in a build path and in a comment.
FETCH_LOOP = re.compile(r"^[ \t]*for[ \t]+repo[ \t]+in[ \t]+([^;\n]+?)[ \t]*;[ \t]*do[ \t]*$",
                        re.M)


def fetch_timeout() -> int:
    """Seconds a fetch may take before it is called a hang rather than a slow clone.

    Generous, because the script ends with `pnpm install --frozen-lockfile && pnpm build` in
    `shared/ui` and these scripts are written for a pipeline. Present because a cron
    job that never returns is a monitor nobody reads any more, and a named state is the
    difference between "this branch is unverified" and "the sweep is wedged".
    """
    try:
        return int(os.environ.get("FLEET_WATCH_FETCH_TIMEOUT", "900"))
    except ValueError:
        return 900


# pane -> (worktree path relative to the workspace, human label)
def git(wt: str, *args: str) -> str:
    return subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True).stdout.strip()


def manifest() -> dict:
    """{worktree path: pane id}, written at dispatch time."""
    try:
        return json.loads(MANIFEST.read_text())
    except Exception:
        return {}


def record_pane(worktree: str, pane: str) -> None:
    m = manifest()
    m[worktree] = pane
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=1))


def live_panes() -> list[tuple[str, str, str]]:
    """Every worktree in the workspace sitting on an issue branch.

    The *branch* is the discriminator, not the directory name: worktrees are created as
    <repo>-<iid> and only the branch says "this is a worker in flight".
    """
    out = []
    # Two levels, not one: the shared modules live under shared/, and a
    # worker in either would otherwise be invisible here — which is the same silent
    # failure this script exists to remove.
    candidates = list(WORKSPACE.iterdir()) + list((WORKSPACE / "shared").iterdir())
    for child in sorted(candidates):
        if not child.is_dir() or not (child / ".git").exists():
            continue
        branch = git(str(child), "rev-parse", "--abbrev-ref", "HEAD")
        if branch.startswith(factory_config.branch_prefix()):
            out.append((str(child), branch, child.name))
    return out


def agent_status(pane: str) -> str:
    # Absent tooling is the absence of an answer, never a crash — the rule `gitlab_ci.py` and
    # `fleet-watch.py`'s own readers state. CI has no `herdr`, and a monitor that dies with
    # `FileNotFoundError` reports nothing about everything; that is how this was found.
    try:
        r = subprocess.run(["herdr", "agent", "get", pane], capture_output=True, text=True)
    except OSError:
        return "?"
    try:
        return json.loads(r.stdout)["result"]["agent"]["agent_status"]
    except Exception:
        return "?"


# The cumulative token counter opencode renders on its status line, e.g. `144.6K (14%)`.
#
# **A rate is not a total.** During a turn the same line also carries `39.9s · 10.4 tok/s`, and a
# parser that accepted that would read a pane a second into its first turn as having done real
# work — which is precisely the false negative this function exists to remove. So the `tok/s`
# lines are excluded, and only the cumulative form counts.
TOKEN_COUNTER = re.compile(r"(\d+(?:\.\d+)?)\s*([KM]?)\s*\((\d+)%\)")
TOKEN_RATE = "tok/s"

# A pane that has demonstrably done nothing since dispatch, and one we cannot read.
NOT_STARTED = "NOT-STARTED"
STARTED = "STARTED"
PROGRESS_UNKNOWN = "UNKNOWN"


def pane_tokens_from(text: str) -> float | None:
    """The cumulative token count in a pane's status text, or None.

    Split out from `pane_tokens` so it can be tested against exact strings — the parsing is where
    the false negatives live (`tok/s` read as a total, a stale `0` masking a real figure), and a
    parser tested only by pointing it at live panes cannot be tested for the cases that matter,
    because no live pane happens to be showing them.
    """
    best = None
    for line in (text or "").splitlines():
        if TOKEN_RATE in line:
            continue
        for m in TOKEN_COUNTER.finditer(line):
            scale = {"": 1.0, "K": 1e3, "M": 1e6}.get(m.group(2).upper(), 1.0)
            value = float(m.group(1)) * scale
            # A stale `0` on a higher line must not mask a real total further down.
            best = value if best is None else max(best, value)
    return best


def pane_tokens(pane: str) -> float | None:
    """The pane's cumulative token count, or None when the counter is not on screen.

    Read from `herdr agent read --source detection` rather than from `herdr agent get`, because
    `agent get` carries **no token field at all** — `agent_status`, `state_change_seq` and
    `revision` are all it has. That is the whole reason the never-started rule exists: the fact that
    distinguishes "never ran a turn" from "finished and signed off" was never missing, it was
    being read from fields that cannot carry it.

    `--source detection` is Herdr's plain-text bottom-buffer snapshot, which is the same text the
    agent detector classifies. It is the status line, not a scrollback search, so the answer does
    not depend on how much the pane has scrolled.

    None is a real answer and not a failure to try: a pane whose status line is not being read
    has NOT been shown to have done nothing, and this function refuses to report the absence of
    evidence as evidence of absence.
    """
    try:
        r = subprocess.run(["herdr", "agent", "read", pane, "--source", "detection"],
                           capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return pane_tokens_from(r.stdout)


def agent_progress(pane: str, status: str | None = None) -> tuple[str, str]:
    """`(progress, why)` for one pane. The never-started fix, and the evidence behind it.

    THE DEFECT
    ----------
    A pane with `agent_status: idle`, a clean tree and no commits is what a **finished** worker
    looks like, and it is also what a worker whose **brief never registered** looks like. Nothing
    in that reading separates them, and `state_change_seq` does not help: it has lied in both
    directions across this project, and `idle` was reported for a pane mid-turn with a live
    spinner. Measured on 2026-10-03 — the keepalive said *"no worker is ready to merge and none is
    failing"* while `media-vendor`'s worker, whose only job was to fix a red pipeline, sat
    at `ahead=0 dirty=0` with a pane showing `3 MCP failed`. **It had never run a turn.**

    WHY TOKENS ARE THE FIELD THAT CARRIES IT
    ------------------------------------------
    A turn is a request to the model, so a worker that has never run one has consumed no tokens —
    and that is arithmetic rather than inference. `agent_status` cannot carry the fact because
    `idle` is the resting state of a session that has done everything it is going to do *and* of
    one that has not started; `state_change_seq` counts screen redraws, not work.

    So the counter is the evidence, and the guard below is what stops it becoming a new false
    negative. Zero tokens with no commits and no diff is not started; zero tokens *with* either is
    not this function's claim to make and is left to `ahead`/`dirty`. And a `working` pane is
    `STARTED` regardless of its count, because a turn in flight has demonstrably begun and reading
    it as "never started" would be the same confident wrong answer in the other direction.

    `PROGRESS_UNKNOWN` is the honest third state: an unreadable counter is not evidence that the
    worker did nothing, and `NOTHING`-by-default is what made the original bug invisible.
    """
    if status is None:
        status = agent_status(pane)
    tokens = pane_tokens(pane)
    if tokens is None:
        return PROGRESS_UNKNOWN, (f"the pane's token counter is not readable, so this cannot tell "
                                  f"a worker that never started from one that finished. Status "
                                  f"is {status!r}, which does not settle it")
    if tokens > 0:
        return STARTED, f"{tokens:,.0f} tokens consumed since dispatch"
    if status == "working":
        return STARTED, "0 tokens but the agent is mid-turn, so it has started"
    return NOT_STARTED, ("0 tokens consumed, 0 commits and 0 changed files since dispatch — this "
                         "pane has never run a turn, which is what an idle pane otherwise looks "
                         "like")


def is_go(wt: str) -> bool:
    return (Path(wt) / "go.mod").exists()


def shared_dir(wt: str) -> Path:
    """Where this repository's fetch script puts its sources — the script's own definition.

    `$(pwd)/../shared` from the script's point of view, which is this worktree's parent, so the
    fetch environment carries `CI_PROJECT_DIR` set to the worktree and the two agree by
    construction rather than by both happening to be right.
    """
    return Path(wt).resolve().parent / SHARED_DIRNAME


def fetch_list(script: Path) -> list[str] | None:
    """The names in the script's `for repo in ...; do` line, or `None` if it has no such line.

    Parsed from the script rather than hardcoded here, because the list differs across the
    family — `media` fetches three and `identity` four — and a hardcoded
    four would both invent a source `media` does not provision and miss the reason
    `meta` is in anyone's list at all (`web/vite.config.ts` reads its `apps.json` at
    BUILD time, so nothing else provisions it). The same reasoning, and the same line, as the
    family guards.
    """
    try:
        text = script.read_text()
    except OSError:
        return None
    found = FETCH_LOOP.search(text)
    return found.group(1).split() if found else None


def provision_shared_sources(wt: str) -> tuple[str, str]:
    """Reuse, or fetch once, the shared sources this worktree's go.mod and demo.md need.

    WHY THIS IS IN THE SWEEP AT ALL
    -------------------------------
    This sweep ran `showboat verify demo.md` and nothing else, so for every repository that
    fetches its siblings from the group it reported

        GATE-RED  clean tree, gate red
            -> demo.md does not verify: block 2:

    Block 2 is *a clean checkout builds one binary with the SPA inside it*, and the SPA build
    needs the fetched `ui`. So the verdict named a **transcript block** while the
    missing thing was a **prerequisite**. Verified both directions on 2026-10-05 from a bare
    clone of `opponent-brief`: no fetch -> block 2 fails; `./scripts/fetch-shared-sources.sh`
    -> `showboat verify demo.md` exits 0. On `identity` the same thing happened with
    `make check`, `ci lint` and `ci web-build` all green while only `showboat verify` was red.

    That is worse than a wrong row, because the documented response to `GATE-RED` is *go and
    gate it*: it sent a coordinator to reject a green branch, twice. The rule the three cases
    share is worth stating on its own, because it is the generalisation and not this bug —
    **a verdict that names a symptom rather than a prerequisite is the tell.** Same shape as
    the `withRepo`/`node_modules` order trap, and same shape as `required flag(s) "meta" not
    set`, which named a missing flag when the real problem was a stale binding.

    THE DECISION: reuse what is there, rather than fetch once per sweep
    ------------------------------------------------------------------
    The ticket allowed either. This reuses, and the reason is a measurement rather than a
    preference. Every copy of the script computes its target as `<checkout>/../shared`, and
    every worker worktree is a sibling under the one workspace root — so **there is one shared
    directory for the whole fleet, not one per worktree.** A per-sweep fetch would therefore
    mean four or five clones per pane per sweep into the same place, and the clone is the cheap
    half: the script ends with `pnpm install --frozen-lockfile && pnpm build` inside
    `shared/ui`, and `pnpm install` in a directory that already has a modules
    directory does not fail, it **waits** for a Proceed? that a non-terminal cannot answer
    (measured in `media`: 19 seconds green without `web/node_modules`, still running
    after ten minutes with it).

    So the shared checkout **is** the cache, and it is a better one than a dict in this
    process: it survives a restart, it is shared by every sweep, and it is what a developer
    already has. When everything the script provisions is beside the worktree the fetch is not
    run at all — which is also why a workstation sweep needs no `SHARED_SOURCES_TOKEN`,
    because nothing is cloned. When something is genuinely missing the fetch runs once, and
    what it leaves behind is the cache, so every later sweep of that worktree reuses it.

    THE ORDER IS PART OF THE CLAIM
    ------------------------------
    `gate()` calls this before anything else, and that is not tidiness. `make check` creates
    `web/node_modules`; a fetch *after* that runs `pnpm install` in a directory that already
    has one, which is the hang above. So a sweep that provisions after gating reports a
    failure that is not one — the defect in the other direction. The call-order assertion in
    `scripts/test_fleet_watch_shared_sources.py` holds the order rather than trusting it.

    FIVE STATES, AND NONE OF THEM IS A PASS BY OMISSION
    ---------------------------------------------------
    - `NOT-NEEDED` — no fetch script: nothing here is provisioned from the group.
    - `REUSED` — every source already present; nothing was run.
    - `FETCHED` — the script ran, and every source is present afterwards.
    - `REFUSED` — the script exists, sources are missing, and the fetch did not provide them.
      The detail carries the script's **own** refusal — `SHARED_SOURCES_TOKEN is not set`,
      a clone error — because that names the missing prerequisite. It is never a transcript
      block, and never a `RED`.
    - `UNPARSEABLE` — the script has no `for repo in ...; do` line, so this cannot tell what
      the repository provisions, and does not guess. Guessing would mean fetching on every
      sweep or assuming the worst; both are worse than saying it cannot see. This is the same
      reasoning as the family guard that refuses to pass against a script fetching nothing.

    The trade, stated rather than hidden: a branch that is genuinely broken *and* whose shared
    sources are missing reports `NO-GATE` rather than `RED`, because the sweep cannot
    distinguish the two without the prerequisite. It is woken either way, and after the fetch
    it reports the real failure.
    """
    script = Path(wt) / FETCH_SCRIPT
    if not script.exists():
        return "NOT-NEEDED", ("no scripts/fetch-shared-sources.sh, so this repository "
                              "provisions nothing from the group")
    wanted = fetch_list(script)
    if wanted is None:
        return "UNPARSEABLE", (
            "scripts/fetch-shared-sources.sh has no `for repo in ...; do` list, so this sweep "
            "cannot tell what it provisions and will not guess whether they are present. The "
            "branch is UNVERIFIED: run the fetch and the gate by hand, and fix the script's "
            "list so the next sweep can see it")
    shared = shared_dir(wt)
    missing = [name for name in wanted if not (shared / name).exists()]
    if not missing:
        return "REUSED", (f"reused the {len(wanted)} shared source(s) already at {shared} "
                          f"({', '.join(wanted)}) — scripts/fetch-shared-sources.sh was NOT run")

    # `CI_PROJECT_DIR` so the script and this check agree on where "shared" is, `CI` because
    # these scripts are written for a pipeline and pnpm does not ask a CI job to confirm
    # anything, and `GOPROXY=off` because a green fetch that reached the network is not evidence
    # that the group clone needs no module proxy.
    env = {**os.environ, "CI_PROJECT_DIR": str(Path(wt).resolve()),
           "CI": os.environ.get("CI", "true"), "GOPROXY": "off"}
    try:
        run = subprocess.run(["bash", str(script)], cwd=wt, capture_output=True, text=True,
                             env=env, timeout=fetch_timeout())
        out, code = run.stdout + run.stderr, run.returncode
    except subprocess.TimeoutExpired:
        return "REFUSED", (f"shared sources are missing ({', '.join(missing)}) and "
                           f"scripts/fetch-shared-sources.sh did not finish within "
                           f"{fetch_timeout()}s, which is the `pnpm install` modules-prompt hang "
                           f"rather than a slow clone. Fetch by hand, then re-sweep")
    still = [name for name in missing if not (shared / name).exists()]
    if code != 0 or still:
        because = first_useful_line(out) or f"exit {code}"
        absent = (f" and {', '.join(still)} is still absent afterwards"
                  if code == 0 and still else "")
        return "REFUSED", (f"shared sources are missing ({', '.join(missing)}) and "
                           f"scripts/fetch-shared-sources.sh could not provide them{absent}: "
                           f"{because}. So neither `go vet` nor `showboat verify demo.md` could "
                           f"be run, and this branch is UNVERIFIED — it is not red, and it is "
                           f"not verified. Fix the fetch and re-sweep")
    return "FETCHED", (f"ran scripts/fetch-shared-sources.sh and provisioned "
                       f"{', '.join(wanted)} at {shared}")


def gate(wt: str) -> tuple[str, str]:
    """The gate, and only the gate, folded onto the sweep's three states.

    The steps and their order live in `gate_steps()`; this is the projection the sweep prints, so
    the sweep and the coordinator's merge gate (`scripts/merge-gate.py`) run one sequence between
    them rather than two that can disagree about what a gate is.

    **Three outcomes, not two.** "I ran nothing" is not a pass, and a `bool` cannot say so.
    This used to return `True` with the reason *"no go module and no Makefile — nothing to
    run"*, and `assess` turned that into `MERGEABLE` with the reason *"clean tree, gate
    green"*. So the meta repository's own ticket — whose entire deliverable is a shell script, and
    therefore whose entire verification is that script running — was reported as verified
    by a monitor that had verified nothing.

    A monitor that says green because it did nothing is worse than one that says nothing,
    because it is a check you stop reading. `NONE` exists so that state is a name rather
    than a lie, and it never becomes `MERGEABLE`. A gate that *could not* run for want of a
    prerequisite is `NONE` for the same reason: it is a name, and reporting it as `RED` names
    a symptom — a transcript block, or a `replacement directory does not exist` — instead of
    the missing fetch.

    **`NOT-VERIFIED` is deliberately *not* `NONE` here.** A `NO-DEMO` repository is genuinely
    unverified and the sweep says so in the detail; a repository whose `demo.md` exists but
    could not be checked is a different fact, and folding it into `NONE` would report a missing
    prerequisite where there is none. It stays `GREEN` with the reason stating plainly that
    nothing was verified, which is the sweep's documented tolerance — and it is exactly why the
    merge gate folds the same verdict the other way. See `demo_gate`.
    """
    steps = gate_steps(wt)
    states = [step_state for _, step_state, _ in steps]
    # `STALE` folds onto `RED` and `NOT-VERIFIED` does not. That is the one place the four demo
    # states become the sweep's three, and it is deliberately different from the merge gate's fold
    # only in `NOT-VERIFIED` — see `demo_gate` and `scripts/merge-gate.py`.
    if any(s in ("RED", "STALE") for s in states):
        state = "RED"
    elif any(s in ("NONE", "REFUSED", "UNPARSEABLE") for s in states):
        state = "NONE"
    else:
        state = "GREEN"
    # Every step is named, so a reader can see that `showboat verify demo.md` ran without
    # re-running anything — the omission is what made the original defect invisible.
    #
    # On `RED` the failing step is put **first**, and the rest follow. The provisioning note was
    # always appended rather than prepended for that reason, and `fleet-keepalive.py` renders this
    # string as the `->` detail a coordinator reads to decide what went wrong: a row whose first
    # line is where the shared sources came from sends them after a clone that already happened
    # instead of after the test that failed.
    ordered = ([s for s in steps if s[1] in ("RED", "STALE")] +
               [s for s in steps if s[1] not in ("RED", "STALE")]
               if state == "RED" else steps)
    why = "; ".join(f"{name}: {step_state} — {detail}" for name, step_state, detail in ordered)
    return state, why


def go_own_files(wt: str) -> list[str]:
    """This repository's own tracked `.go` files — `vendor/` excluded, and why.

    `gofmt` has no exclude flag and `vendor/` is committed third-party code, so the exclusions each
    repository would need differ by how it vendors. Asking the tree is the wrong move and hardcoding
    one repository's answer is worse — it is the port migration's *"a guard keyed on a name rather than
    a shape"* one level up.

    **The exclusion is precautionary, not currently load-bearing:** measured across all nine
    repositories, zero files are unformatted including under `vendor/`. It stands because a re-vendor
    can reintroduce that at any time, and because a gate that goes red on a third-party dependency is a
    gate people learn to ignore.

    `git ls-files` rather than a filesystem walk for the reason `check_build_stamping.py` already
    records: **a file that is on disk but ignored is invisible to `git status` and passes every check
    that reads the working tree.** Gofmt-ing a gitignored file would report on state a fresh clone does
    not have.
    """
    out = subprocess.run(["git", "-C", wt, "ls-files", "--", "*.go"],
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l.strip() and not l.startswith("vendor/")]


def code_gate(wt: str) -> tuple[str, str]:
    """The repository's own gate — `go vet` + `go test`, or `make check` — and NO demo step.

    Split out of the old `run_the_gate` wrapper (removed since) so that the demo step has one
    implementation, shared with the coordinator's merge gate (`scripts/merge-gate.py`). Two
    copies of "what this repository's gate is" is the pair of implementations that disagree,
    which is the failure this repository's own notes name twice; so the shapes live here and
    `gate_steps()` is what sequences them.

    `GOPROXY=off` is not optional: a green run that reached the network is a run that will
    fail in CI, and a gate that only fails when the network does is not a gate.
    """
    env = {**os.environ, "GOPROXY": "off"}
    # Decide what kind of gate this repository has BEFORE running anything. Running
    # `go vet` first and only then noticing the repo is TypeScript reports a failure
    # that is not one - which is how a green worker comes back GATE-RED.
    if not is_go(wt):
        mk = Path(wt) / "Makefile"
        if mk.exists():
            c = subprocess.run(["make", "check"], cwd=wt, capture_output=True, text=True)
            out = (c.stdout or c.stderr).strip()
            if c.returncode == 0:
                return "GREEN", (out.splitlines()[-1][:DIAG_WIDTH] if out else "make check green")
            return "RED", first_useful_line(out, skip=("make:",)) or "make check failed"
        return "NONE", ("no go module and no Makefile, so there is no gate to run — this "
                        "branch is UNVERIFIED. Run its deliverable by hand, and say in the "
                        "issue that the sweep could not check it")
    # `gofmt` over THIS repository's own files, before vet.
    #
    # Why here and not `make check`: the gate's job is a fast uniform subset, and a non-Go
    # repository already gets its whole documented gate for free because `code_gate` shells out to
    # `make check`. Every Go repository instead gets **two hand-typed commands** — and that subset
    # silently dropped every step the repository documented but the coordinator did not type.
    # `contracts` writes `check: fmt vet test typecheck web-lint api-check check-english
    # check-demo`, and `fmt` is its FIRST step, so a branch with two unformatted new files came back
    # `MERGEABLE — clean tree, gate green`.
    #
    # `gofmt` has no exclude flag and `vendor/` is committed third-party code that is not this
    # repository's own, so the file list is the repository's TRACKED `.go` files minus `vendor/` —
    # the property "this repository's own files are gofmt-clean" rather than a spelling of it.
    #
    # **The exclusion is precautionary, and I measured that rather than assuming it.** `media`'s
    # AGENTS.md records that 17 of its 653 vendored files are not gofmt-clean; measured today, **all nine
    # repositories are clean including vendor** — zero unformatted files anywhere. So citing that number
    # would be exactly the family's own defect: *a recorded fact that stopped being a fact.* The rule
    # stands because a re-vendor can reintroduce it at any time and a gate that is red for a third-party
    # dependency is a gate people learn to ignore — but it is not currently load-bearing, and this comment
    # says so rather than implying otherwise.
    fmt = subprocess.run(
        ["gofmt", "-l"] + go_own_files(wt), cwd=wt, capture_output=True, text=True
    )
    if fmt.returncode != 0:
        return "RED", (first_useful_line(fmt.stderr or fmt.stdout, skip=("#",))
                       or "gofmt could not run over this repository's own files")
    unformatted = [l.strip() for l in fmt.stdout.splitlines() if l.strip()]
    if unformatted:
        return "RED", (f"gofmt: {len(unformatted)} of this repository's own file(s) are not "
                       f"gofmt-clean — {', '.join(unformatted[:3])}"
                       + (f" (+{len(unformatted) - 3} more)" if len(unformatted) > 3 else "")
                       + ". A check nine repositories run in their own make check, and this gate "
                         "did not.")
    v = subprocess.run(["go", "vet", "./..."], cwd=wt, capture_output=True, text=True, env=env)
    if v.returncode != 0:
        return "RED", first_useful_line(v.stderr or v.stdout, skip=("#",)) or "vet failed"
    t = subprocess.run(["go", "test", "./...", "-count=1"], cwd=wt, capture_output=True, text=True, env=env)
    if t.returncode != 0:
        return "RED", first_useful_line(t.stdout + t.stderr, skip=("FAIL", "ok  ")) or "test failed"
    return "GREEN", "gofmt + go vet + go test -count=1 passed (GOPROXY=off)"


# There was a `run_the_gate(wt)` here until the demo fix landed, and it was this:

#     state, why = code_gate(wt)
#     if state != "GREEN" or not is_go(wt):
#         return state, why
#     return demo_gate(wt)
#
# The `or not is_go(wt)` is the whole of the demo fix, and its docstring claimed the asymmetry was
# "deliberate and it is a filed bug". Both halves of that sentence were wrong in the same way: the
# asymmetry was in `code_gate` for a **Makefile** repository, not in `code_gate` for a Go one, and
# by the time it was read it was in a function **nothing called**.
#
# It was dead because `d853fd5` split the gate into `code_gate` + `gate_steps()` and left
# this wrapper behind. `gate()` calls `gate_steps()` and never came here, and `gate_steps()`
# reaches `demo_verdict` for **every** repository shape — so the defect the wrapper still carried
# was not in the sweep at all. The ticket was filed at 12:20 on 2026-10-05; that commit landed at
# 16:56 the same day, and fixed it as a side effect of making the merge gate share the sequence.
#
# **So it was removed rather than repaired, and that is the whole of this change.** Repairing it
# would have edited dead code and left the live path — the one the fix is about — as unguarded as it
# was. Two things follow, and both are held in `scripts/test_fleet_watch.py` rather than here:
#
#   1. `gate_steps()` reaches the demo step for a Makefile repository. Driven through the real
#      `gate()`/`assess()` with a stub `make` and a stub `showboat`, not by reading the code that
#      claims it — a Python copy of the rule passes while the sweep stays broken.
#   2. The asymmetry is not reintroducible silently. Restoring `or not is_go(wt)` into
#      `gate_steps` makes a stale `demo.md` on a Makefile repository read `MERGEABLE` again, and
#      that is asserted with the three preconditions this repository applies to every mutation:
#      the edit applied, the module compiles, and the failure is the stated one.
#
# What this comment is for is the second half. A dead function whose docstring describes itself as
# "the sweep's" behaviour is worse than no function: a reader greps for the gate, finds this, and
# concludes the sweep skips `demo.md` for every non-Go repository — the exact belief the ticket was
# filed to correct, now held in the source as a comment. **A comment that names a function which
# does not run is a defect that documents itself.** The removal is the fix; this paragraph is so
# the next reader does not reintroduce it, and so the greppable name is still here to be found.


# `demo.md` has four states, not two or three, and the fourth is the one this function was
# written for. A repository's `go vet` and `go test` can both be green while its recorded demo</path>
# transcript describes a commit that no longer exists. On 2026-10-03 that happened here:
# merging an agent-hub ticket added five packages, `demo.md`'s recorded gate transcript had
# never seen them, and `showboat verify` went from exit 0 to exit 1 — with the merge gate green
# throughout, because **the merge gate never asked**.
#
# - `VERIFIED`      — demo.md present, it records at least one exec entry, and `showboat verify`
#                     exited 0. `showboat verify` prints **nothing** on success, so this is an
#                     exit status and never an absence of output, and the count is reported so
#                     the report says what was checked rather than only that it was.
# - `STALE`         — exit non-zero, carrying the named block diff, because that is what makes
#                     the report actionable instead of merely alarming.
# - `NO-DEMO`       — no demo.md at all. Named rather than folded into a pass: "I verified
#                     nothing" and "I verified everything" must not print the same word.
# - `NOT-VERIFIED`  — the check could not be made: `showboat` is not on PATH, or `demo.md`
#                     records **zero** exec entries, in which case `showboat verify` exits 0
#                     having compared nothing at all. Measured on showboat 0.4.0: a document of
#                     prose with no code blocks verifies 0 silently, and a `bash` block with no
#                     following `output` block is executed but never compared. Both read as a
#                     green from the exit status alone, which is why the recorded entries are
#                     counted before the verdict is formed.
def DEMO_VERIFIED() -> str:
    return "VERIFIED"


def recorded_exec_entries(extract_stdout: str) -> int:
    """How many exec entries a `showboat extract` listing records.

    Counted from showboat's own parse rather than from a second regex over the markdown, for the
    same reason the gate reads `showboat verify`'s exit status rather than its output: one tool
    that knows the document format, and one place where a disagreement could hide.

    Anchored to the start of the line, and that anchor is load-bearing. `showboat extract` also
    emits `showboat note <file> '<commentary>'`, and a `demo.md` that quotes a command inside its
    prose puts `showboat exec …` inside that quoted string. Measured across this family on
    2026-10-05: `grep -c 'showboat exec'` counted **14** exec entries in
    `contracts/demo.md` where `showboat extract` records **13**, because one line of
    commentary mentions the command. An unanchored count would over-report the evidence by one
    and look like a demonstration with a block nobody recorded.
    """
    return sum(1 for line in (extract_stdout or "").splitlines()
               if line.startswith("showboat exec "))


def demo_verdict(wt: str) -> tuple[str, str]:
    """`showboat verify demo.md` as a four-valued verdict, for the sweep and the merge gate.

    **NEVER WRAPPED IN `timeout`.** The recorded blocks in these transcripts start servers and
    bind ports — `APP=29087; OPA=29187` in `agent-hub/demo.md` — so a timeout that
    fires mid-run leaves a port bound and a half-written demo, and the next run then fails on
    the port rather than on the code. There is a recorded instance of that wedging for eight
    hours. `scripts/test_merge_gate.py` reads this file's syntax tree and fails if a `timeout`
    ever appears on the call that runs showboat, so the rule cannot rot into a habit.

    **Never wrapped in `timeout` either is not "never run it".** A missing or empty demo is a
    reported state, not a skipped step and not a failure.
    """
    demo = Path(wt) / "demo.md"
    if not demo.exists():
        return "NO-DEMO", ("no demo.md in this repository, so none was verified. This is reported "
                           "rather than passed over silently: a green that verified nothing and a "
                           "green that verified everything must not print the same word")
    if shutil.which("showboat") is None:
        return "NOT-VERIFIED", ("`showboat` is not on PATH, so demo.md was NOT verified. Install "
                                "it and run `showboat verify demo.md` yourself; a green that "
                                "verified nothing is not a green")
    # `verify` first, and its **exit status** is the verdict. It prints nothing at all on
    # success, so an output-shaped check would read the green case as an absence of evidence and
    # the red case only by parsing a diff. Reached before `extract` deliberately: a stale
    # transcript is the common failure, and it must be reported even by a `showboat` whose
    # `extract` subcommand cannot be relied on.
    s = subprocess.run(["showboat", "verify", "demo.md"], cwd=wt, capture_output=True, text=True)
    # `extract` parses the document and prints the commands that would rebuild it, so it is what
    # the file *records* as opposed to what it currently produces. It executes nothing.
    x = subprocess.run(["showboat", "extract", "demo.md"], cwd=wt, capture_output=True, text=True)
    if s.returncode != 0:
        # The count is a nicety on the failure path: a stale transcript is worth reporting even
        # when this cannot say how many entries it holds.
        counted = ""
        if x.returncode == 0:
            entries = recorded_exec_entries(x.stdout)
            counted = f" over {entries} recorded exec entr{'y' if entries == 1 else 'ies'}"
        return "STALE", (f"demo.md does not verify — `showboat verify demo.md` exit "
                         f"{s.returncode}{counted}: " +
                         (first_useful_line(s.stdout + s.stderr) or "showboat verify printed nothing"))
    if x.returncode != 0:
        # Green from `verify`, but this cannot tell what the document records, so it cannot say
        # the green means anything. `NOT-VERIFIED`, not `VERIFIED`.
        return "NOT-VERIFIED", ("`showboat verify demo.md` exited 0, but `showboat extract "
                                "demo.md` failed, so this cannot tell what the document records: " +
                                (first_useful_line(x.stdout + x.stderr) or
                                 f"exit {x.returncode}"))
    entries = recorded_exec_entries(x.stdout)
    if entries == 0:
        # The case an exit status alone cannot see. `showboat verify` exits 0 in silence on a
        # document of prose with no code blocks, having compared nothing at all, so the green is
        # real and means nothing. Measured on showboat 0.4.0, 2026-10-05.
        return "NOT-VERIFIED", ("demo.md records **0 exec entries**, so `showboat verify demo.md` "
                                "exited 0 having compared nothing at all — that is not a verified "
                                "transcript. Record the workflow with `showboat exec` and re-run")
    return "VERIFIED", (f"`showboat verify demo.md` exit 0 over {entries} recorded exec entr"
                        f"{'y' if entries == 1 else 'ies'}")


def demo_gate(wt: str) -> tuple[str, str]:
    """`demo_verdict` folded onto the sweep's two gate states — the sweep's contract, unchanged.

    `demo_verdict` is the four-valued form; this is the projection of it onto `GREEN`/`RED`. It is
    the sweep's tolerance expressed as a function, and `gate()` applies the same fold over
    `gate_steps()` — so it is what the *tests* reach for when they want the sweep's policy without
    a repository, and the two must agree (asserted in `scripts/test_fleet_watch.py`). The tolerance
    is a policy of its own: a sweep that reports `NO-GATE` on every pane because `showboat` is not
    installed is a sweep nobody reads, whereas the merge gate (`scripts/merge-gate.py`) folds the
    same verdict the other way and refuses to call a merge verified when it could not verify it.
    Two folds over one verdict, deliberately, and the divergence is asserted in
    `scripts/test_merge_gate.py` so neither can drift unnoticed.
    """
    state, why = demo_verdict(wt)
    if state == "STALE":
        return "RED", why
    if state == "VERIFIED":
        return "GREEN", f"gate green, and {why}"
    return "GREEN", f"gate green, but {why}"


def gate_steps(wt: str) -> list[tuple[str, str, str]]:
    """Every gate step, in the order it runs, as `(step, state, detail)`.

    One sequence, shared by the sweep and the coordinator's merge gate. The order is the claim and
    it is not tidiness: the shared sources come first because `go vet` and `showboat verify` both
    read `../shared/`, and a `make check` creates `web/node_modules`, so provisioning after
    gating is a `pnpm install` in a directory that already has one — which waits for a `Proceed?`
    no non-terminal can answer rather than failing.

    A step that cannot run **stops the sequence**, and that is what makes the reported states
    honest rather than merely tidy: a `REFUSED` fetch means no gate command's output can be
    believed, and a `RED` code gate means the demo transcript is not the thing to look at next.
    """
    shared, provisioning = provision_shared_sources(wt)
    steps = [("shared sources", shared, provisioning)]
    if shared in ("REFUSED", "UNPARSEABLE"):
        return steps
    state, why = code_gate(wt)
    steps.append(("repository gate", state, why))
    if state != "GREEN":
        return steps
    demo_state, demo_why = demo_verdict(wt)
    steps.append(("demo.md", demo_state, demo_why))
    return steps


def assess(wt: str, branch: str, progress: str | None = None,
           progress_why: str = "") -> dict:
    """The row's verdict, from git, the gate, and — when given — whether the agent ever started.

    `progress` is a parameter rather than something read here on purpose. `sweep()` reads it once
    per pane and passes it in, so the row carries `progress` even when the verdict is `NOT-STARTED`
    and no gate ran at all; and a caller that wants the git-and-gate verdict alone can omit it,
    which is every existing test. It is `None` — not `NOT-STARTED` — when absent, so "nobody said"
    and "nobody started" cannot be confused by a reader of the code.
    """
    base = "main"
    ahead = git(wt, "rev-list", "--count", f"{base}..HEAD") or "0"
    st = subprocess.run(["git", "-C", wt, "status", "--porcelain"], capture_output=True, text=True).stdout
    dirty = len([l for l in st.splitlines() if l.strip()])
    behind = git(wt, "rev-list", "--count", f"HEAD..origin/{base}") or "0"
    rec = {"worktree": wt, "branch": branch, "ahead": int(ahead), "dirty": dirty, "behind": int(behind)}
    if int(ahead) == 0:
        # "No commits ahead" is the same reading for a worker mid-edit and for a worker that
        # never ran a turn, and conflating them is the defect: the keepalive's stall rule keys on
        # `NOTHING`, so the *least* alarming state in the fleet was also the slowest to be
        # reported. When the pane says it has consumed no tokens and there is no diff either,
        # there is nothing to wait for — this is `NOT-STARTED`, and it is a name rather than a
        # synonym for `NOTHING`.
        #
        # Guarded by `dirty == 0` as well: a dirty tree is evidence of work whatever the token
        # counter says, and a pane whose counter read 0 because it is not being rendered is not
        # evidence of anything.
        if progress == NOT_STARTED and dirty == 0:
            rec["verdict"] = NOT_STARTED
            rec["why"] = progress_why or "the pane has never run a turn"
        else:
            rec["verdict"] = "NOTHING"
            rec["why"] = "no commits ahead"
    elif dirty:
        rec["verdict"] = "IN-FLIGHT"
        rec["why"] = f"{dirty} uncommitted file(s)"
    else:
        state, detail = gate(wt)
        rec["gate"] = state
        rec["verdict"] = {"GREEN": "MERGEABLE", "RED": "GATE-RED", "NONE": "NO-GATE"}[state]
        rec["why"] = {"GREEN": "clean tree, gate green",
                      "RED": "clean tree, gate red",
                      "NONE": "clean tree, NO GATE COULD RUN — unverified"}[state]
        rec["detail"] = detail

    # Self-consistency. The verdict must be exactly what the recorded numbers imply,
    # recomputed here by a second, independent expression of the same rule. This is not
    # paranoia about the reader: it has already happened once that a correct verdict was
    # misread as an inconsistent one, and the cost of a tool that cannot vouch for its own
    # output is that every row becomes something to double-check by hand. A disagreement
    # here means this file is wrong, so say so instead of printing a verdict.
    n, d = rec["ahead"], rec["dirty"]
    # `NOTHING` and `NOT-STARTED` are both zero-commit rows, so the recomputation has to
    # re-derive which one the recorded evidence implies — otherwise the check below would flag
    # every `NOT-STARTED` row `TOOL-INCONSISTENT`, and a guard that fires on correct rows is a
    # guard that trains the reader to ignore it. Deliberately re-derived from the *inputs*
    # (`progress`, `ahead`, `dirty`) rather than compared with `rec["verdict"]`: a check that
    # compares a verdict with itself cannot catch it, which is the mistake already documented two
    # lines below for the `MERGEABLE` case.
    if n == 0:
        expected = NOT_STARTED if (progress == NOT_STARTED and d == 0) else "NOTHING"
    else:
        expected = "IN-FLIGHT" if d else rec["verdict"]
    # …and a verdict about the gate must match the gate that actually ran. `MERGEABLE`
    # claims a green gate; if no gate ran, that claim is false whatever the numbers say.
    if n > 0 and d == 0 and rec["verdict"] == "MERGEABLE":
        # Independent of what `gate()` said, because a check that asks the thing it is
        # checking cannot catch it. The first version of this compared the verdict with
        # `rec["gate"]`, which is `gate()`'s own answer — so a `gate()` that lied about
        # running a green gate agreed with itself and the row printed "clean tree, gate
        # green" anyway. The mutation test in `scripts/test_fleet_watch.py` exists because
        # that is exactly the failure it walks into.
        #
        # So re-derive it from the filesystem: a gate is runnable only if there is a Go
        # module to vet and test, or a Makefile with something to run.
        has_gate = is_go(wt) or (Path(wt) / "Makefile").exists()
        if not has_gate:
            rec["verdict"] = "TOOL-INCONSISTENT"
            rec["why"] = ("assess() said MERGEABLE but this worktree has neither a go.mod "
                          "nor a Makefile, so no gate could have run - this script is "
                          "buggy, do not trust this row")
    if expected != rec["verdict"]:
        claimed = rec["verdict"]          # capture before overwriting, or the message lies
        rec["verdict"] = "TOOL-INCONSISTENT"
        rec["why"] = (f"assess() said {claimed} but ahead={n} dirty={d} implies "
                      f"{expected} - this script is buggy, do not trust this row")
    return rec


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(s: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=1))


def sweep(verbose: bool = True) -> list[dict]:
    results = []
    known = manifest()
    for wt, branch, label in live_panes():
        # Only worktrees the coordinator recorded at dispatch time are live. A
        # worktree left behind by a merged ticket is not a worker, and reporting it
        # as NOT NOTHING would train the reader to ignore the table.
        if wt not in known:
            continue
        pane = known.get(wt)
        # Read the progress evidence BEFORE the row, and pass it in, so `assess()` can set
        # `NOT-STARTED` as a verdict rather than as an annotation nobody reads. A row whose only
        # claim lives in a field the table does not print is a field nobody reads.
        if pane:
            status = agent_status(pane)
            progress, why_progress = agent_progress(pane, status)
        else:
            # No pane means no counter can be read, and "no pane" is not "no work". `UNKNOWN` here
            # is the difference between a worker we cannot see and a worker that never started —
            # collapsing them is how a manifest entry with a dead pane reads as health.
            status = "?"
            progress, why_progress = PROGRESS_UNKNOWN, (
                "no pane is recorded for this worktree, so there is no token counter to read and "
                "nothing can be said about whether this worker ever started")
        rec = assess(wt, branch, progress=progress, progress_why=why_progress)
        rec["pane"] = pane
        rec["status"] = status
        rec["progress"] = progress
        rec["progress_why"] = why_progress
        results.append(rec)
    if verbose:
        # The reason is printed ON the row, not inferred by cross-reading columns. A
        # wide table invites reading a number out of the wrong column and then building
        # a conclusion on it; a self-explaining row does not.
        print(f"  {'pane':<9}{'status':<9}{'started':<14}{'ahead':<6}{'dirty':<6}{'behind':<7}verdict")
        print("  " + "-" * 78)
        for r in sorted(results, key=lambda x: x["verdict"]):
            # `started` is printed because a row that says `NOTHING` for a pane which has never
            # run a turn is the exact false negative the never-started rule exists to remove — and an invisible field is how
            # it stayed invisible. `UNKNOWN` is spelled out rather than blanked, so "we cannot see"
            # never renders as "there is nothing to report".
            line = (f"  {str(r['pane'] or '?'):<9}{r['status']:<9}"
                    f"{r.get('progress', PROGRESS_UNKNOWN):<14}"
                    f"{r['ahead']:<6}{r['dirty']:<6}"
                    f"{r['behind']:<7}{r['verdict']:<15} {r.get('why','')}")
            if r["verdict"] in ("GATE-RED", "NO-GATE", NOT_STARTED) and r.get("progress_why"):
                line += f"\n      -> {r['progress_why']}"
            elif r["verdict"] in ("GATE-RED", "NO-GATE") and r.get("detail"):
                line += f"\n      -> {r['detail']}"
            print(line)
        bad = [r for r in results if r["verdict"] == "TOOL-INCONSISTENT"]
        if bad:
            print()
            print(f"  !! {len(bad)} row(s) are internally inconsistent — fleet-watch.py is buggy.")
            print("     Do not act on those rows; fix the script first.")
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="sweep, print the table, exit")
    ap.add_argument("--json", action="store_true",
                    help="sweep, print one JSON object, exit. For scripts: never parse the table")
    ap.add_argument("--watch", action="store_true", help="block until a pane becomes actionable")
    ap.add_argument("--every", type=int, default=90, help="seconds between checks")
    ap.add_argument("--stalled", type=int, default=0,
                    help="also exit when a pane has not changed for this many checks")
    args = ap.parse_args()

    if args.json:
        # One JSON object on stdout, nothing else on stdout. Anything that consumes this
        # must use --json rather than scraping the table: a wide table invites reading a
        # number out of the wrong column, which has already happened once and cost a
        # confident claim about a bug that did not exist.
        print(json.dumps({"panes": sweep(verbose=False)}, indent=1, sort_keys=True))
        return 0

    if args.once or not args.watch:
        sweep()
        return 0

    state = load_state()
    checks = 0
    while True:
        checks += 1
        results = sweep(verbose=False)
        news = []
        for r in results:
            key = r["worktree"]
            # `progress` is IN the signature, not just the verdict. A pane that goes from `UNKNOWN` to
            # `NOT-STARTED` keeps `ahead=0 dirty=0`, so a signature without it would see no change
            # and stay silent through the exact transition the watch exists to notice.
            sig = (f"{r['verdict']}|{r['ahead']}|{r['dirty']}|{r['behind']}"
                   f"|{r.get('progress', PROGRESS_UNKNOWN)}")
            if state.get(key) != sig:
                news.append(r)
                state[key] = sig
        save_state(state)
        # `NOT-STARTED` is actionable on sight rather than through the stall counter, and the
        # distinction is the point. A stalled worker has been working; a worker that never ran a
        # turn has not, and waiting eight more checks for it is waiting for something that will
        # not happen on its own — its brief never registered, so nothing is going to change until
        # a human re-prompts it.
        actionable = [r for r in news
                      if r["verdict"] in ("MERGEABLE", "GATE-RED", "NO-GATE", NOT_STARTED)]
        if actionable:
            print(f"{checks} check(s). Something is actionable — run the gate on it:")
            for r in actionable:
                detail = r.get("progress_why") if r["verdict"] == NOT_STARTED else r.get("detail", "")
                print(f"  {r['pane']}  {r['branch']}  {r['verdict']}  {detail}")
        if args.stalled and checks % args.stalled == 0:
            stale = [r for r in results if r["verdict"] in ("NOTHING", "IN-FLIGHT")]
            if stale:
                print(f"{checks} check(s) with no change. Stalled or mid-flight:")
                for r in stale:
                    print(f"  {r['pane']}  {r['branch']}  {r['status']}  ahead={r['ahead']} dirty={r['dirty']}")
                return 0
        time.sleep(args.every)


if __name__ == "__main__":
    # `fleet-watch.py --record <pane> <worktree>` is called at dispatch time.
    if len(sys.argv) == 4 and sys.argv[1] == "--record":
        record_pane(sys.argv[3], sys.argv[2])
        print(f"  recorded {sys.argv[2]} -> {sys.argv[3]}")
        sys.exit(0)
    sys.exit(main())