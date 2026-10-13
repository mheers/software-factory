#!/usr/bin/env python3
"""The allocator's own tests: scripts/port-allocation.py, and the hold that makes its answer true.

Run: `python3 scripts/test_port_allocation.py` (no test runner, no network, no live server).

## What is held here

1. **The defect, reproduced against the real tool.** a meta ticket measured six concurrent
   callers all told `29140`: the allocator answered "is this port free?" and nothing held the
   number between the answer and the caller's bind. `TheHoldMakesTheAnswerTrue` launches six real
   allocator processes against one state directory and asserts six distinct ports; the mutation
   beside it removes the write that records them and gets one number repeated, which is the
   ticket's measurement as a test.
2. **A live hold is respected and a stale one is not.** The pid test is the load-bearing one
   (`pid_alive`, signal 0, `PermissionError` counting as alive — the same discipline
   `reap-port-holders.py` uses), the TTL is the backstop, and each has a mutation that disables
   it and a control that shows the observation changing.
3. **A corrupt state file is exit 2, not "no holds".** This is the one `try/except` in the tool
   whose inversion silently restores the bug while every other test stays green, so a mutation
   that swallows it is carried here.
4. **The documented command still works.** The nine repositories call `port --repo X --slot N`
   and parse stdout with `mapfile`; `--count 2` still prints one port per line and nothing else.
5. **The mutations, with the three preconditions `test_browser_grant.py` states** — the needle is
   asserted present (a `str.replace` on a string that is not there is a silent no-op that reads
   as a passing guard), the text is asserted changed, and the mutant is asserted to parse (a
   mutant that cannot be imported proves nothing about the guard it was meant to break).

## Why the scratch family lives at 12000-12899

The scratch registry's ports are `17920…17928`, and the derivation puts their blocks at
`12000…12899`: **below** `net.ipv4.ip_local_port_range` (`32768–60999`, measured on this host),
so the kernel cannot lease one of them to an outgoing connection while a test runs. That is
`AGENTS.md`'s own rule — *ask what owns the number* — applied to the test's fixture: a port the
kernel may hand to somebody else's `curl` is not a port a test can call free by construction.
Each test also scans the ten slots for one with nothing bound, so a busy machine narrows the
choice instead of failing the run.

**Nothing here signals anything and nothing binds a port.** The processes are launched and
reaped; the state directories are temporary; the machine's real `/tmp/opencode/port-allocation`
is never touched.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "port-allocation.py"

REPO = "test-app-0"
# Nine registered ports, the coverage floor `registry_apps` enforces. The values are chosen so
# the blocks land at 12000…12899, below the ephemeral range; see the module docstring.
SCRATCH_PORTS = list(range(17920, 17920 + 9))


def load(name: str, path: Path | None = None):
    """Import the module (or a mutated copy of it) by path, so this needs no package."""
    spec = importlib.util.spec_from_file_location(name, path or SOURCE)
    assert spec and spec.loader, f"{path or SOURCE} could not be located"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pa = load("port_allocation")


def synthetic_registry(tmp: Path) -> Path:
    path = tmp / "apps.json"
    path.write_text(json.dumps({"apps": [
        {"repo": f"test-app-{i}", "port": p, "title": f"scratch app {i}"}
        for i, p in enumerate(SCRATCH_PORTS)]}))
    return path


_SLOT_CACHE: dict[int, int] = {}


def scratch_slot(app) -> int:
    """The first slot of the scratch block with nothing bound, from the real kernel table.

    Cached per block for the life of the process: the kernel query costs seconds on a loaded host
    (a `ps` per listener), and nothing in this file binds a port. A colleague binding a scratch
    port mid-suite surfaces as a named refusal in the test that asked, not as a wrong answer.
    """
    if app.base in _SLOT_CACHE:
        return _SLOT_CACHE[app.base]
    known = pa.bound_ports()
    for slot in range(pa.SLOTS):
        low, high = app.slot_range(slot)
        if all(p not in known for p in range(low, high + 1)):
            _SLOT_CACHE[app.base] = slot
            return slot
    raise AssertionError(
        f"every slot of {app.repo}'s scratch block ({app.base}-{app.high}) is bound on this "
        f"machine, so a concurrency test cannot choose a free one. That is a machine state to "
        f"look at, not a code failure.")


def dead_pid() -> int:
    """A pid that certainly existed and is certainly gone — a child of this test, waited for."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def mutate(old: str, new: str, tmp: Path, name: str) -> Path:
    """A mutated copy of the module, with the three preconditions from AGENTS.md checked.

    Returns the **path**, so a mutant can be run as a subprocess as well as imported: the
    headline defect lives in two processes, and a mutation proven only inside one import would
    never reach it.
    """
    src = SOURCE.read_text()
    if old not in src:
        raise AssertionError(
            f"MUTATION DID NOT APPLY. The needle\n  {old!r}\nis not in {SOURCE.name}. A "
            f"str.replace on a string that is not there is a silent no-op that reads as a "
            f"passing guard. (it occurs {src.count(old)} time(s))")
    mutated = src.replace(old, new, 1)
    if mutated == src:
        raise AssertionError("MUTATION DID NOT APPLY. The replacement produced identical text.")
    try:
        ast.parse(mutated)
    except SyntaxError as exc:
        raise AssertionError(
            f"MUTATION DID NOT PARSE. {exc}. A mutant that cannot be imported proves nothing "
            f"about the guard it was meant to break.") from exc
    path = tmp / f"port-allocation-{name}.py"
    path.write_text(mutated)
    return path


def clean_env(**extra) -> dict:
    """The ambient environment without the holder overrides, plus what a caller passes."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PORT_ALLOCATION_HOLDER", "PORT_ALLOCATION_HOLDER_PID")}
    env.update(extra)
    return env


def concurrent_allocations(script: Path, registry: Path, state: Path, *, slot: int,
                           count: int = 6) -> list[int]:
    """`count` real allocator processes against one state directory, launched together.

    The tool itself, through `main` — the lock, the read, the kernel check and the write as they
    are wired — not `allocate`'s function body alone. Raises if any caller refused, because a
    refusal would make "the ports were distinct" an assertion about nothing.
    """
    env = clean_env(PORT_ALLOCATION_STATE=str(state))
    procs = [subprocess.Popen(
        [sys.executable, str(script), "--registry", str(registry),
         "port", "--repo", REPO, "--slot", str(slot)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(count)]
    ports = []
    for proc in procs:
        out, err = proc.communicate()
        if proc.returncode != 0:
            raise AssertionError(f"{script.name} refused a concurrent caller: {err.strip()}")
        ports.append(int(out.strip()))
    return ports


class ScratchAllocator(unittest.TestCase):
    """A temporary directory, a scratch registry, and a slot that is genuinely free."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.state = self.tmp / "state"
        self.registry = synthetic_registry(self.tmp)
        self.app = pa.app_for(REPO, pa.registry_apps(self.registry))
        self.slot = scratch_slot(self.app)
        self.low, self.high = self.app.slot_range(self.slot)
        # An ambient holder override would anchor every grant to the same foreign pid and turn
        # the tests below into assertions about a shell nobody arranged. Remove it, restore it.
        for var in ("PORT_ALLOCATION_HOLDER", "PORT_ALLOCATION_HOLDER_PID"):
            held = os.environ.pop(var, None)
            if held is not None:
                self.addCleanup(os.environ.__setitem__, var, held)

    def run_allocator(self, *args: str, state: Path | None = None,
                      env_extra: dict | None = None) -> tuple[int, str, str]:
        env = clean_env(**(env_extra or {}))
        env["PORT_ALLOCATION_STATE"] = str(state if state is not None else self.state)
        proc = subprocess.run(
            [sys.executable, str(SOURCE), "--registry", str(self.registry), *args],
            capture_output=True, text=True, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    def run_port(self, *args: str, state: Path | None = None,
                 env_extra: dict | None = None) -> tuple[int, str, str]:
        return self.run_allocator("port", "--repo", REPO, "--slot", str(self.slot), *args,
                                  state=state, env_extra=env_extra)

    def put_grant(self, port: int, *, pid: int, claimed_at: float | None = None,
                  ttl: int = 3600) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        grants = pa.read_grants(self.state)
        grants[port] = pa.Grant(port=port, repo=REPO, slot=self.slot, pid=pid,
                                held_by="scratch", cwd=str(self.tmp),
                                claimed_at=time.time() if claimed_at is None else claimed_at,
                                ttl=ttl)
        pa.write_grants(self.state, grants)

    def allocate(self, *, count: int = 1, state: Path | None = None,
                 holders: dict | None = None, **kw):
        """`allocate` with the kernel table injected, so no in-process test depends on what this
        machine happens to be listening on."""
        return pa.allocate(self.app, self.slot, count, state=state or self.state,
                           holders={} if holders is None else holders, **kw)


class TheHoldMakesTheAnswerTrue(ScratchAllocator):
    """The ticket's measurement: concurrent callers, one state directory, distinct ports."""

    def test_six_concurrent_callers_get_six_distinct_ports(self):
        """The defect, as a test. Six processes, launched together, one state directory.

        Without a hold the first free port in the slot is the same answer for all six — the
        lowest-free rule is deterministic — which is exactly how six callers were all told
        `29140` on 2026-10-06. With the hold, each write lands before the next read.
        """
        ports = concurrent_allocations(SOURCE, self.registry, self.state, slot=self.slot)
        self.assertEqual(len(set(ports)), len(ports),
                         f"the same port was handed to two callers: {sorted(ports)}")
        self.assertTrue(all(self.low <= p <= self.high for p in ports),
                        f"every port is inside the slot {self.low}-{self.high}: {sorted(ports)}")
        self.assertEqual(set(pa.read_grants(self.state)), set(ports),
                         "every answer is a recorded grant — the answer and the hold are one "
                         "operation, so an answer with no grant behind it is the defect")

    def test_a_second_caller_takes_a_different_port_while_the_first_is_recorded(self):
        first = self.allocate()
        second = self.allocate()
        self.assertEqual([first[0].port, second[0].port], [self.low, self.low + 1],
                         "the first caller gets the lowest free port, and the second cannot "
                         "have it while the first's grant is live")

    def test_the_grant_is_anchored_to_the_caller_not_to_this_process(self):
        """`os.getppid()` is the shell about to bind the port — measured, not assumed: a shell
        runs the command substitution's command with itself as its parent, so the anchor outlives
        the allocator by the length of the caller's run."""
        grant = self.allocate()[0]
        self.assertEqual(grant.pid, os.getppid(),
                         "the default anchor is the process that ran the allocator — the caller")
        self.assertNotEqual(grant.pid, os.getpid(),
                            "and not the allocator itself, which is gone before the bind")

    def test_the_hold_remembers_the_repo_the_slot_and_its_identity(self):
        grant = self.allocate()[0]
        self.assertEqual((grant.repo, grant.slot, grant.ttl),
                         (REPO, self.slot, pa.DEFAULT_TTL))
        self.assertTrue(grant.held_by, "and what the anchor pid was doing is recorded too")


class TheHoldEndsWhenTheHolderDoes(ScratchAllocator):
    """Reclamation: the pid test, the TTL backstop, and the controls that keep them honest."""

    def test_a_grant_whose_pid_is_gone_is_reclaimed(self):
        self.put_grant(self.low, pid=dead_pid())
        got = self.allocate()
        self.assertEqual(got[0].port, self.low,
                         "a holder that is gone still holds nothing; the port must come back")
        recorded = pa.read_grants(self.state)
        self.assertEqual(len(recorded), 1,
                         "and the stale record was replaced, not left beside the new one")
        self.assertEqual(recorded[self.low].pid, os.getppid(),
                         "the reclaimed port is re-recorded to its new holder")

    def test_a_grant_past_its_ttl_is_reclaimed_even_though_its_pid_lives(self):
        """The TTL is the backstop for an anchor that outlives its run — an interactive shell
        that ran `PORT=$(… port …)` and moved on. Both sides of the boundary are asserted, so
        this is a test of the TTL and not of "the second allocation happened to differ"."""
        base = 1_000_000.0
        self.put_grant(self.low, pid=os.getpid(), claimed_at=base, ttl=60)
        just_inside = self.allocate(now=base + 59.0, alive=lambda pid: True)
        self.assertEqual(just_inside[0].port, self.low + 1,
                         "59s into a 60s TTL is still a hold")
        past = self.allocate(now=base + 60.0, alive=lambda pid: True)
        self.assertEqual(past[0].port, self.low,
                         "at the TTL the port is reclaimable again, pid or no pid")


class RefusalsNameWhatHoldsIt(ScratchAllocator):
    """A refusal that does not say who holds the port sends somebody to the wrong thing."""

    def test_a_live_grant_in_a_full_slot_refuses_and_names_bound_and_granted(self):
        known = {p: pa.Holder(port=p, pid=4242, name="scratch-holder", args="scratch")
                 for p in range(self.low, self.high)}
        self.put_grant(self.high, pid=os.getpid())
        before = (self.state / "grants.json").read_bytes()
        with self.assertRaises(pa.Refused) as caught:
            self.allocate(holders=known)
        message = str(caught.exception)
        self.assertIn("refusing before the run", message)
        self.assertIn("Bound:", message)
        self.assertIn("Granted:", message)
        self.assertIn(str(self.high), message)
        self.assertIn("release", message)
        self.assertIn("do not type a port instead", message)
        self.assertEqual((self.state / "grants.json").read_bytes(), before,
                         "and a refusal does not rewrite the table: the live grantees keep "
                         "their holds through somebody else's refusal")

    def test_a_slot_of_live_grants_refuses_rather_than_reusing_one(self):
        for port in range(self.low, self.high + 1):
            self.put_grant(port, pid=os.getpid())
        with self.assertRaises(pa.Refused) as caught:
            self.allocate()
        self.assertIn("Granted:", str(caught.exception))


class TheStateIsNotOptional(ScratchAllocator):
    """The one `try/except` whose inversion re-opens the reclaim defect while everything else stays green."""

    def test_a_corrupt_state_file_is_exit_2_and_prints_no_port(self):
        self.state.mkdir()
        (self.state / "grants.json").write_text("{ this is not json")
        code, out, err = self.run_port()
        self.assertEqual(code, 2, f"an unparseable state is cannot-tell, not a pass: {err}")
        self.assertEqual(out.strip(), "",
                         "and no port is printed — the answer depends on the hold")
        self.assertIn("not valid JSON", err)

    def test_a_state_that_cannot_be_written_is_exit_2_and_prints_no_port(self):
        blocked = self.tmp / "not-a-directory"
        blocked.write_text("")
        code, out, err = self.run_port(state=blocked)
        self.assertEqual(code, 2, f"no state, no hold, no port: {err}")
        self.assertEqual(out.strip(), "")
        self.assertIn("could not be created or opened", err)

    def test_a_state_file_without_the_grants_shape_is_exit_2(self):
        self.state.mkdir()
        (self.state / "grants.json").write_text(json.dumps({"version": 1}))
        code, out, err = self.run_port()
        self.assertEqual(code, 2, f"a file that is not this tool's shape is not empty: {err}")
        self.assertEqual(out.strip(), "")

    def test_a_grant_missing_a_field_is_exit_2_not_a_default(self):
        self.state.mkdir()
        (self.state / "grants.json").write_text(json.dumps(
            {"version": 1, "grants": {str(self.low): {"repo": REPO, "slot": self.slot}}}))
        code, out, err = self.run_port()
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")
        self.assertIn("without", err)


class TheDocumentedCommandStillWorks(ScratchAllocator):
    """The nine repositories' contract: same command, one port per line on stdout, nothing else."""

    def test_count_two_prints_two_distinct_ports_one_per_line(self):
        code, out, err = self.run_port("--count", "2")
        self.assertEqual(code, 0, err)
        ports = [int(line) for line in out.splitlines()]
        self.assertEqual(len(ports), 2, f"mapfile reads exactly one port per line ({out!r})")
        self.assertEqual(len(set(ports)), 2)

    def test_the_ttl_flag_and_the_holder_env_reach_the_record(self):
        code, out, err = self.run_port(
            "--ttl", "45", env_extra={"PORT_ALLOCATION_HOLDER": "the scratch walk"})
        self.assertEqual(code, 0, err)
        grant = pa.read_grants(self.state)[int(out.strip())]
        self.assertEqual(grant.ttl, 45)
        self.assertEqual(grant.held_by, "the scratch walk")

    def test_the_state_flag_works_without_the_env_var(self):
        alt = self.tmp / "alt-state"
        env = clean_env()
        env.pop("PORT_ALLOCATION_STATE", None)
        proc = subprocess.run(
            [sys.executable, str(SOURCE), "--registry", str(self.registry), "--state", str(alt),
             "port", "--repo", REPO, "--slot", str(self.slot)],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((alt / "grants.json").is_file(), "--state is where the grant went")

    def test_a_negative_ttl_is_refused(self):
        code, out, err = self.run_port("--ttl", "-1")
        self.assertEqual(code, 3, err)
        self.assertEqual(out.strip(), "")
        self.assertIn("negative", err)

    def test_explain_documents_the_hold_and_the_two_new_commands(self):
        proc = subprocess.run([sys.executable, str(SOURCE), "--explain"],
                              capture_output=True, text=True)
        for needle in ("PORT_ALLOCATION_STATE", "grants", "release", "flock"):
            self.assertIn(needle, proc.stdout,
                          f"--explain is the tool's own manual, and it must document {needle!r}")

    def test_release_frees_a_named_grant_and_an_unnamed_port_is_reported(self):
        code, out, _ = self.run_port()
        self.assertEqual(code, 0)
        port = int(out.strip())
        code, out, err = self.run_allocator("release", "--port", str(port))
        self.assertEqual(code, 0, err)
        self.assertIn(f"released 127.0.0.1:{port}", out)
        code, out, _ = self.run_port()
        self.assertEqual(code, 0)
        self.assertEqual(int(out.strip()), port, "the released port is allocatable again")
        code, _, err = self.run_allocator("release", "--port", str(port + 4))
        self.assertEqual(code, 0)
        self.assertIn("nothing to release", err,
                      "a release that matched nothing is reported, not silently a success")

    def test_release_stale_drops_only_the_stale_grants(self):
        self.put_grant(self.low, pid=dead_pid())
        self.put_grant(self.low + 1, pid=os.getpid())
        code, out, err = self.run_allocator("release", "--stale")
        self.assertEqual(code, 0, err)
        self.assertIn(f"released 127.0.0.1:{self.low}", out)
        self.assertNotIn(f"released 127.0.0.1:{self.low + 1}", out,
                         "a live grant is not swept by --stale; that is a decision, not a sweep")
        self.assertEqual(set(pa.read_grants(self.state)), {self.low + 1},
                         "and the live grant survives it")

    def test_a_port_named_beside_stale_is_not_reported_as_never_recorded(self):
        """The overlap the two selectors share: the port is stale *and* named, and the report
        must not contradict itself by saying it was both released and never recorded."""
        self.put_grant(self.low, pid=dead_pid())
        code, out, err = self.run_allocator("release", "--stale", "--port", str(self.low))
        self.assertEqual(code, 0, err)
        self.assertIn(f"released 127.0.0.1:{self.low}", out)
        self.assertNotIn("nothing to release", err,
                         "the stale pass had just released it; 'no grant is recorded' on the "
                         "line below would be a report contradicting itself")

    def test_a_release_with_neither_flag_is_refused(self):
        code, out, err = self.run_allocator("release")
        self.assertEqual(code, 2, f"an unqualified release would free every live grant: {err}")
        self.assertIn("--port", err)
        self.assertIn("--stale", err)

    def test_grants_reports_live_and_stale(self):
        dead = dead_pid()
        self.put_grant(self.low, pid=os.getpid())
        self.put_grant(self.low + 1, pid=dead, claimed_at=time.time() - 10)
        code, out, err = self.run_allocator("grants")
        self.assertEqual(code, 0, err)
        self.assertIn(f"127.0.0.1:{self.low}  LIVE", out)
        self.assertIn(f"STALE (pid {dead} is gone)", out)


class MutationsBreakTheHold(unittest.TestCase):
    """A guard nobody has broken is a claim, not a guard.

    Each mutation asserts the observation **changed** against its control — the real tool, driven
    the same way in the same test — because a mutation that applied and changed nothing is
    indistinguishable from a passing guard.
    """

    # Two processes for the lock mechanism, choreographed so the assertion has no timing
    # assumption in it: the holder reports whether the contender got in *while the holder still
    # held the lock*, and it only looks after the contender has announced that it has tried.
    # See `test_the_state_lock_admits_one_holder_at_a_time`.
    HOLDER_SCRIPT = textwrap.dedent("""\
        import importlib.util, sys, time
        from pathlib import Path
        module_path, state, tried, got = sys.argv[1:5]
        spec = importlib.util.spec_from_file_location("pa_hold", module_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["pa_hold"] = mod
        spec.loader.exec_module(mod)
        tried, got = Path(tried), Path(got)
        with mod.StateLock(Path(state)):
            print("HELD", flush=True)
            deadline = time.time() + 10
            while not tried.exists() and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.5)
            print("GOT-EXISTS" if got.exists() else "STILL-BLOCKED", flush=True)
        """)

    CONTENDER_SCRIPT = textwrap.dedent("""\
        import importlib.util, sys
        from pathlib import Path
        module_path, state, tried, got = sys.argv[1:5]
        spec = importlib.util.spec_from_file_location("pa_contend", module_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["pa_contend"] = mod
        spec.loader.exec_module(mod)
        Path(tried).write_text("trying")
        with mod.StateLock(Path(state)):
            Path(got).write_text("got")
        """)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.registry = synthetic_registry(self.tmp)
        self.app = pa.app_for(REPO, pa.registry_apps(self.registry))
        self.slot = scratch_slot(self.app)
        self.low, self.high = self.app.slot_range(self.slot)
        for var in ("PORT_ALLOCATION_HOLDER", "PORT_ALLOCATION_HOLDER_PID"):
            held = os.environ.pop(var, None)
            if held is not None:
                self.addCleanup(os.environ.__setitem__, var, held)

    def mutant(self, old: str, new: str, name: str):
        return load(f"pa-mutant-{name}", mutate(old, new, self.tmp, name))

    def put(self, state: Path, port: int, **kw) -> None:
        state.mkdir(parents=True, exist_ok=True)
        grants = pa.read_grants(state)
        fields = {"repo": REPO, "slot": self.slot, "pid": os.getpid(), "held_by": "scratch",
                  "cwd": str(self.tmp), "claimed_at": time.time(), "ttl": 3600}
        fields.update(kw)
        grants[port] = pa.Grant(port=port, **fields)
        pa.write_grants(state, grants)

    def run_script(self, script: Path, state: Path) -> tuple[int, str, str]:
        env = clean_env(PORT_ALLOCATION_STATE=str(state))
        proc = subprocess.run(
            [sys.executable, str(script), "--registry", str(self.registry),
             "port", "--repo", REPO, "--slot", str(self.slot)],
            capture_output=True, text=True, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    def lock_choreography(self, module_path: Path) -> tuple[str, bool]:
        """Run holder and contender against one module copy; the holder reports what it saw."""
        state = self.tmp / f"lockstate-{module_path.stem}"
        tried = self.tmp / f"tried-{module_path.stem}"
        got = self.tmp / f"got-{module_path.stem}"
        holder = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER_SCRIPT, str(module_path), str(state),
             str(tried), str(got)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first = holder.stdout.readline().strip()
        if first != "HELD":
            _, err = holder.communicate()
            raise AssertionError(f"the holder never announced the lock: {first!r} {err}")
        contender = subprocess.Popen(
            [sys.executable, "-c", self.CONTENDER_SCRIPT, str(module_path), str(state),
             str(tried), str(got)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        verdict = holder.stdout.readline().strip()
        holder.communicate(timeout=30)
        contender.communicate(timeout=30)
        return verdict, got.exists()

    def test_the_state_lock_admits_one_holder_at_a_time(self):
        """MUTATION: remove the flock — the serialisation, and with it the guarantee.

        The concurrency test above is the race observed end to end; this is the mechanism
        underneath it, asserted without a timing assumption: the holder takes the lock and reports
        whether the contender got in *while the holder still held it*, and it only looks after
        the contender has announced that it tried. A sentinel file with no kernel lock behind it
        would read as held while admitting everyone, which is the `O_EXCL`-left-behind defect
        `browser-grant.py` warns about.
        """
        verdict, got = self.lock_choreography(SOURCE)
        self.assertEqual(verdict, "STILL-BLOCKED",
                         "the contention was synchronous: the holder had the lock when it checked")
        self.assertTrue(got, "and the contender got the lock once the holder was done with it")
        mutant = mutate("                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                        "                return self",
                        "                pass\n                return self",
                        self.tmp, "no-lock")
        verdict, got = self.lock_choreography(mutant)
        self.assertEqual(verdict, "GOT-EXISTS",
                         "with the flock removed the contender walked in while the holder still "
                         "held the lock — which is what six concurrent callers would do")

    def test_without_the_write_concurrent_callers_share_a_port(self):
        """MUTATION: remove the write that records the grant — the defect as it was measured.

        The mutant keeps the lock, the read and the kernel check and only drops the record, so
        the six callers that are distinct against the real tool collapse onto one number: nothing
        remembers the first answer.
        """
        control = concurrent_allocations(SOURCE, self.registry, self.tmp / "control",
                                         slot=self.slot)
        self.assertEqual(len(set(control)), len(control), "the real tool is the control")
        path = mutate(
            "        for grant in claimed:\n            grants[grant.port] = grant\n"
            "        write_grants(state, grants)",
            "        for grant in claimed:\n            pass\n        pass",
            self.tmp, "no-write")
        shared = concurrent_allocations(path, self.registry, self.tmp / "mutant", slot=self.slot)
        self.assertLess(len(set(shared)), len(shared),
                        f"without the record the callers share a port: {sorted(shared)}")

    def test_ignoring_live_grants_hands_one_port_to_two_callers(self):
        """MUTATION: `free_in` stops consulting `held` — a read that happens and changes nothing."""
        control = self.tmp / "control"
        first = pa.allocate(self.app, self.slot, 1, state=control, holders={})
        second = pa.allocate(self.app, self.slot, 1, state=control, holders={})
        self.assertNotEqual(first[0].port, second[0].port, "the real tool is the control")
        mutant = self.mutant(
            "    free = [p for p in range(low, high + 1) if p not in known and p not in held]",
            "    free = [p for p in range(low, high + 1) if p not in known]",
            "no-held")
        state = self.tmp / "mutant"
        mfirst = mutant.allocate(self.app, self.slot, 1, state=state, holders={})
        msecond = mutant.allocate(self.app, self.slot, 1, state=state, holders={})
        self.assertEqual(mfirst[0].port, msecond[0].port,
                         "with live grants ignored the second caller gets the first one's port")

    def test_treating_a_dead_pid_as_alive_never_lets_the_port_go(self):
        """MUTATION: `is_stale` never says yes to a gone pid — every leak becomes permanent."""
        dead = dead_pid()
        control = self.tmp / "control"
        self.put(control, self.low, pid=dead)
        got = pa.allocate(self.app, self.slot, 1, state=control, holders={})
        self.assertEqual(got[0].port, self.low, "the real tool reclaims the dead holder's port")
        mutant = self.mutant(
            "        if not alive(self.pid):\n            return True\n"
            "        return now - self.claimed_at >= self.ttl",
            "        if False:\n            return True\n"
            "        return now - self.claimed_at >= self.ttl",
            "never-stale")
        state = self.tmp / "mutant"
        self.put(state, self.low, pid=dead)
        got = mutant.allocate(self.app, self.slot, 1, state=state, holders={})
        self.assertNotEqual(got[0].port, self.low,
                            "with the pid test disabled the dead holder's port is refused to "
                            "everyone after them")

    def test_treating_the_ttl_as_infinite_keeps_an_expired_hold(self):
        """MUTATION: the TTL backstop removed — an anchor that outlives its run holds for ever."""
        base = 1_000_000.0
        control = self.tmp / "control"
        self.put(control, self.low, pid=os.getpid(), claimed_at=base, ttl=60)
        got = pa.allocate(self.app, self.slot, 1, state=control, holders={},
                          now=base + 3600, alive=lambda pid: True)
        self.assertEqual(got[0].port, self.low, "the real tool reclaims past the TTL")
        mutant = self.mutant("        return now - self.claimed_at >= self.ttl",
                             "        return False", "no-ttl")
        state = self.tmp / "mutant"
        self.put(state, self.low, pid=os.getpid(), claimed_at=base, ttl=60)
        got = mutant.allocate(self.app, self.slot, 1, state=state, holders={},
                              now=base + 3600, alive=lambda pid: True)
        self.assertNotEqual(got[0].port, self.low,
                            "with the TTL disabled an expired hold is kept against the caller")

    def test_swallowing_an_unparseable_state_turns_cannot_tell_into_a_pass(self):
        """MUTATION: `read_grants` returns {} on bad JSON — "I could not read the holds" becomes
        "there are no holds", the one inversion that re-opens the reclaim defect in silence."""
        control = self.tmp / "control"
        control.mkdir(parents=True)
        (control / "grants.json").write_text("{ not json")
        code, out, err = self.run_script(SOURCE, control)
        self.assertEqual(code, 2, "the real tool is cannot-tell, not a pass")
        self.assertEqual(out.strip(), "")
        mutant = mutate("    except ValueError as exc:\n        raise CannotTell(\n",
                        "    except ValueError as exc:\n        return {}\n    if False:\n"
                        "        raise CannotTell(\n", self.tmp, "swallow")
        code, out, err = self.run_script(mutant, self.tmp / "mutant")
        self.assertEqual(code, 0,
                         f"the mutant hands out a port over a state it cannot read ({err})")
        self.assertTrue(out.strip().isdigit(), f"and prints it: {out!r}")

    def test_anchoring_to_the_allocator_itself_loses_the_hold_the_moment_it_exits(self):
        """MUTATION: the anchor becomes `os.getpid()` — the allocator's own pid, dead by the
        time its caller binds, so every hold it records is instantly stale and the reclaim defect returns
        through the anchor. This is the mutation the ticket's "with the caller's pid" clause is
        about."""
        mutant = self.mutant("            pid = int(env) if env.strip() else os.getppid()",
                             "            pid = int(env) if env.strip() else os.getpid()",
                             "own-pid")
        control = pa.allocate(self.app, self.slot, 1, state=self.tmp / "control", holders={})
        self.assertEqual(control[0].pid, os.getppid(), "the real anchor is the caller")
        got = mutant.allocate(self.app, self.slot, 1, state=self.tmp / "mutant", holders={})
        self.assertEqual(got[0].pid, os.getpid(),
                         "the mutated anchor is the allocator itself, gone before the bind")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--explain":
        print(__doc__)
        sys.exit(0)
    unittest.main(verbosity=2)
