#!/usr/bin/env python3
"""reap-port-holders.py — report, and with `--yes` kill, the test processes holding a TCP port.

    python3 scripts/reap-port-holders.py                 # dry-run, prints the table
    python3 scripts/reap-port-holders.py --yes           # reap what is eligible
    python3 scripts/reap-port-holders.py --older-than 600
    python3 scripts/reap-port-holders.py --json          # for the keepalive
    python3 scripts/reap-port-holders.py --explain

Dry-run by default. Coordinator only — a worker is running inside a directory this sweep may
signal, and a signal delivered to the wrong pid is not a report.

## WHY IT FINDS HOLDERS BY PROCESS, NOT BY SCANNING A RANGE

The obvious design is to scan the allocated blocks and ask who holds each port. **That finds
nothing here**, and it is worth saying why rather than shipping it. Measured on 2026-10-05 while
writing this, one leaked `go test` in `player-registry-issue-13`:

    127.0.0.1:29400   pid 4045586   /tmp/go-build…/api.test -test.run=TestTheGoldenPathGateway
    127.0.0.1:33397   pid 4045586   (the same process)
    127.0.0.1:35947   pid 4045586   (the same process)
    127.0.0.1:43335   pid 4045586   (the same process)

`29400` is the one it was told to take. The other three are **ephemeral-range ports no
allocation scheme covers**, bound by the same process on descriptors it opened itself. Every one
of the sixteen leaked ports observed that day was outside every allocated block, so a
range-scanning reaper reports a clean machine while four processes hold sixteen ports.

So the direction is inverted: ask the kernel which processes are listening, identify the ones
that are test processes, and report what *they* hold.

## `-timeout 0` IS WHY THEY EXIST AT ALL

    /tmp/go-build2137343990/b001/api.test -test.paniconexit0 -test.run=TestTheGoldenPathGateway
      -test.count=1 -test.timeout=0s

`-timeout 0` is **load-bearing** where the test binary *is* the server a golden-path harness then
drives with `curl` — `player-registry/scripts/verify-golden-path.sh` and
`scripts/player-360.sh` — because a finite Go test timeout kills the gateway mid-walkthrough.
It is also what makes a hang hold a port forever, and **a hung test is indistinguishable from a
slow machine** to everything downstream: `merge-gate.py` reads the consequence, not the cause.

The rule this implements, for `AGENTS.md`: **`-timeout 0` belongs on the golden-path scripts and
nowhere else.**

## FOUR VERDICTS, NOT TWO

"Nothing to do" and "would not" are different facts, and a boolean collapses them — which is how a
report ends up claiming it removed something it never considered.

| verdict | meaning |
|---|---|
| `REAP` | older than the bound, no established connections, branch merged or not a repository — and it may be signalled |
| `REFUSE` | a reason not to, printed beside the verdict |
| `SKIP` | a test process inside the age bound; it is a run in progress, not a leak |
| `NOT-A-TEST` | something is listening and it is not a Go test — reported, never signalled |

### The refusals, and what each guards against

- **an established connection** — a client is talking to it. `docs/FLEET-HANDOFF-2026-10-05.md`
  records killing these "after confirming zero established connections"; this is that check.
- **the branch is not merged** — borrowed from `cleanup-workers.sh`, which refuses to drop an
  unmerged branch for the same reason. A leaked gateway on a worktree whose branch is unmerged is
  a **judgement about somebody's work**, not a fact this sweep can read. It is reported and
  refused.
- **this process, or one of its ancestors** — a reaper that signals its own shell is not a
  reaper. `docs/FLEET-HANDOFF-2026-10-05.md` trap 5 records a session SIGKILLing itself with a
  `pkill` pattern that matched its own command line; **every signal here is an explicit pid read
  out of the kernel's socket table**, and this script never greps a process list for a name.

## WHAT IT REPORTS AND NEVER DELETES

No worktree, no branch, no session. It signals a pid and reports what it did. A transcript, a
commit and a closed issue are not its business.

## Exit codes

    0  nothing eligible, or `--yes` reaped what was eligible
    1  something is eligible but this was a dry run — re-run with `--yes`
    2  cannot tell — `ss` is absent, or the pid's cwd and arguments could not be read
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sleep(seconds: float) -> None:
    """Named so it can be replaced in a test, and imported at the top rather than inside the
    function — a default argument that names a function defined below it raises `NameError` at
    import time, which is the sort of thing a test that imports the module catches and a test
    that only runs it does not."""
    time.sleep(seconds)

# How old a test process must be before it is a leak rather than a run. Half an hour is long
# past the longest golden path in this family and short enough that a leak is still holding a
# port by the time somebody notices.
DEFAULT_OLDER_THAN = 1800

# A grace period between SIGTERM and the report. A Go test binary dies on SIGTERM; a second
# signal is not sent, because a process that ignored the first will not be helped by the second
# and a sweep that escalates on a timer is a sweep that can signal a pid that has just been
# reused.
TERM_GRACE = 2

# `ss -ltnp` renders the owner as users:(("api.test",pid=1234,fd=9)).
SS_PROCESS = re.compile(r'users:\(\("(?P<name>[^"]+)",pid=(?P<pid>\d+)')
SS_STATE = re.compile(r"^(ESTAB|ESTABLISHED)\b")

# A Go test binary, or `go test` itself. Both hold ports: the first because it *is* the server in
# a golden-path harness, the second while the binary is built and run.
TEST_BINARY = re.compile(r"\.test(\s|$)|(^|\s)go\s+test(\s|$)|/go-build\d+/")

# `-timeout 0`, in any spelling `go test` accepts, which is also how the compiled binary spells
# it (`-test.timeout=0s`).
UNBOUNDED = re.compile(r"(-test)?[.-]timeout[= ](0|0s|0m0s|0h0m0s)(\s|$)")

REAP = "REAP"
REFUSE = "REFUSE"
SKIP = "SKIP"
NOT_A_TEST = "NOT-A-TEST"


class CannotTell(RuntimeError):
    """A fact this sweep needs could not be read. Exit 2, never a pass."""


@dataclass
class Holder:
    """One listening process, and everything this sweep knows about it."""

    pid: int
    name: str
    ports: list[int] = field(default_factory=list)
    age: int | None = None
    cwd: str = ""
    args: str = ""
    repo: str = ""
    branch: str = ""
    merged: bool | None = None
    connections: int = 0
    unbounded: bool = False
    verdict: str = SKIP
    reason: str = ""

    @property
    def age_text(self) -> str:
        if self.age is None:
            return "age unknown"
        if self.age >= 3600:
            return f"{self.age // 3600}h{(self.age % 3600) // 60:02d}m"
        if self.age >= 60:
            return f"{self.age // 60}m{self.age % 60:02d}s"
        return f"{self.age}s"

    @property
    def is_test(self) -> bool:
        return bool(TEST_BINARY.search(self.args) or TEST_BINARY.search(self.name))

    def sort_key(self) -> tuple:
        return (-(self.age or 0), self.pid)


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _ss_available() -> bool:
    return any(directory and (Path(directory) / "ss").exists()
               and os.access(Path(directory) / "ss", os.X_OK)
               for directory in os.environ.get("PATH", "").split(os.pathsep))


def listening() -> dict[int, Holder]:
    """Every process with a TCP LISTEN, keyed by pid, with the ports it holds.

    `ss` rather than a Python bind, because the question is *who is holding this* and only the
    kernel's socket table names the process. A listener the kernel cannot attribute to a pid
    arrives with `pid=0` and is reported rather than dropped.
    """
    if not _ss_available():
        raise CannotTell("`ss` is not on PATH, so which processes are listening cannot be "
                         "answered, and this sweep would report a clean machine")
    out = _run(["ss", "-Hltnp"])
    if out.returncode != 0:
        raise CannotTell(f"`ss -ltnp` exited {out.returncode}: {out.stderr.strip() or 'no output'}")
    holders: dict[int, Holder] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        _, _, port_text = parts[3].rpartition(":")
        if not port_text.isdigit():
            continue
        m = SS_PROCESS.search(line)
        pid = int(m.group("pid")) if m else 0
        name = m.group("name") if m else "(not attributable)"
        holder = holders.setdefault(pid, Holder(pid=pid, name=name))
        port = int(port_text)
        # A dual-stack listener reports the same port twice — once for `0.0.0.0` and once for
        # `[::]` — and "holding 22, 22" reads as two things when it is one.
        if port not in holder.ports:
            holder.ports.append(port)
    return holders


def established(pid: int) -> int:
    """How many TCP connections this pid has, in either direction.

    The reason a refusal exists at all: a client talking to a leaked gateway is the difference
    between a process nobody is using and one a walkthrough is mid-way through.
    """
    out = _run(["ss", "-Htnp"])
    count = 0
    for line in out.stdout.splitlines():
        if f"pid={pid}," not in line and f"pid={pid})" not in line:
            continue
        if SS_STATE.match(line.strip()):
            count += 1
    return count


def ancestors() -> set[int]:
    """This process and every process above it, so the sweep cannot signal its own shell."""
    out: set[int] = set()
    pid = os.getpid()
    while pid > 1:
        out.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        # The comm field is parenthesised and may itself contain spaces or parentheses, so the
        # fields after it are found from the *last* ')'.
        try:
            pid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (ValueError, IndexError):
            break
    return out


def repo_of(cwd: str) -> tuple[str, str, bool | None]:
    """`(repository root, branch, merged into the default branch)`, or `("", "", None)`.

    Borrowed from `cleanup-workers.sh`'s discipline: a leaked process in a worktree whose branch
    is unmerged is a judgement about somebody's work, so this reports it and refuses.
    """
    if not cwd or not os.path.isdir(cwd):
        return "", "", None
    root = Path(cwd)
    for candidate in [root, *root.parents]:
        if (candidate / ".git").exists():
            root = candidate
            break
    else:
        return "", "", None
    branch = _run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
    name = branch.stdout.strip() if branch.returncode == 0 else ""
    if not name or name == "HEAD":
        return str(root), "", None
    for default in ("main", "master"):
        present = _run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", default])
        if present.returncode != 0:
            continue
        is_ancestor = _run(["git", "-C", str(root), "merge-base", "--is-ancestor", "HEAD", default])
        return str(root), name, is_ancestor.returncode == 0
    return str(root), name, None


def _cwd(pid: int) -> str:
    """A pid's working directory, from the kernel's own record of it."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _args(pid: int) -> str:
    """A pid's command line, from `/proc/<pid>/cmdline`.

    `/proc` rather than `ps` because `/proc` holds NUL-separated argv and `ps` renders it with
    spaces, losing the boundary between a program name and its first argument — which is the
    boundary `-test.timeout=0s` sits on. `ps` is the fallback for a pid `/proc` will not yield.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        out = _run(["ps", "-o", "args=", "-p", str(pid)])
        return out.stdout.strip()
    return " ".join(part for part in raw.decode("utf-8", "replace").split("\0") if part)


def classify(holder: Holder, *, older_than: int, safe_pids: frozenset[int],
             age_of, connections_of, repo_of_callable=repo_of,
             args_of=_args, cwd_of=_cwd) -> Holder:
    """Decide one holder. Returns it with `verdict` and `reason` set.

    **Every** fact this function consults arrives as a callable — the age, the connections, the
    repository, the command line and the working directory — so the whole decision can be
    exercised offline. A test that needs four leaked Go processes and three git worktrees to be
    true is a test that is never run, and a function that reads `/proc` itself cannot be tested
    without arranging the thing it is looking for.

    That was not the first shape: `args` and `cwd` were read from `/proc` inline, so a test
    could hand in a `Holder` carrying a recorded command line and have it silently overwritten
    with whatever `ps` said about a pid that did not exist — which is how three of these checks
    came to pass on an empty string and then fail on an assertion about what was in it. The
    parameter list is the contract; adding a fact to the decision means adding it here.
    """
    holder.age = age_of(holder.pid)
    holder.cwd = cwd_of(holder.pid)
    holder.args = args_of(holder.pid)
    holder.unbounded = bool(UNBOUNDED.search(holder.args))
    holder.ports.sort()

    if not holder.is_test:
        holder.verdict = NOT_A_TEST
        holder.reason = ("listening, and not a Go test — reported, never signalled: a process "
                         "this sweep cannot identify is a process it must not kill")
        return holder

    holder.repo, holder.branch, holder.merged = repo_of_callable(holder.cwd)

    if holder.pid in safe_pids or holder.pid <= 1:
        holder.verdict = REFUSE
        holder.reason = ("this is the sweep, or something it is descended from; signalling it "
                         "would signal the shell running the sweep")
        return holder
    if holder.age is None:
        holder.verdict = REFUSE
        holder.reason = ("its age could not be read, so 'older than the bound' is unknown, and "
                         "unknown is not old enough")
        return holder
    if holder.age < older_than:
        holder.verdict = SKIP
        holder.reason = (f"{holder.age_text} old, inside the {older_than}s bound — a run in "
                         f"progress is not a leak")
        return holder
    holder.connections = connections_of(holder.pid)
    if holder.connections:
        holder.verdict = REFUSE
        holder.reason = (f"{holder.connections} established connection(s): somebody is talking "
                         f"to it, so it is mid-walkthrough rather than abandoned")
        return holder
    if holder.merged is False:
        holder.verdict = REFUSE
        holder.reason = (f"branch {holder.branch!r} in {holder.repo} is not merged into its "
                         f"default branch, so this is a judgement about unmerged work rather "
                         f"than a stale process. Close the branch's worker, or merge it and "
                         f"re-run")
        return holder

    holder.verdict = REAP
    holder.reason = (f"{holder.age_text} old, no established connections, "
                     + (f"branch {holder.branch!r} merged" if holder.merged
                        else "not in a git worktree")
                     + f", holding {', '.join(str(p) for p in holder.ports)}")
    return holder


def survey(*, older_than: int = DEFAULT_OLDER_THAN, safe_pids=None,
           age_of=None, connections_of=None, args_of=_args, cwd_of=_cwd) -> list[Holder]:
    """Every listening process, classified. The whole sweep, with no signalling."""
    safe = ancestors() if safe_pids is None else safe_pids
    age = age_of or (lambda pid: _age(pid))
    conns = connections_of or established
    holders = listening()
    return sorted((classify(h, older_than=older_than, safe_pids=frozenset(safe),
                            age_of=age, connections_of=conns,
                            args_of=args_of, cwd_of=cwd_of)
                   for h in holders.values()), key=Holder.sort_key)


def _age(pid: int) -> int | None:
    out = _run(["ps", "-o", "etimes=", "-p", str(pid)])
    text = out.stdout.strip()
    return int(text) if text.isdigit() else None


def _pid_alive(pid: int) -> bool:
    """Is this pid still running?

    Signal 0 asks the kernel without delivering anything. `PermissionError` counts as **alive**:
    a process we may not signal is a process that is still there, and treating "not permitted"
    as "gone" would have a sweep report success for something it never touched.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap(holder: Holder, *, signal=os.kill, wait=sleep, alive=_pid_alive) -> str:
    """Signal one pid and report what happened. No pid list, no pattern, no escalation.

    SIGTERM and then **stop**. Escalating on a timer would be worse than useless here: a pid
    that has been reaped is a pid the kernel may hand to something else, and a sweep that
    re-signals on a schedule is a sweep that eventually signals a stranger. `TERM_GRACE` is a
    pause so the report can say what happened, not a deadline before SIGKILL.

    **`alive` is injected, and it is injected for a reason that is in this repository's history.**
    The test for this function used a pid that does not exist, so `_pid_alive` answered false,
    the escalation branch was never taken — and a mutation that added a `SIGKILL` there **passed**.
    That is the failure `AGENTS.md` names as the worst kind: a mutation that applied, reached
    nothing, and read as a guard holding. The hazard being guarded is a process that *survives*
    SIGTERM, so the branch worth testing is the one where it does.
    """
    signal(holder.pid, 15)  # SIGTERM
    wait(TERM_GRACE)
    if alive(holder.pid):
        return (f"pid {holder.pid} still alive after SIGTERM; not escalating, because a process "
                f"that ignored the first will not be helped by the second and a pid can be "
                f"reused in between")
    return f"pid {holder.pid} gone (SIGTERM)"


def render(holders: list[Holder], *, mode: str, older_than: int, applied: bool = False) -> None:
    """The decision table.

    Rendered **before** anything is signalled, and labelled as the decision it is: after a reap
    the ports are gone and re-reading the table would show a pid holding nothing, which reads as
    a contradiction rather than as a record. So the table says what was decided and the caller
    prints what was done, and the two are never mixed into one claim.
    """
    tests = [h for h in holders if h.verdict != NOT_A_TEST]
    others = len(holders) - len(tests)
    print(f"reap-port-holders.py — {mode}")
    print(f"older than {older_than}s   test processes: {len(tests)}   other listeners: {others}")
    for bucket in (REAP, REFUSE, SKIP):
        rows = [h for h in tests if h.verdict == bucket]
        if not rows:
            continue
        if bucket == REAP:
            verb = "reaped" if applied else "would reap"
        else:
            verb = {REFUSE: "refused", SKIP: "younger than the bound"}[bucket]
        print(f"\n{verb} ({len(rows)}):")
        for h in rows:
            ports = ", ".join(str(p) for p in h.ports[:6])
            more = f" (+{len(h.ports) - 6} more)" if len(h.ports) > 6 else ""
            flag = "  -timeout 0" if h.unbounded else ""
            print(f"  pid {h.pid:<8} {h.age_text:>8}  {h.name}{flag}")
            print(f"      ports: {ports}{more}")
            print(f"      cwd:   {h.cwd or 'unknown'}"
                  + (f"  [{h.repo} on {h.branch}]" if h.repo else ""))
            print(f"      why:   {h.reason}")
    if others:
        print(f"\nnot a test process ({others}), reported and not signalled:")
        for h in holders:
            if h.verdict == NOT_A_TEST:
                print(f"  pid {h.pid:<8} {h.name}  ports: "
                      f"{', '.join(str(p) for p in h.ports[:6])}")
    if not tests:
        print("\nnothing is a test process holding a port on this machine.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="reap-port-holders.py",
        description="Report — and with --yes kill — the Go test processes holding a TCP port.")
    p.add_argument("--yes", action="store_true", help="signal what is eligible (dry-run default)")
    p.add_argument("--older-than", type=int, default=DEFAULT_OLDER_THAN, metavar="SECONDS",
                   help=f"a test process must be at least this old to be a leak "
                        f"(default {DEFAULT_OLDER_THAN})")
    p.add_argument("--json", action="store_true", help="machine-readable, for the keepalive")
    p.add_argument("--explain", action="store_true", help="print this document and exit")
    args = p.parse_args(argv)

    if args.explain:
        print(__doc__)
        return 0
    if args.older_than < 0:
        print("reap-port-holders: --older-than cannot be negative; that would make every process "
              "a leak", file=sys.stderr)
        return 2

    try:
        holders = survey(older_than=args.older_than)
    except CannotTell as exc:
        print(f"reap-port-holders: COULD NOT TELL — {exc}", file=sys.stderr)
        print("reap-port-holders: exit 2. This is not a pass: the sweep did not run, so nothing "
              "here says the machine is clean.", file=sys.stderr)
        return 2

    eligible = [h for h in holders if h.verdict == REAP]

    if args.json:
        print(json.dumps({
            "older_than": args.older_than,
            "would_reap": [h.pid for h in eligible],
            "holders": [{"pid": h.pid, "name": h.name, "ports": h.ports, "age": h.age,
                         "cwd": h.cwd, "args": h.args, "repo": h.repo, "branch": h.branch,
                         "merged": h.merged, "connections": h.connections,
                         "unbounded_timeout": h.unbounded, "verdict": h.verdict,
                         "reason": h.reason} for h in holders],
        }, indent=2))
        return 0

    if not args.yes:
        render(holders, mode="DRY RUN (re-run with --yes)", older_than=args.older_than)
        if eligible:
            print(f"\nreap-port-holders: {len(eligible)} process(es) are eligible. Re-run with "
                  f"--yes to signal them, by explicit pid.", file=sys.stderr)
            return 1
        return 0

    # The decision table first, then what was done about it. Rendering after signalling would
    # show a pid holding ports it no longer holds, which reads as a contradiction rather than as
    # a record — and a report that contradicts itself is the failure this repository keeps
    # paying for, not one to introduce in the tool meant to prevent it.
    render(holders, mode="APPLYING", older_than=args.older_than, applied=True)
    print("\nsignalled:")
    for h in eligible:
        print(f"  {reap(h)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())