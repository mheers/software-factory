#!/usr/bin/env python3
"""Tests for the browser grant: the recognisable shape, and the lock that makes it honest.

Run: `python3 scripts/test_browser_grant.py` (no test runner, no network, **no browser**).

## What is held here

1. **The verbatim failure**, classified both ways round. It is BUSY when this run did
   not hold the session and BROWSER-FAULT when it did, and the second of those is the whole
   reason the classifier exists: a text match alone cannot tell "somebody else was busy" from
   "the daemon is unwell", and a retry is only safe if you know which.
2. **A bare `exit status 1` is FAIL, never BUSY.** The ticket's complaint is that both arrive as
   `exit status 1`; if the classifier ever answered BUSY on the status alone it would have
   reproduced the defect it was written to remove.
3. **Undecidable is a verdict.** No exit status, or nobody checked the lock, gives UNDECIDED —
   never a guess. `agent-browser` confirms success in prose, so its text genuinely cannot decide.
4. **Refusing does not run the command**, because a run that evaluates an assertion under a
   shared session is how a false red gets believed.
5. **`STALE` exists.** A record whose lock the kernel already released is reported, not deleted.
6. **The override is loud and refuses a live holder**, which is what keeps the automation
   additive rather than a replacement for a coordinator-held grant.
7. **The mutations**, with the three preconditions from AGENTS.md on every one: that it
   **applied** (the old string is asserted present, so a `str.replace` on a string that is not
   there cannot read as a passing guard), that the mutant **parses** (`ast.parse`, so a mutation
   that breaks the import proves nothing), and that the failure would name **this** guard.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "browser-grant.py"


def load(name: str, path: Path | None = None):
    """Import the module (or a mutated copy of it) by path, so this needs no package."""
    spec = importlib.util.spec_from_file_location(name, path or SOURCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bg = load("browser-grant")

# Verbatim from the browser-fault report. Kept byte-for-byte including
# the accented selector, because a fixture that has been tidied is a fixture that can drift
# away from the thing it is a fixture of.
TICKET_FAILURE = (
    "agent-browser click button[title=\"Français\"] failed: exit status 1\n"
    "Failed to read: Resource temporarily unavailable (os error 11)\n"
    "(after 5 retries - daemon may be busy or unresponsive)\n"
)


def args_for(state: Path, **kw):
    """A stand-in for parsed argv. `cmd_status` and `cmd_force_release` read plain attributes,
    so the tests hand them one rather than going through argparse for every case."""
    class A:
        pass
    a = A()
    a.state = state
    a.json = False
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class StateDir(unittest.TestCase):
    """Every test that touches the lock gets its own state directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _args(self, **kw):
        return args_for(self.state, **kw)


# ------------------------------------------------------------------ 1. the shape


class TheVerbatimFailure(unittest.TestCase):
    def test_contended_it_is_BUSY_and_says_it_is_not_evidence(self):
        verdict, reason = bg.classify(TICKET_FAILURE, 1, held=False)
        self.assertEqual(verdict, bg.BUSY)
        self.assertIn("NOT evidence", reason,
                      "a BUSY verdict that does not say it is not evidence invites exactly the "
                      "false positive this ticket is about: a rerun that passes being taken as proof")

    def test_held_it_is_BROWSER_FAULT_because_nobody_else_could_be_busy(self):
        """The distinction a text match cannot make, and the one that makes a retry safe."""
        verdict, reason = bg.classify(TICKET_FAILURE, 1, held=True)
        self.assertEqual(verdict, bg.BROWSER_FAULT)
        self.assertIn("HELD THE SESSION LOCK", reason)
        self.assertIn("not evidence", reason)
        self.assertIn("not contention", reason,
                      "a BROWSER-FAULT reported as contention sends the reader to re-run, which "
                      "is the response that cannot fix a sick daemon")

    def test_every_transport_token_is_recognised(self):
        for display, _ in bg.TRANSPORT_TOKENS:
            with self.subTest(token=display):
                verdict, reason = bg.classify(f"Failed to read: {display}", 1, held=False)
                self.assertEqual(verdict, bg.BUSY)
                self.assertIn(repr(display), reason, "the reason must name the token it matched")

    def test_a_near_miss_errno_is_not_a_transport_token(self):
        """`os error 11` must not match `os error 111`, or `os error 2` (ENOENT)."""
        for text in ("Failed to read: os error 111", "Failed to read: os error 2",
                     "Failed to read: os error 104"):
            with self.subTest(text=text):
                self.assertEqual(bg.classify(text, 1, held=False)[0], bg.FAIL)

    def test_a_page_level_error_is_not_contention(self):
        """A selector that is not there is the code's problem and must stay FAIL."""
        for text in ("✗ page.evaluate: SecurityError: Failed to read the 'localStorage' property",
                     "✗ element not found: button[title=\"Français\"]",
                     "✗ net::ERR_CONNECTION_REFUSED at http://127.0.0.1:18081"):
            with self.subTest(text=text[:40]):
                self.assertEqual(bg.classify(text, 1, held=True)[0], bg.FAIL,
                                 "a real assertion failure must not be excused as contention")


class TheComplaintItself(unittest.TestCase):
    def test_a_bare_exit_status_1_is_FAIL_never_BUSY(self):
        """The ticket says both kinds arrive as `exit status 1`. Only one may be BUSY."""
        verdict, reason = bg.classify("agent-browser click @e2 failed: exit status 1", 1, held=False)
        self.assertEqual(verdict, bg.FAIL)
        self.assertIn("real failure", reason)

    def test_exit_0_is_PASS_even_though_the_cli_prints_prose(self):
        verdict, _ = bg.classify("✓ Opponent Brief — https://127.0.0.1:28086/", 0, held=True)
        self.assertEqual(verdict, bg.PASS)

    def test_exit_0_with_a_stray_token_is_still_PASS_but_says_why(self):
        verdict, reason = bg.classify("✓ done\n(after 5 retries - daemon may be busy)", 0, held=False)
        self.assertEqual(verdict, bg.PASS, "the CLI reported success; the token does not overturn it")
        self.assertIn("reported success", reason)


class UndecidableIsAVerdict(unittest.TestCase):
    """`fleet-watch.py` has a `NONE` that never becomes MERGEABLE, for the same reason."""

    def test_no_exit_status_is_UNDECIDED_not_a_guess(self):
        verdict, reason = bg.classify(TICKET_FAILURE, None, held=False)
        self.assertEqual(verdict, bg.UNDECIDED)
        self.assertIn("cannot decide", reason)
        self.assertIn(bg.PROSE_SUCCESS, reason,
                      "the reason must say why text alone cannot decide — that the CLI confirms "
                      "success in prose — or it reads as a refusal to answer")

    def test_token_present_but_lock_never_checked_is_UNDECIDED(self):
        verdict, reason = bg.classify(TICKET_FAILURE, 1, held=None)
        self.assertEqual(verdict, bg.UNDECIDED)
        self.assertIn("cannot be told apart", reason)

    def test_the_five_verdicts_have_five_distinct_exit_codes(self):
        codes = [bg.EXIT_CODE[v] for v in (bg.PASS, bg.FAIL, bg.BUSY, bg.BROWSER_FAULT, bg.SKIPPED)]
        self.assertEqual(len(set(codes)), len(codes), f"two verdicts share an exit code: {codes}")

    def test_BUSY_uses_EX_TEMPFAIL_so_a_retry_loop_can_be_told_apart(self):
        self.assertEqual(bg.EXIT_CODE[bg.BUSY], 75,
                         "75 is sysexits EX_TEMPFAIL, which is what this is: retry later")


# ---------------------------------------------------------------------- 2. the lock


def spawn_holder(state: Path, session: str, label: str = "press #7") -> subprocess.Popen:
    """A real separate process that takes the session lock and writes the record.

    Simulating a dead holder by closing an fd in this process would leave the record's pid
    pointing at a *live* process — this one — so `STALE` would be asserted with
    `pid_alive=True`, which is a state that cannot occur in production and proves nothing. A
    subprocess that is then killed is the real thing.
    """
    code = (
        "import importlib.util,sys,time;"
        f"spec=importlib.util.spec_from_file_location('bg', {str(SOURCE)!r});"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        f"lock=m.SessionLock({session!r}, {str(state)!r});"
        f"ok=lock.acquire(label={label!r}, granted_by='coordinator');"
        "print('held' if ok else 'failed', flush=True);time.sleep(120)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline().strip()
    if line != "held":
        proc.kill()
        raise AssertionError(f"the holder subprocess reported {line!r} instead of 'held'")
    return proc


class TheLockIsReal(StateDir):
    def test_two_fds_on_one_file_conflict_even_inside_one_process(self):
        """The mechanism rests on this. If flock did not conflict per open file description,
        every test below would be testing a lock that does not lock."""
        a = bg.SessionLock("s", self.state)
        b = bg.SessionLock("s", self.state)
        self.assertTrue(a.acquire(), "the first holder must get the lock")
        self.addCleanup(a.release)
        self.assertFalse(b.acquire(),
                         "a second holder got the lock in the SAME process, so the lock is not "
                         "doing what this file claims")

    def test_two_different_sessions_do_not_block_each_other(self):
        a = bg.SessionLock("press-gp", self.state)
        b = bg.SessionLock("media-gp", self.state)
        self.assertTrue(a.acquire())
        self.addCleanup(a.release)
        self.assertTrue(b.acquire(), "distinct sessions are the partition; they must not collide")
        b.release()

    def test_a_holder_that_dies_releases_the_lock(self):
        """Why flock and not an O_EXCL sentinel: the kernel drops it, so a Herdr restart in the
        middle of a golden path cannot leave the session locked for ever."""
        holder = spawn_holder(self.state, "s")
        self.addCleanup(holder.kill)
        self.assertTrue(bg.is_locked("s", self.state), "a live holder must read as locked")
        holder.kill()
        holder.wait(timeout=10)
        self.assertFalse(bg.is_locked("s", self.state),
                         "the lock survived its holder's death, so a crashed worker wedges the "
                         "session for good and the coordinator must clear it by hand")

    def test_release_removes_the_record_and_locks_again(self):
        a = bg.SessionLock("s", self.state)
        self.assertTrue(a.acquire(label="press #7", granted_by="coordinator"))
        self.assertEqual(bg.session_state("s", self.state)["state"], "HELD")
        a.release()
        self.assertEqual(bg.session_state("s", self.state)["state"], "FREE")
        self.assertTrue(bg.SessionLock("s", self.state).acquire(), "the lock must be reusable")


class RefusingDoesNotRunTheCommand(StateDir):
    def test_a_refused_command_never_executes(self):
        """The point of refusing rather than diagnosing afterwards: no assertion is evaluated,
        so nothing can be falsified."""
        sentinel = self.state / "ran"
        held = bg.SessionLock("press-gp", self.state)
        self.assertTrue(held.acquire(label="press #7"))
        self.addCleanup(held.release)

        err = io.StringIO()
        code = bg.run_locked("press-gp", ["sh", "-c", f"touch {sentinel}"], label="press #9",
                             granted_by="", wait=0, state=self.state, out=err)
        self.assertEqual(code, bg.EXIT_CODE[bg.BUSY])
        self.assertFalse(sentinel.exists(),
                         "the refused command RAN. A refused run that still executes has already "
                         "evaluated its assertions against somebody else's page.")

    def test_the_refusal_names_the_holder_and_says_it_is_not_evidence(self):
        held = bg.SessionLock("press-gp", self.state)
        held.acquire(label="press #7")
        self.addCleanup(held.release)
        err = io.StringIO()
        bg.run_locked("press-gp", ["true"], label="press #9", granted_by="", wait=0,
                      state=self.state, out=err)
        text = err.getvalue()
        self.assertIn("BROWSER-GRANT: BUSY", text)
        self.assertIn("press #7", text, "the refusal must name who holds it, or it is a shrug")
        self.assertIn("not evidence", text)

    def test_the_banner_is_one_greppable_line_per_run(self):
        err = io.StringIO()
        bg.run_locked("solo-gp", ["true"], label="", granted_by="", wait=0,
                      state=self.state, out=err)
        lines = [l for l in err.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, f"expected exactly one banner line, got {lines}")
        self.assertTrue(lines[0].startswith("BROWSER-GRANT: "), lines[0])


class HeldRunClassification(StateDir):
    def test_a_held_run_that_gets_the_token_is_BROWSER_FAULT(self):
        err = io.StringIO()
        code = bg.run_locked("s", ["sh", "-c",
                                   "echo 'Failed to read: Resource temporarily unavailable "
                                   "(os error 11)' >&2; exit 1"],
                             label="", granted_by="", wait=0, state=self.state, out=err)
        self.assertEqual(code, bg.EXIT_CODE[bg.BROWSER_FAULT])
        self.assertIn("BROWSER-GRANT: BROWSER-FAULT", err.getvalue())

    def test_a_held_run_that_fails_for_real_reasons_is_still_FAIL(self):
        """A gate that excuses every failure as contention is the defect, not the fix."""
        err = io.StringIO()
        code = bg.run_locked("s", ["sh", "-c", "echo 'heading reads Deutsch' >&2; exit 1"],
                             label="", granted_by="", wait=0, state=self.state, out=err)
        self.assertEqual(code, bg.EXIT_CODE[bg.FAIL])
        self.assertIn("BROWSER-GRANT: FAIL", err.getvalue())

    def test_a_pass_releases_the_lock_even_if_the_command_raises(self):
        lock = bg.SessionLock("s", self.state)
        self.assertTrue(lock.acquire())
        lock.release()
        try:
            bg.run_locked("s", [sys.executable, "-c", "raise SystemExit(3)"], label="",
                          granted_by="", wait=0, state=self.state, out=io.StringIO())
        except Exception:  # pragma: no cover - subprocess.run does not raise on non-zero
            pass
        self.assertEqual(bg.session_state("s", self.state)["state"], "FREE",
                         "the lock leaked, so one failure wedges the session for every later run")


# --------------------------------------------------------- 3. three states, not two


class StaleIsItsOwnState(StateDir):
    def test_a_record_whose_holder_is_gone_is_STALE_not_FREE(self):
        holder = spawn_holder(self.state, "s")
        self.addCleanup(holder.kill)
        row = bg.session_state("s", self.state)
        self.assertEqual(row["state"], "HELD")
        holder.kill()
        holder.wait(timeout=10)

        row = bg.session_state("s", self.state)
        self.assertEqual(row["state"], "STALE",
                         "a dead holder's surviving record read as FREE, so a wedged session "
                         "becomes an invisible one")
        self.assertFalse(row["pid_alive"], "the recorded pid must read as dead")
        self.assertEqual(row["label"], "press #7",
                         "the stale record must still say whose it was, or nobody can act on it")

    def test_STALE_is_reported_and_not_silently_deleted(self):
        holder = spawn_holder(self.state, "s")
        self.addCleanup(holder.kill)
        holder.kill()
        holder.wait(timeout=10)
        record = bg.record_path("s", self.state)
        self.assertTrue(record.exists())
        bg.session_state("s", self.state)  # reading must not clean up
        self.assertTrue(record.exists(),
                        "reading the state deleted the evidence. A wedge that cleans itself up "
                        "whenever anybody looks is a wedge nobody investigates.")

    def test_a_never_used_session_is_FREE_and_says_so(self):
        row = bg.session_state("nothing-here", self.state)
        self.assertEqual(row["state"], "FREE")
        self.assertIsNone(row["pid"])
        self.assertIsNone(row["pid_alive"], "no pid is not a dead pid; it must not read as one")


class UngrantedIsReportedNotEnforced(StateDir):
    def test_a_held_session_with_no_grant_is_flagged_UNGRANTED(self):
        lock = bg.SessionLock("s", self.state)
        lock.acquire(label="a worker that did not ask", granted_by="")
        self.addCleanup(lock.release)
        self.assertTrue(bg.session_state("s", self.state)["ungranted"],
                        "a run nobody authorised must be visible to the coordinator")

    def test_a_granted_session_is_not_flagged(self):
        lock = bg.SessionLock("s", self.state)
        lock.acquire(label="press #7", granted_by="coordinator")
        self.addCleanup(lock.release)
        self.assertFalse(bg.session_state("s", self.state)["ungranted"])


# ----------------------------------------------------------------- 4. the override


class TheOverrideIsLoud(StateDir):
    def test_it_refuses_a_live_holder_without_force_live(self):
        """Pulling the daemon out from under a worker mid-walk is worse than the wedge."""
        lock = bg.SessionLock("s", self.state)
        lock.acquire(label="press #7")
        self.addCleanup(lock.release)
        err = io.StringIO()
        with redirect_stderr(err):
            code = bg.cmd_force_release(self._args(session="s", reason="wedged",
                                                   granted_by="coordinator", force_live=False))
        self.assertEqual(code, 2)
        self.assertIn("Refusing", err.getvalue())
        self.assertEqual(bg.session_state("s", self.state)["state"], "HELD")

    def test_it_frees_a_stale_holder_and_writes_the_audit_line(self):
        holder = spawn_holder(self.state, "s")
        self.addCleanup(holder.kill)
        holder.kill()
        holder.wait(timeout=10)
        out = io.StringIO()
        with redirect_stdout(out):
            code = bg.cmd_force_release(self._args(session="s", reason="worker died at 14:02",
                                                   granted_by="coordinator", force_live=False))
        self.assertEqual(code, 0)
        self.assertIn("OVERRIDE", out.getvalue())
        self.assertEqual(bg.session_state("s", self.state)["state"], "FREE")
        log = (self.state / "overrides.log").read_text().strip().splitlines()
        self.assertEqual(len(log), 1)
        entry = json.loads(log[0])
        self.assertEqual(entry["reason"], "worker died at 14:02", "an override with no recorded "
                                                                 "reason cannot be reviewed later")
        self.assertEqual(entry["was"], "STALE")

    def test_releasing_something_already_free_is_SKIPPED_not_success(self):
        """'I released it' and 'there was nothing there' are different facts."""
        err = io.StringIO()
        with redirect_stderr(err):
            code = bg.cmd_force_release(self._args(session="never-used", reason="x",
                                                   granted_by="", force_live=False))
        self.assertEqual(code, bg.EXIT_CODE[bg.SKIPPED])
        self.assertIn("already FREE", err.getvalue())


# ------------------------------------------------------------------- 5. the CLI


class TheCommandLine(StateDir):
    def _cli(self, *argv, stdin: str = ""):
        return subprocess.run(
            [sys.executable, str(SOURCE), "--state", str(self.state), *argv],
            input=stdin, capture_output=True, text=True)

    def test_classify_exits_75_on_the_verbatim_failure_when_contended(self):
        p = self._cli("classify", "--exit", "1", "--contended", stdin=TICKET_FAILURE)
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertIn("BROWSER-GRANT: BUSY", p.stderr)

    def test_classify_exits_76_when_held(self):
        p = self._cli("classify", "--exit", "1", "--held", stdin=TICKET_FAILURE)
        self.assertEqual(p.returncode, 76, p.stdout + p.stderr)
        self.assertIn("BROWSER-GRANT: BROWSER-FAULT", p.stderr)

    def test_classify_exits_1_on_a_bare_exit_status_1(self):
        p = self._cli("classify", "--exit", "1", "--contended",
                      stdin="agent-browser click @e2 failed: exit status 1")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_classify_without_an_exit_status_exits_78_rather_than_guessing(self):
        p = self._cli("classify", "--contended", stdin=TICKET_FAILURE)
        self.assertEqual(p.returncode, bg.EXIT_CODE[bg.UNDECIDED], p.stdout + p.stderr)

    def test_classify_json_names_the_verdict_and_its_exit_code(self):
        p = self._cli("classify", "--exit", "1", "--contended", "--json", stdin=TICKET_FAILURE)
        payload = json.loads(p.stdout)
        self.assertEqual(payload["verdict"], "BUSY")
        self.assertEqual(payload["exit_code"], 75)

    def test_the_separator_does_not_reach_the_command_as_a_program_name(self):
        """The documented spelling is `run --session NAME -- <command>`, and `argparse.REMAINDER`
        hands the `--` straight through. Every other test here calls `run_locked()` directly and so
        never touches the parser — which is exactly how the tool shipped a version that tried to
        execute a program named `--`. This one goes through the real CLI."""
        sentinel = self.state / "ran"
        p = self._cli("run", "--session", "cli-gp", "--", "sh", "-c", f"touch {sentinel}")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(sentinel.exists(),
                        "the command did not run: `--` was executed as the program name")

    def test_a_contended_run_through_the_real_CLI_refuses_with_75(self):
        held = bg.SessionLock("cli-gp", self.state)
        self.assertTrue(held.acquire(label="press #7"))
        self.addCleanup(held.release)
        sentinel = self.state / "should-not-exist"
        p = self._cli("run", "--session", "cli-gp", "--", "sh", "-c", f"touch {sentinel}")
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertIn("BROWSER-GRANT: BUSY", p.stderr)
        self.assertFalse(sentinel.exists())

    def test_status_json_lists_the_sessions_and_says_what_it_could_not_read(self):
        p = self._cli("status", "--json")
        payload = json.loads(p.stdout)
        self.assertIn("sessions", payload)
        self.assertIn("agent_browser_sessions", payload,
                      "the key must exist even when it is empty, so 'nothing to report' and "
                      "'could not look' are different facts")

    def test_status_never_claims_all_clear_when_it_could_not_look(self):
        """Same rule as `demo_gate`: a check that saw nothing must say it saw nothing."""
        original = bg.shutil.which
        bg.shutil.which = lambda name: None
        self.addCleanup(setattr, bg.shutil, "which", original)
        buf = io.StringIO()
        with redirect_stdout(buf):
            bg.cmd_status(self._args())
        self.assertIn("not on PATH", buf.getvalue())

    def test_status_names_a_stale_record_in_words(self):
        holder = spawn_holder(self.state, "s")
        self.addCleanup(holder.kill)
        holder.kill()
        holder.wait(timeout=10)
        buf = io.StringIO()
        with redirect_stdout(buf):
            bg.cmd_status(self._args())
        self.assertIn("STALE", buf.getvalue())
        self.assertIn("not cleaned", buf.getvalue())

    def test_status_with_no_sessions_does_not_say_the_workspace_is_idle(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            bg.cmd_status(self._args())
        self.assertIn("not the same as", buf.getvalue(),
                      "reporting 'no sessions' as though the workspace were quiet is the "
                      "'green because I ran nothing' failure in a new place")


# ----------------------------------------------------------------- 6. mutations


def mutate(old: str, new: str, tmp: Path, name: str):
    """A mutated copy of the module, with FACTORY.md's three preconditions checked.

    A mutation that did not apply is not a finding — it is the worst kind, because the guard
    then correctly passes and the pass is read as evidence. So: assert the needle is there,
    assert the text actually changed, and assert the mutant still parses. For Python, `ast.parse`
    is what `go build` is for Go: a mutant that cannot load proves nothing about the guard.
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
    path = tmp / f"browser-grant-{name}.py"
    path.write_text(mutated)
    return load(f"browser-grant-mutant-{name}", path)


class MutationsBreakTheGuard(unittest.TestCase):
    """A guard nobody has broken is a claim, not a guard.

    Each mutation changes exactly one thing, and each asserts the verdict for an input the
    guard is actually about — **not** the verbatim fault text in every case. Removing the
    `eagain` token leaves the verbatim failure alone, because that text also carries
    `os error 11` and `resource temporarily unavailable`; the input that catches it is an errno
    spelled out on its own. Asserting the verbatim verdict there would have been an assertion
    about nothing, and it passed for a while before it did not.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._state, True)
        self.state = Path(self._state)

    def test_removing_eagain_from_the_token_list_flips_that_input(self):
        mutant = mutate('    ("eagain", re.compile(r"\\beagain\\b")),\n', "", self.tmp, "no-eagain")
        self.assertEqual(bg.classify("Failed to read: EAGAIN", 1, held=False)[0], bg.BUSY,
                         "the real classifier must recognise the errno spelled out")
        self.assertEqual(mutant.classify("Failed to read: EAGAIN", 1, held=False)[0], bg.FAIL,
                         "with EAGAIN gone, that input stops being contention")

    def test_a_loose_substring_matching_os_error_111(self):
        """Drop the word boundary from `os error 11` and it starts matching `os error 111`."""
        mutant = mutate('re.compile(r"os error 11\\b")', 're.compile(r"os error 1")',
                        self.tmp, "loose-errno")
        self.assertEqual(bg.classify("Failed to read: os error 111", 1, held=False)[0], bg.FAIL,
                         "the real classifier must not excuse a bound port as daemon contention")
        self.assertEqual(mutant.classify("Failed to read: os error 111", 1, held=False)[0], bg.BUSY,
                         "the mutant matches a different errno")

    def test_treating_a_bare_exit_status_1_as_BUSY_reproduces_the_original_defect(self):
        """The mutation that IS the ticket: stop consulting the text and trust the status."""
        mutant = mutate('    return FAIL, f"exit {exit_code} with no transport token',
                        '    return BUSY, f"exit {exit_code} with no transport token',
                        self.tmp, "trust-status")
        self.assertEqual(bg.classify("some assertion failed", 1, held=False)[0], bg.FAIL)
        self.assertEqual(mutant.classify("some assertion failed", 1, held=False)[0], bg.BUSY,
                         "the mutant excuses a real failure as contention, which is the exact "
                         "false positive this guard exists to prevent")

    def test_ignoring_the_lock_makes_a_sick_daemon_look_like_contention(self):
        """Drop `held` from the BROWSER-FAULT branch and the two states collapse into one."""
        mutant = mutate("    if token and held:", "    if token and not held:",
                        self.tmp, "invert-held")
        self.assertEqual(bg.classify(TICKET_FAILURE, 1, held=True)[0], bg.BROWSER_FAULT)
        self.assertEqual(mutant.classify(TICKET_FAILURE, 1, held=True)[0], bg.BUSY,
                         "the mutant reports a sick daemon as contention, sending the reader to "
                         "re-run, which is the one response that cannot fix it")

    def test_guessing_when_nobody_checked_the_lock(self):
        mutant = mutate("    if token and held is None:", "    if False:",
                        self.tmp, "no-unddecided")
        self.assertEqual(bg.classify(TICKET_FAILURE, 1, held=None)[0], bg.UNDECIDED,
                         "the real classifier must refuse to guess")
        self.assertNotEqual(mutant.classify(TICKET_FAILURE, 1, held=None)[0], bg.UNDECIDED,
                            "the mutant collapses 'nobody checked' into a verdict")

    def test_guessing_when_there_is_no_exit_status(self):
        mutant = mutate("    if exit_code is None:", "    if False:",
                        self.tmp, "no-exit-undecided")
        self.assertEqual(bg.classify(TICKET_FAILURE, None, held=False)[0], bg.UNDECIDED)
        self.assertNotEqual(mutant.classify(TICKET_FAILURE, None, held=False)[0], bg.UNDECIDED)

    def test_a_lock_that_does_not_lock(self):
        """Replace the non-blocking flock with a blocking one.

        This mutation has to be observed rather than awaited: a blocking `LOCK_EX` never
        returns while another holder lives, so calling it inline hangs the suite instead of
        failing it — which is exactly why `acquire` polls `LOCK_NB` against a deadline rather
        than blocking, and why the refusal it produces is visible immediately. The claim under
        test is "the mutant blocks, so the real one must not".
        """
        mutant = mutate("                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                        "                fcntl.flock(self.fd, fcntl.LOCK_EX)",
                        self.tmp, "blocking-flock")
        a = mutant.SessionLock("s", self.state)
        self.assertTrue(a.acquire())
        self.addCleanup(a.release)

        b = mutant.SessionLock("s", self.state)
        outcome: list[bool] = []
        probe = threading.Thread(target=lambda: outcome.append(b.acquire()), daemon=True)
        probe.start()
        probe.join(timeout=3)
        self.assertTrue(probe.is_alive(),
                        "the mutant returned instead of blocking, so the non-blocking flock in "
                        "acquire() is not what makes a refusal immediate — and a refusal that "
                        "waits is a worker that cannot answer a human")

    def test_a_classifier_that_never_reports_browser_fault(self):
        mutant = mutate('BROWSER_FAULT = "BROWSER-FAULT"', 'BROWSER_FAULT = "BUSY"',
                        self.tmp, "alias-fault")
        self.assertEqual(bg.classify(TICKET_FAILURE, 1, held=True)[0], bg.BROWSER_FAULT)
        self.assertEqual(mutant.classify(TICKET_FAILURE, 1, held=True)[0], "BUSY",
                         "aliasing the constant collapses the distinction the ticket needs")

    def test_refusing_without_running_the_command(self):
        """If refusal ran the command anyway, the sentinel would exist.

        The lock is held by a third party for **both** calls, so the two are comparable: the
        real one refuses, and the mutant — with `return EXIT_CODE[BUSY]` turned into `pass` —
        falls through and runs the command it just refused.
        """
        mutant = mutate("        return EXIT_CODE[BUSY]\n\n    try:",
                        "        pass\n\n    try:", self.tmp, "refuse-runs")
        sentinel = self.tmp / "ran"
        holder = bg.SessionLock("press-gp", self.state)
        self.assertTrue(holder.acquire(label="press #7"))
        self.addCleanup(holder.release)

        bg.run_locked("press-gp", ["sh", "-c", f"touch {sentinel}"], label="press #9",
                      granted_by="", wait=0, state=self.state, out=io.StringIO())
        self.assertFalse(sentinel.exists(),
                         "the real one refused and did not run, while a live holder held it")

        mutant.run_locked("press-gp", ["sh", "-c", f"touch {sentinel}"], label="press #9",
                          granted_by="", wait=0, state=self.state, out=io.StringIO())
        self.assertTrue(sentinel.exists(),
                        "the mutant ran a command it had just refused, which is the whole failure "
                        "this guard exists to prevent")

    def test_a_mutation_needle_that_is_not_in_the_source_is_refused_outright(self):
        """The precondition itself is a guard, and it is the one that got its author three times.

        A `str.replace` on a string that is not in the file changes nothing, the guard under test
        is never run, and the pass is read as evidence. So `mutate` raises instead, and this
        asserts it raises — with a message that names the failure mode.
        """
        with self.assertRaises(AssertionError) as caught:
            mutate("a string that is definitely not in browser-grant.py", "x",
                   self.tmp, "no-such-needle")
        self.assertIn("DID NOT APPLY", str(caught.exception))
        self.assertIn("silent no-op", str(caught.exception))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--explain":
        print(__doc__)
        sys.exit(0)
    unittest.main(verbosity=2)
