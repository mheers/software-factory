#!/usr/bin/env python3
"""port-allocation.py — one allocation of golden-path ports for the whole family.

    python3 scripts/port-allocation.py plan
    python3 scripts/port-allocation.py range  --repo press
    python3 scripts/port-allocation.py port   --repo press --slot 2 --count 1
    python3 scripts/port-allocation.py check
    python3 scripts/port-allocation.py holders --port 29181
    python3 scripts/port-allocation.py grants
    python3 scripts/port-allocation.py release --port 28300
    python3 scripts/port-allocation.py --explain

Offline. It reads `apps.json`, asks the kernel which TCP ports are bound, records the ports it
hands out as grants under `$PORT_ALLOCATION_STATE`, and prints. It starts nothing and contacts no
network service; the state directory is the only thing it ever writes to.

## WHY THIS FILE EXISTS

Six repositories typed their `demo.md` ports by hand:

    social-studio     29084 29100 29181
    media             28077
    press             28300 28399
    player-registry   29200 29299 29300 29399 29400 29499
    portal            28090 28100 28110 28120 28130 28140 28141
    opponent-brief    38086
    agent-hub         29087 29187

Six files, six chances to collide, and **not one of the six was wrong on its own.** A typed
constant is not wrong until somebody else types the same one, so a reviewer reading a diff sees
`OPA=29181` and has nothing to object to.

Then `social-studio` bound `29181` for its OPA container while a colleague's
`go test` in `player-registry` already held it:

    127.0.0.1:29181   pid 3652333   1670s   go test -run TestTheGoldenPathGateway -timeout 0

So the studio's policy endpoint **was another repository's test server**, and every authorization
call answered `403`. `403` is a *correct* answer from an OPA that does not know this application,
so the transcript could not tell a policy refusal from a wrong-server answer — and the verdict
named a symptom whose cause was a prerequisite. That is the tell this file answers: refuse
**before** the run, at a point where the only possible complaint is "that port is taken", instead
of failing twenty lines into a transcript with a status code that has two causes.

## THE ALLOCATION, AND WHY IT IS DERIVED RATHER THAN TYPED

Each application already owns a unique integer in `apps.json`: its registered `port`. So the
golden-path block is a function of that number:

    base  = 28000 + (port - 18080) * 100        # one 100-port block per application
    slot  = base + slot_number * 10              # ten slots of ten ports inside the block

Two properties follow from the arithmetic rather than from anybody's care:

- **Unique `port` ⇒ disjoint blocks.** The multiplier is 100 and the block is 100 wide, so two
  applications cannot be handed the same base. There is no list to keep in step.
- **Registered ports are `18xxx`, blocks are `28xxx`–`29xxx`,** so a golden-path port cannot be an
  application's own. `check` asserts both against `apps.json` instead of trusting the constants.

The derivation is also **stable**: adding an application does not renumber the blocks of the ones
already there, which is what an index-derived scheme would do — and index-derived would invalidate
all six transcripts on every insertion.

`plan` prints the whole family, so the allocation is readable without running anything:

    $ python3 scripts/port-allocation.py plan
    agent-hub          28700-28799   registered 18087
    contracts          28500-28599   registered 18085

## WHY SLOTS, AND WHY ONLY TEN

Two blocks of one repository run concurrently — `demo.md` blocks 1 and 2 of `player-registry`
each start a server — so "one block per repository" would leave them sharing a hundred ports and
relying on a bind race to break the tie. A slot is the *within-repository* half of the same
guarantee: block 1 takes slot 0, block 2 takes slot 1, and they cannot choose the same number
however the kernel is feeling. Ten slots of ten ports is 100, which is the whole block; the
constant is here, in one file, so the day a repository needs more it is changed here rather than
typed into six transcripts.

## THE ANSWER AND THE HOLD ARE ONE OPERATION

`port` used to answer *is this port free?* and hand the number back, and **nothing held it
between the answer and the caller's bind**. Measured 2026-10-06: six concurrent callers were all
told `29140`. The winner bound it; the losers read the winner's process, and a transcript died
with `net::ERR_CONNECTION_REFUSED` — a fact about the machine printed as a verdict about a branch
(a meta ticket).

So the answer is now a **grant**. Inside one `flock` on `$PORT_ALLOCATION_STATE/lock`, `port`
reads the recorded grants, drops the stale ones, chooses from what the kernel's table and the
live grants leave free, and writes the chosen ports to `grants.json` — all before it prints (the
kernel's table is read just before the lock, because it is the slow half and it is advisory; the
grants are the half that must be serialised). A second caller arriving while a grant is live is
handed a different port from the same slot, however the two interleave.

A grant records the port, the repository, the slot, the **pid of the calling shell** (the process
that is about to bind, not this one: a shell runs the command substitution's command with itself
as its parent, so `os.getppid()` is the caller), what that pid is, its working directory, when it
was claimed and its TTL — default `3600` seconds, `--ttl` overrides it.

A grant stops being a hold when:

- **its pid is gone** — the same signal-0 test `scripts/reap-port-holders.py` uses, with
  "permission denied" counting as alive, because a process this user may not signal is a process
  that is still there; or
- **its TTL has passed** — the backstop for a pid that outlives its run, which an interactive
  shell does;

or when it is released explicitly:

    python3 scripts/port-allocation.py grants
    python3 scripts/port-allocation.py release --port 28300
    python3 scripts/port-allocation.py release --stale

`grants` reports what is recorded and whether each hold is live; `release` drops named grants or
every stale one. Neither is required for correctness — a stale grant is reclaimed on the next
`port` — but an operator who knows a run is over should not have to wait for a pid or a TTL.

The state file is read **strictly**: a `grants.json` that cannot be parsed is exit 2, not an
empty one. Treating "I could not read the holds" as "there are no holds" is precisely how a held
port gets handed to a second caller.

## `port` REFUSES; IT DOES NOT RETRY INTO A COLLISION

`port` scans the slot and prints free ports. **If the slot is exhausted it refuses: exit 3, on
stderr, naming the holder** — pid, command, age, working directory — because "no free port" with
no holder is not actionable and sends somebody to look at the wrong thing.

It cannot hand back a bound port, and it cannot hand the same number to two callers: the answer
and the hold are one operation. That is stronger than the ticket's "fail loudly if the chosen
port is already bound": there is no chosen port to be bound, because nothing is typed. `holders`
exists for the diagnosis from the other end — given a port that *is* held, say by whom — which is
the question you ask after a transcript has already failed. `grants` is the same question answered
from the hold side, for a port nobody has bound yet.

## WHERE A GOLDEN PATH CALLS THIS

Every application fetches `shared/meta`, so the path from a worktree is fixed:

    PORT="$(python3 ../shared/meta/scripts/port-allocation.py port \
             --repo press --slot 0)"

The copy-pasted `freeport()` shell function currently in `press` and
`player-registry` — duplicated into every block of every transcript, six times over —
is this command with the derivation typed in by hand. That is the defect it already half fixed,
and why it collided with a repository that never adopted it.

## Exit codes

    0  printed, and the printed ports are recorded as grants under `$PORT_ALLOCATION_STATE`
    2  cannot tell — no registry, a registry that is not the shape this reads, an unknown
       repository, a duplicate `port` in the registry (the derivation cannot be trusted), or the
       grant state could not be read or written (a state file that cannot be parsed is not an
       empty one)
    3  refused — no free port in the slot; the kernel holders and the live grants are named on
       stderr

Exit 2 is never 0. "I could not tell" must not read as "there is nothing to report", which is
the reason this family has three of its own monitors rather than two.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
META = HERE.parent
REGISTRY = META / "apps.json"
WORKSPACE = Path(os.environ.get("WORKSPACE", META.parent)).resolve()

# --- The derivation. Four constants, and this is the only place in the family they appear. ---

# The band registered application ports live in, and the band golden-path ports live in. They are
# disjoint by 10000, which is what makes "a golden-path port is not an application's own port" a
# consequence rather than a hope. `check` asserts the real registry agrees with both.
REGISTERED_BASE = 18000
GOLDEN_BASE = 28000

# One hundred ports per application, and ten slots of ten inside it. Written as the two halves
# they are: a block width that makes `base` unique per application, and a slot size that gives
# concurrent blocks of one repository disjoint numbers.
BLOCK_WIDTH = 100
SLOT_WIDTH = 10
SLOTS = BLOCK_WIDTH // SLOT_WIDTH

# A coverage floor, so a registry that has lost half its applications is reported rather than
# producing a confident short list. Nine registered applications.
MIN_APPS = 9

# --- The grant state. The answer and the hold are one operation, and this is where the hold
# lives. ---

# Outside every repository, because the hold is shared by every checkout on this host: a caller
# in a worktree and the coordinator in the main checkout must see one table, and a checkout that
# is deleted must not take the holds with it. `PORT_ALLOCATION_STATE` overrides it, and the
# default is the same `/tmp/opencode` directory `browser-grant.py` keeps the session locks in.
STATE_DIR = Path(os.environ.get("PORT_ALLOCATION_STATE", "/tmp/opencode/port-allocation"))
GRANTS_FILE = "grants.json"
LOCK_FILE = "lock"

# How long a grant is believed. A golden path is minutes, not hours; the pid anchor reclaims most
# grants the moment their caller is gone, and the TTL is the backstop for an anchor that outlives
# its run — an interactive shell that ran `PORT=$(… port …)` and then moved on to something else.
DEFAULT_TTL = 3600

# How long `allocate` waits for the state lock before it calls the state unreadable. The critical
# section is one read, one `ss` and one atomic write — milliseconds — so this is not a tuning
# knob: it is the point past which "somebody else holds the lock" is no longer a race but a fault.
LOCK_WAIT = 15.0

# The fields a grant is made of. Written as a constant so `read_grants` can refuse a record with
# a missing one rather than defaulting it: a grant nobody can reason about is not a grant that
# can be believed, which is the same reason `registry_apps` requires its keys.
GRANT_FIELDS = ("repo", "slot", "pid", "held_by", "cwd", "claimed_at", "ttl")

# `ss -ltnp` renders the owning process as users:(("api.test",pid=1234,fd=9)). Anything that is
# not that shape means the guard cannot see the holder, which is a fact it must be able to report
# rather than a format it may assume.
SS_PROCESS = re.compile(r'users:\(\("(?P<name>[^"]+)",pid=(?P<pid>\d+)')

# A Go test binary, or `go test` itself. Both hold ports: the first because it *is* the server in
# a golden-path harness, the second while the binary is being built and run.
TEST_BINARY = re.compile(r"\.test(\s|$)|(^|\s)go\s+test(\s|$)|/go-build\d+/")


class CannotTell(RuntimeError):
    """The port could not be chosen for want of a fact. Exit 2, never a pass."""


class Refused(RuntimeError):
    """No free port, and the holders are known. Exit 3 — a refusal, not a cannot-tell."""


@dataclass
class Holder:
    """One process holding a TCP port, as far as the kernel and `ss` will say."""

    port: int
    pid: int
    name: str
    seconds: int | None = None
    cwd: str = ""
    args: str = ""

    @property
    def age(self) -> str:
        if self.seconds is None:
            return "age unknown"
        if self.seconds >= 3600:
            return f"{self.seconds // 3600}h{(self.seconds % 3600) // 60:02d}m old"
        if self.seconds >= 60:
            return f"{self.seconds // 60}m{self.seconds % 60:02d}s old"
        return f"{self.seconds}s old"

    @property
    def is_test(self) -> bool:
        return bool(TEST_BINARY.search(self.args) or TEST_BINARY.search(f"{self.name} {self.args}"))


@dataclass
class App:
    repo: str
    port: int
    title: str = ""

    @property
    def base(self) -> int:
        return GOLDEN_BASE + (self.port - REGISTERED_BASE - 80) * BLOCK_WIDTH

    @property
    def high(self) -> int:
        return self.base + BLOCK_WIDTH - 1

    def slot_range(self, slot: int) -> tuple[int, int]:
        low = self.base + slot * SLOT_WIDTH
        return low, low + SLOT_WIDTH - 1


@dataclass
class Grant:
    """One port handed to one caller, and everything needed to decide whether it still is theirs.

    A grant is not a lock and it is not durable by design: what it has to do is close the window
    between the allocator's answer and the caller's bind, on one host, and the caller's own pid
    anchors it there. The record is a *claim*, and every reader re-derives the verdict from the
    pid and the TTL rather than trusting a stored boolean — a boolean would be stale the moment
    it was written, which is the whole class of defect this file exists against. A pid can be
    reused, so an anchor that looks alive is not a proof either; the TTL bounds that, which is
    why both tests are here rather than only the pid.
    """

    port: int
    repo: str
    slot: int
    pid: int
    held_by: str
    cwd: str
    claimed_at: float
    ttl: int

    def age(self, now: float) -> str:
        seconds = max(0, int(now - self.claimed_at))
        if seconds >= 3600:
            return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
        if seconds >= 60:
            return f"{seconds // 60}m{seconds % 60:02d}s"
        return f"{seconds}s"

    def is_stale(self, *, now: float, alive) -> bool:
        """Is this hold over? **The pid test is the load-bearing one; the TTL is the backstop.**

        `alive` is a callable so the whole decision can be exercised offline, the same reason
        `reap-port-holders.py::classify` takes `age_of` and `connections_of`: a test that needs a
        dead pid and a clock to have moved is a test that is never run.
        """
        if not alive(self.pid):
            return True
        return now - self.claimed_at >= self.ttl

    def stale_reason(self, *, now: float, alive) -> str:
        """Why this grant is over, as a sentence, or `""` while it is live."""
        if not alive(self.pid):
            return f"pid {self.pid} is gone"
        if now - self.claimed_at >= self.ttl:
            return f"older than its {self.ttl}s TTL"
        return ""

    def describe(self, now: float) -> str:
        """One line a person can act on, in `describe_holder`'s shape for the kernel's holders."""
        parts = [f"127.0.0.1:{self.port}",
                 f"granted to pid {self.pid} ({self.held_by or 'command line unreadable'})",
                 f"{self.age(now)} old, ttl {self.ttl}s"]
        if self.cwd:
            parts.append(f"cwd {self.cwd}")
        return " — ".join(parts)


# ------------------------------------------------------------------- the registry


def registry_apps(registry: Path = REGISTRY, min_apps: int = MIN_APPS) -> list[App]:
    """Every application with its registered port, read from `apps.json`.

    `.apps` and `.repo` are required rather than `.get`, and `port` is required too: a missing
    key would silently shrink the family this tool claims to have allocated for, and a smaller
    family is a confident wrong answer rather than an error.
    """
    if not registry.exists():
        raise CannotTell(f"{registry} does not exist — there is no registry to allocate from")
    try:
        data = json.loads(registry.read_text())
        apps = [App(repo=a["repo"], port=a["port"], title=a.get("title", ""))
                for a in data["apps"]]
    except (KeyError, TypeError) as exc:
        raise CannotTell(
            f"{registry} does not have the shape this reads — it wants .apps[] with repo and port "
            f"(missing {exc}). A missing key is never tolerated: it would shrink the family this "
            f"allocation claims to cover") from exc
    except ValueError as exc:
        raise CannotTell(f"{registry} is not valid JSON: {exc}") from exc

    seen = []
    for a in apps:
        if a.repo and a not in seen:
            seen.append(a)
    if len(seen) < min_apps:
        raise CannotTell(f"{registry} names {len(seen)} application(s), below the floor of "
                         f"{min_apps} — the registry changed shape or the family shrank, and an "
                         f"allocation over fewer than the whole family is not an allocation for "
                         f"this family")
    return seen


def app_for(repo: str, apps: list[App]) -> App:
    """The one application named `repo`, or CannotTell.

    An unknown repository is never given a synthesised block. A guess here is exactly the defect
    this file exists to end: a port nobody allocated, which is how a collision starts.

    It also refuses when `repo` shares its registered port with another application, because
    then two applications have one block and the answer would be a number two transcripts could
    both be handed. A defect elsewhere in the registry is *not* a reason to refuse an unrelated
    repository — that would make one bad entry stop the other eight, which trades a correct
    answer for a missing one.
    """
    matches = [a for a in apps if a.repo == repo]
    if not matches:
        known = ", ".join(a.repo for a in apps)
        raise CannotTell(f"{repo!r} is not in the registry, so it has no allocated block. The "
                         f"registered applications are: {known}. Add it to apps.json rather than "
                         f"choosing a port by hand")
    if len(matches) > 1:
        raise CannotTell(f"{repo!r} appears {len(matches)} times in the registry, so its block is "
                         f"ambiguous")
    app = matches[0]
    sharers = [a.repo for a in apps if a is not app and a.port == app.port]
    if sharers:
        raise CannotTell(f"registered port {app.port} is shared by {app.repo} and "
                         f"{', '.join(sharers)}, so {app.repo}'s block is not unique to it and "
                         f"the derivation cannot be trusted to answer for it. Give each "
                         f"application its own port in apps.json")
    return app


def allocation_defects(apps: list[App]) -> list[str]:
    """What is wrong with the allocation itself, as a list of sentences.

    Three checks, each about a property the derivation relies on, and each asked of the real
    registry rather than assumed:

    - **unique `port` per application**, without which two applications get one base;
    - **blocks pairwise disjoint**, which unique ports imply only while the arithmetic holds;
    - **no block contains any registered port**, which is the "a golden-path port is not an
      application's own" claim `GOLDEN_BASE` is separated by 10000 to make true.
    """
    defects: list[str] = []

    by_port: dict[int, list[str]] = {}
    for a in apps:
        by_port.setdefault(a.port, []).append(a.repo)
    for port, repos in sorted(by_port.items()):
        if len(repos) > 1:
            defects.append(
                f"port {port} is registered to {len(repos)} applications ({', '.join(repos)}), so "
                f"they are allocated the same block and two transcripts can choose one port")

    for i, a in enumerate(apps):
        for b in apps[i + 1:]:
            if a.base <= b.high and b.base <= a.high:
                defects.append(f"{a.repo} ({a.base}-{a.high}) and {b.repo} ({b.base}-{b.high}) "
                               f"overlap, so the two blocks are not disjoint")

    registered = {a.port for a in apps}
    for a in apps:
        inside = sorted(p for p in registered if a.base <= p <= a.high)
        if inside:
            defects.append(f"{a.repo}'s block {a.base}-{a.high} contains registered application "
                           f"port(s) {', '.join(str(p) for p in inside)}, so a golden-path port "
                           f"could be an application's own")
        for port in (REGISTERED_BASE, GOLDEN_BASE):
            if not a.base <= port <= a.high:
                pass
            else:
                defects.append(f"{a.repo}'s block contains the {port} band boundary; the "
                               f"derivation is producing a block inside the band it must avoid")

    for a in apps:
        if not 1 <= a.base <= 65535 or not 1 <= a.high <= 65535:
            defects.append(f"{a.repo}'s block {a.base}-{a.high} is outside 1..65535, so the "
                           f"derivation does not fit this address space")
    return defects


# ------------------------------------------------------------------ the kernel's answer


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def bound_ports() -> dict[int, Holder]:
    """Every loopback-or-wildcard TCP LISTEN on this host, with its holder.

    `ss` rather than a Python bind, because the question is *who is holding this*, and only the
    kernel's table names the process. A holder the kernel cannot name is reported as an unknown
    holder (`pid=0`) rather than dropped — a port that is bound with no readable owner is exactly
    the state a golden path has to refuse, so dropping it would turn a refusal into a bind
    failure twenty lines later.
    """
    if not _ss_available():
        raise CannotTell("`ss` is not on PATH, so which ports are bound cannot be answered. "
                         "Install iproute2; do not guess — a guess here is the collision")
    out = _run(["ss", "-Hltnp"])
    if out.returncode != 0:
        raise CannotTell(f"`ss -ltnp` exited {out.returncode}: {out.stderr.strip() or 'no output'}")
    holders: dict[int, Holder] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        # LISTEN 0 4096 127.0.0.1:29181 0.0.0.0:* users:(...)
        if len(parts) < 4:
            continue
        local = parts[3]
        _, _, port_text = local.rpartition(":")
        if not port_text.isdigit():
            continue
        port = int(port_text)
        m = SS_PROCESS.search(line)
        if m:
            pid = int(m.group("pid"))
            holder = Holder(port=port, pid=pid, name=m.group("name"))
            holder.seconds = _process_age(pid)
            holder.cwd = _process_cwd(pid)
            holder.args = _process_args(pid)
        else:
            holder = Holder(port=port, pid=0, name="(not attributable)")
        holders.setdefault(port, holder)
    return holders


def _ss_available() -> bool:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and (Path(directory) / "ss").exists():
            return os.access(Path(directory) / "ss", os.X_OK)
    return False


def _process_age(pid: int) -> int | None:
    out = _run(["ps", "-o", "etimes=", "-p", str(pid)])
    text = out.stdout.strip()
    return int(text) if text.isdigit() else None


def _process_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _process_args(pid: int) -> str:
    out = _run(["ps", "-o", "args=", "-p", str(pid)])
    return out.stdout.strip()


def validate_slot(app: App, slot: int, count: int) -> None:
    """The refusals that need neither the kernel nor the grant state, so a bad request refuses
    before anything is locked, read, or written."""
    if not 0 <= slot < SLOTS:
        raise Refused(f"--slot {slot} is outside 0..{SLOTS - 1}, so it names no slot of "
                      f"{app.repo}'s block {app.base}-{app.high}. {SLOTS} slots of {SLOT_WIDTH} "
                      f"ports is the whole block; a transcript needing more slots wants a bigger "
                      f"BLOCK_WIDTH in scripts/port-allocation.py, not a port typed by hand")
    if count < 1:
        raise Refused(f"--count {count} asks for no ports, which is not an allocation")
    if count > SLOT_WIDTH:
        raise Refused(f"--count {count} exceeds the {SLOT_WIDTH} ports in a slot, so it cannot be "
                      f"satisfied without borrowing another slot's numbers — which is the "
                      f"collision this tool exists to prevent. Ask for a smaller count, or raise "
                      f"SLOT_WIDTH in scripts/port-allocation.py")


def free_in(app: App, slot: int, count: int, holders: dict[int, Holder] | None = None,
            held: dict[int, Grant] | None = None) -> list[int]:
    """`count` free ports from `app`'s `slot`, lowest first, or Refused.

    "Free" is a conjunction of two answers, and they are different questions: the kernel says
    *nothing is listening on it right now*, and the grant table says *nobody holds it between an
    answer and a bind*. A port either of them covers is not free, and the refusal names both.

    Lowest-first rather than randomised: a transcript that records its own port is easier to read
    when the port is the first one in the slot, and the collision that matters is a *typed*
    constant, which this cannot be. The slot is disjoint from every other slot, so "lowest free
    in this slot" cannot collide with another block of the same repository either.
    """
    validate_slot(app, slot, count)
    known = bound_ports() if holders is None else holders
    held = held or {}
    low, high = app.slot_range(slot)
    free = [p for p in range(low, high + 1) if p not in known and p not in held]
    if len(free) < count:
        taken = [(p, known[p]) for p in range(low, high + 1) if p in known]
        granted = [(p, held[p]) for p in range(low, high + 1) if p in held]
        now = time.time()
        parts = []
        if taken:
            parts.append("Bound: " + "; ".join(
                f"{p} held by pid {h.pid} {h.name!r}, {h.age}, cwd {h.cwd or 'unknown'}"
                for p, h in taken))
        if granted:
            parts.append("Granted: " + "; ".join(g.describe(now) for _, g in granted))
        detail = ". ".join(parts) or "nothing in this slot is listening and no grant covers it"
        raise Refused(
            f"refusing before the run: {app.repo} slot {slot} ({low}-{high}) has {len(free)} free "
            f"port(s) and {count} were asked for. {detail}. Close the holder, release the grant "
            f"(`port-allocation.py release --port <port>`), or pick another slot — do not type a "
            f"port instead")
    return free[:count]


# ----------------------------------------------------------------------- grant state


def pid_alive(pid: int) -> bool:
    """Is this pid still running? Signal 0 asks the kernel without delivering anything.

    `PermissionError` counts as **alive**, verbatim the discipline of
    `scripts/reap-port-holders.py::_pid_alive`: a process this user may not signal is a process
    that is still there, and treating "not permitted" as "gone" would reclaim a grant from a
    holder that is very much alive. The two tools read this fact the same way on purpose —
    "stale" must not mean two different things in two files of one repository.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def grants_path(state: Path) -> Path:
    return state / GRANTS_FILE


def grant_to_json(grant: Grant) -> dict:
    return {"repo": grant.repo, "slot": grant.slot, "pid": grant.pid, "held_by": grant.held_by,
            "cwd": grant.cwd, "claimed_at": grant.claimed_at, "ttl": grant.ttl}


def grant_from_json(port: int, obj: object, path: Path) -> Grant:
    """One grant, strictly: a missing key is CannotTell, never a default.

    The same discipline as `registry_apps`, for the same reason: defaulting a field would make a
    record that cannot be reasoned about look like one that can, and the reader who needs the
    missing fact (whose pid? how long?) is the reader deciding whether to hand the port out.
    """
    if not isinstance(obj, dict):
        raise CannotTell(f"{path} holds {obj!r} for port {port}, which is not a grant record")
    missing = [k for k in GRANT_FIELDS if k not in obj]
    if missing:
        raise CannotTell(
            f"{path} holds a grant for port {port} without {', '.join(missing)} — a record this "
            f"tool cannot read is not the same as no grant, and reading it as one would hand a "
            f"held port to a second caller")
    try:
        return Grant(port=port, repo=str(obj["repo"]), slot=int(obj["slot"]), pid=int(obj["pid"]),
                     held_by=str(obj["held_by"]), cwd=str(obj["cwd"]),
                     claimed_at=float(obj["claimed_at"]), ttl=int(obj["ttl"]))
    except (TypeError, ValueError) as exc:
        raise CannotTell(f"{path} holds a grant for port {port} whose fields do not have the "
                         f"types this reads ({exc})") from exc


def read_grants(state: Path) -> dict[int, Grant]:
    """Every recorded grant, **or CannotTell**. A file that cannot be read is never "no grants".

    This is the rule the whole hold rests on, and it is one line of failure away from being
    inverted: "I could not read the holds" and "there are no holds" produce the same empty dict
    if a `try/except` swallows the error, and the second one hands a held port to a second caller.
    So every unreadable, unparseable or malformed state is a refusal to answer, not an empty
    table, and the same three-part distinction `registry_apps` makes.
    """
    path = grants_path(state)
    if not path.exists():
        return {}
    try:
        text = path.read_text()
    except OSError as exc:
        raise CannotTell(
            f"{path} exists but could not be read ({exc}), so which ports are held cannot be "
            f"answered — and 'I could not read the holds' must not read as 'there are none'"
        ) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CannotTell(
            f"{path} is not valid JSON: {exc}. A state file that cannot be parsed is not the "
            f"same as no holds — reading it as one would hand a held port to a second caller. "
            f"Fix it, or delete it on purpose: it is machine-local state, safe to delete") from exc
    if not isinstance(data, dict) or not isinstance(data.get("grants"), dict):
        raise CannotTell(
            f"{path} does not have the {{version, grants}} shape this writes "
            f"({type(data).__name__}), so its holds cannot be trusted and will not be treated "
            f"as none")
    grants: dict[int, Grant] = {}
    for key, obj in data["grants"].items():
        try:
            port = int(key)
        except (TypeError, ValueError) as exc:
            raise CannotTell(
                f"{path} keys a grant by {key!r}, which is not a port number — a record this "
                f"tool cannot read is not the same as no grant") from exc
        grants[port] = grant_from_json(port, obj, path)
    return grants


def write_grants(state: Path, grants: dict[int, Grant]) -> None:
    """The whole table, atomically, so a reader never sees half a write.

    `os.replace` within the same directory: the new table either is or is not there. A plain
    truncate-and-rewrite would let a crash leave a truncated file, which `read_grants` refuses as
    unparseable — honest, but a state a tool can wedge itself into is not worth building.
    """
    path = grants_path(state)
    body = {"version": 1,
            "grants": {str(p): grant_to_json(grants[p]) for p in sorted(grants)}}
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(body, indent=1) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise CannotTell(
            f"the grant could not be recorded in {path} ({exc}), so no port was handed out: a "
            f"port this tool cannot hold is the collision it exists to prevent. Set "
            f"PORT_ALLOCATION_STATE (or --state) to a writable directory") from exc


class StateLock:
    """The state directory's exclusive lock, held for one choose-and-record.

    `flock` rather than an `O_EXCL` sentinel, for the reason `browser-grant.py` gives: the kernel
    drops it when the holder dies, so an allocator killed between the choose and the write cannot
    wedge the state directory for everybody after it.
    """

    def __init__(self, state: Path):
        self.state = Path(state)
        self.fd: int | None = None

    def __enter__(self) -> "StateLock":
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(self.state / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise CannotTell(
                f"the grant state at {self.state} could not be created or opened ({exc}), so no "
                f"port can be held and none will be handed out. Set PORT_ALLOCATION_STATE (or "
                f"--state) to a writable directory") from exc
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise CannotTell(
                        f"the state lock {self.state / LOCK_FILE} stayed held for "
                        f"{LOCK_WAIT:.0f}s, so the allocation cannot be serialised — a fault, "
                        f"not the ordinary contention the lock exists for")
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def resolve_holder_pid(explicit: int | None = None) -> int:
    """The pid a grant is anchored to: **the caller**, not this process.

    The default is `os.getppid()`, and it is not a guess — measured: a shell that runs
    `PORT="$(python3 … port …)"` runs the command substitution's command with the shell itself
    as its parent (bash does not interpose a second fork), so the parent is the process that is
    about to bind the port and it outlives this one. `PORT_ALLOCATION_HOLDER_PID` (or
    `--holder-pid`) overrides it for a caller that knows better — a supervisor pid, a server
    that started first.
    """
    if explicit is not None:
        pid = int(explicit)
    else:
        env = os.environ.get("PORT_ALLOCATION_HOLDER_PID", "")
        try:
            pid = int(env) if env.strip() else os.getppid()
        except ValueError as exc:
            raise CannotTell(
                f"PORT_ALLOCATION_HOLDER_PID={env!r} is not a pid, so the grant cannot be "
                f"anchored to anything") from exc
    if pid <= 0:
        raise CannotTell(f"holder pid {pid} is not a process, so a grant cannot be anchored to it")
    return pid


def holder_identity(pid: int) -> str:
    """What the anchor pid is, for a human reading `grants` after a failure: its command line."""
    args = _process_args(pid)
    return args[:160] if args else "command line unreadable"


def allocate(app: App, slot: int, count: int, *, state: Path, ttl: int = DEFAULT_TTL,
             holder_pid: int | None = None, holder: str | None = None,
             holders: dict[int, Holder] | None = None, now: float | None = None,
             alive=None) -> list[Grant]:
    """`count` free ports from `app`'s `slot`, **recorded as grants**, or Refused.

    The answer and the hold are one operation: read the recorded grants, drop the stale ones,
    choose, and write the chosen ports back — all inside one `flock`, and the ports are not
    printed until the write succeeded. A second caller cannot be handed the same number however
    the two interleave, because its read happens after the first one's write.

    The kernel's table is read **before** the lock, and that split is deliberate: it is the slow
    half on a busy host (`ss` plus a `ps` per listener — seconds with sixty listeners, measured)
    and it is *advisory*, because a port can be bound a moment after any answer. Holding the lock
    across it would queue six callers behind five seconds each and turn contention into a lock
    timeout, which is the defect wearing a different hat. The grants are the part that must be
    serialised; the kernel is a filter applied to what the grants leave free.

    `holders`, `now` and `alive` are injected in tests, and they are injected **whole** — a test
    that supplied its own kernel table but let liveness come from the live machine is the
    half-injected function `test_reap_port_holders.py` records as an untested one wearing a test.
    """
    validate_slot(app, slot, count)
    if ttl < 0:
        raise Refused(f"--ttl {ttl} is negative, so the grant would be stale before it was "
                      f"recorded — that is the race this tool exists to remove, not a setting")
    now = time.time() if now is None else float(now)
    alive = pid_alive if alive is None else alive
    known = bound_ports() if holders is None else holders
    pid = resolve_holder_pid(holder_pid)
    if holder is not None:
        identity = holder
    else:
        identity = os.environ.get("PORT_ALLOCATION_HOLDER") or holder_identity(pid)
    cwd = _process_cwd(pid)
    with StateLock(state):
        grants = read_grants(state)
        for stale in [g for g in grants.values() if g.is_stale(now=now, alive=alive)]:
            del grants[stale.port]
        chosen = free_in(app, slot, count, known, dict(grants))
        claimed = [Grant(port=p, repo=app.repo, slot=slot, pid=pid, held_by=identity,
                         cwd=cwd, claimed_at=now, ttl=ttl) for p in chosen]
        for grant in claimed:
            grants[grant.port] = grant
        write_grants(state, grants)
    return claimed


def describe_holder(holder: Holder) -> str:
    """One line a person can act on."""
    who = f"pid {holder.pid} {holder.name!r}" if holder.pid else "an unattributable process"
    parts = [f"127.0.0.1:{holder.port}", who, holder.age]
    if holder.cwd:
        parts.append(f"cwd {holder.cwd}")
    if holder.args and holder.is_test:
        parts.append(f"cmd {holder.args[:120]}")
    return " — ".join(parts)


# ------------------------------------------------------------------------ the commands


def cmd_plan(args, apps: list[App]) -> int:
    for a in sorted(apps, key=lambda x: x.base):
        print(f"{a.repo:<28} {a.base}-{a.high}   registered {a.port}   "
              f"slots {a.base}..{a.base + (SLOTS - 1) * SLOT_WIDTH} in steps of {SLOT_WIDTH}")
    print(f"\n{len(apps)} applications, {BLOCK_WIDTH} ports each, {SLOTS} slots of {SLOT_WIDTH}. "
          f"Registered ports are in the {REGISTERED_BASE} band, blocks in the {GOLDEN_BASE} band.")
    defects = allocation_defects(apps)
    for d in defects:
        print(f"DEFECT: {d}", file=sys.stderr)
    return 1 if defects else 0


def cmd_range(args, apps: list[App]) -> int:
    app = app_for(args.repo, apps)
    if args.slot is None:
        print(f"{app.base}-{app.high}")
        return 0
    low, high = app.slot_range(args.slot)
    print(f"{low}-{high}")
    return 0


def state_dir(args) -> Path:
    return Path(args.state) if args.state else STATE_DIR


def cmd_port(args, apps: list[App]) -> int:
    app = app_for(args.repo, apps)
    grants = allocate(app, args.slot, args.count, state=state_dir(args), ttl=args.ttl,
                      holder_pid=args.holder_pid, holder=args.holder)
    for grant in grants:
        print(grant.port)
    return 0


def cmd_grants(args, apps: list[App]) -> int:
    """What is recorded, and whether each hold is still live — read without writing.

    Read-only on purpose: printing the table must not change it, or a diagnosis taken mid-run
    would reclaim the hold it was taken to inspect. Stale grants are reported as stale and left
    for the next `port` (or an explicit `release --stale`) to clear.
    """
    state = state_dir(args)
    grants = read_grants(state)
    if not grants:
        print(f"port-allocation: no grants are recorded in {state}. Nothing has asked this "
              f"allocator for a port since the state directory was last cleared.",
              file=sys.stderr)
        return 0
    now = time.time()
    live = 0
    for grant in sorted(grants.values(), key=lambda g: g.port):
        reason = grant.stale_reason(now=now, alive=pid_alive)
        if reason:
            verdict = f"STALE ({reason})"
        else:
            live += 1
            verdict = f"LIVE ({grant.age(now)} old, ttl {grant.ttl}s)"
        cwd = f"  cwd {grant.cwd}" if grant.cwd else ""
        print(f"127.0.0.1:{grant.port}  {verdict}  pid {grant.pid} {grant.held_by!r}{cwd}")
    print(f"port-allocation: {len(grants)} grant(s) in {state}, {live} live. A stale grant is "
          f"reclaimed by the next `port`, or dropped by `release --stale`.", file=sys.stderr)
    return 0


def cmd_release(args, apps: list[App]) -> int:
    """Drop grants: named ports, or everything stale.

    Under the same lock `port` takes, so a release cannot race a claim for the same table. A
    named port with no grant is reported rather than ignored — a release that silently matched
    nothing is indistinguishable from one that worked.
    """
    state = state_dir(args)
    if not args.port and not args.stale:
        print("port-allocation: `release` wants --port PORT (one or more) or --stale. An "
              "unqualified release is not offered: it would free every live grant on the "
              "machine, which is a decision, not a default.", file=sys.stderr)
        return 2
    now = time.time()
    released: list[Grant] = []
    missing: list[int] = []
    with StateLock(state):
        grants = read_grants(state)
        if args.stale:
            for grant in list(grants.values()):
                if grant.is_stale(now=now, alive=pid_alive):
                    released.append(grant)
                    del grants[grant.port]
        already = {g.port for g in released}
        for port in args.port:
            if port in already:
                # The stale pass above just released it. Popping it again would find nothing and
                # report "no grant is recorded" directly under the line saying it was released —
                # a report contradicting itself, which is the failure this repository pays for
                # most often, not one to introduce in the command that hands a port back.
                continue
            grant = grants.pop(port, None)
            if grant is not None:
                released.append(grant)
            else:
                missing.append(port)
        if released:
            write_grants(state, grants)
    for grant in sorted(released, key=lambda g: g.port):
        print(f"released 127.0.0.1:{grant.port} — was pid {grant.pid} ({grant.held_by}), "
              f"{grant.age(now)} old, ttl {grant.ttl}s")
    for port in missing:
        print(f"port-allocation: no grant is recorded for port {port}, so there is nothing to "
              f"release", file=sys.stderr)
    return 0


def cmd_check(args, apps: list[App]) -> int:
    defects = allocation_defects(apps)
    known = None
    if not args.offline:
        known = bound_ports()
    print(f"port-allocation: {len(apps)} applications read from {args.registry or REGISTRY}")
    for d in defects:
        print(f"DEFECT: {d}", file=sys.stderr)
    if known is not None:
        leaked = [h for p, h in sorted(known.items())
                  if any(a.base <= p <= a.high for a in apps) and h.is_test]
        for h in leaked:
            print(f"HELD BY A TEST: {describe_holder(h)}", file=sys.stderr)
        if leaked:
            print(f"port-allocation: {len(leaked)} allocated port(s) are held by a test process, "
                  f"which is the state that produced the 403 in a meta ticket. "
                  f"scripts/reap-port-holders.py reports them", file=sys.stderr)
    print(f"port-allocation: {len(defects)} defect(s) in the allocation itself, "
          f"{len(leaked) if known is not None else 'not'} checked against what is bound now")
    return 1 if defects else 0


def cmd_holders(args, apps: list[App]) -> int:
    known = bound_ports()
    if args.port:
        h = known.get(args.port)
        if h is None:
            print(f"port-allocation: nothing is listening on {args.port}", file=sys.stderr)
            return 0
        print(describe_holder(h))
        return 0
    rows = [(p, h) for p, h in sorted(known.items())
            if h.is_test or any(a.base <= p <= a.high for a in apps)]
    if not rows:
        print("port-allocation: no test process holds a port, and nothing holds an allocated "
              "block. This machine is quiet.", file=sys.stderr)
        return 0
    for p, h in rows:
        owner = next((a.repo for a in apps if a.base <= p <= a.high), "outside every block")
        print(f"{describe_holder(h)}  [{owner}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="port-allocation.py",
        description="Allocate this family's golden-path ports from apps.json, hold each answer "
                    "until the caller is done, and refuse rather than hand back a bound or "
                    "already-held one.")
    p.add_argument("--registry", default=str(REGISTRY), metavar="FILE",
                   help="read the family from this registry instead of apps.json")
    p.add_argument("--state", default=None, metavar="DIR",
                   help="the grant state directory (default $PORT_ALLOCATION_STATE or "
                        f"{STATE_DIR})")
    p.add_argument("--explain", action="store_true", help="print this document and exit")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("plan", help="every application's block, one line each")
    sp.set_defaults(func=cmd_plan)

    sr = sub.add_parser("range", help="print one repository's block, or one slot of it")
    sr.add_argument("--repo", required=True)
    sr.add_argument("--slot", type=int, default=None)
    sr.set_defaults(func=cmd_range)

    spo = sub.add_parser("port", help="print N free ports from a slot, recorded as grants, "
                                      "or refuse")
    spo.add_argument("--repo", required=True)
    spo.add_argument("--slot", type=int, default=0)
    spo.add_argument("--count", type=int, default=1)
    spo.add_argument("--ttl", type=int, default=DEFAULT_TTL, metavar="SECONDS",
                     help=f"how long the grant holds the port (default {DEFAULT_TTL}s; an "
                          f"interactive shell's claim needs one, a script's is reclaimed by its "
                          f"pid)")
    spo.add_argument("--holder", default=None, metavar="TEXT",
                     help="what to record as the holder's identity (default "
                          "$PORT_ALLOCATION_HOLDER, or the caller's own command line)")
    spo.add_argument("--holder-pid", type=int, default=None, metavar="PID",
                     help="the pid the grant is anchored to (default "
                          "$PORT_ALLOCATION_HOLDER_PID, or the caller's pid)")
    spo.set_defaults(func=cmd_port)

    sc = sub.add_parser("check", help="assert the allocation is disjoint and off the app ports")
    sc.add_argument("--offline", action="store_true",
                    help="do not ask the kernel what is bound (the allocation is checked either "
                         "way; this only skips the live half)")
    sc.set_defaults(func=cmd_check)

    sh = sub.add_parser("holders", help="who holds a port, or who holds a test port")
    sh.add_argument("--port", type=int, default=None)
    sh.set_defaults(func=cmd_holders)

    sg = sub.add_parser("grants", help="the recorded grants, and whether each is still live")
    sg.set_defaults(func=cmd_grants)

    srel = sub.add_parser("release", help="drop grants: named ports, or everything stale")
    srel.add_argument("--port", type=int, action="append", default=[], metavar="PORT",
                      help="release the grant on this port (repeat for several)")
    srel.add_argument("--stale", action="store_true",
                      help="release every grant whose pid is gone or whose TTL has passed")
    srel.set_defaults(func=cmd_release)

    args = p.parse_args(argv)
    if args.explain or args.command is None:
        print(__doc__)
        return 0 if args.explain else 2

    try:
        apps = registry_apps(Path(args.registry), args.min_apps if hasattr(args, "min_apps")
                             else MIN_APPS)
        return args.func(args, apps)
    except Refused as exc:
        print(f"port-allocation: REFUSED — {exc}", file=sys.stderr)
        print("port-allocation: exit 3. Nothing was started, so no assertion has been evaluated "
              "and nothing can be falsified by this run.", file=sys.stderr)
        return 3
    except CannotTell as exc:
        print(f"port-allocation: COULD NOT TELL — {exc}", file=sys.stderr)
        print("port-allocation: exit 2. This is not a pass: the port was not chosen, so nothing "
              "here says anything about whether it was free.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())