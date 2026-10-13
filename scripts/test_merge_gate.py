#!/usr/bin/env python3
"""The merge gate runs `showboat verify demo.md`, and says so when it could not.

Run: `python3 scripts/test_merge_gate.py` (no test runner, no network, no sleeps).

## What this holds, and why each one is here

1. **The demo step is in the gate.** Driven through the real `scripts/merge-gate.py` as a
   subprocess with stub `go`, `make` and `showboat` on a temp `PATH`, because the claim is about
   a sequence of commands and a sequence is observed rather than read — the same method as
   `scripts/test_fleet_watch_shared_sources.py`, and for the same reason: a Python copy of the
   loop passes while the loop stays broken.
2. **A repository with no `demo.md` says so.** `NO-DEMO` is a state, not a folded green and not
   a red. A line that is simply absent reads as a pass, which is the defect the transcript rule exists for.
3. **A `demo.md` recording zero exec entries is `NOT-VERIFIED`, not green.** This is the case an
   exit status cannot see: showboat 0.4.0 verifies a document with no code blocks and exits 0 in
   silence. Measured on 2026-10-05, and held here so it cannot be forgotten.
4. **`showboat verify` is never wrapped in `timeout`.** Asserted by reading the syntax tree of
   `fleet-watch.py`, because the recorded transcripts bind ports (`APP=29087; OPA=29187`) and a
   timeout mid-run leaves one bound — a recorded instance of which wedged for eight hours.
5. **The exit status is the evidence.** A green from `showboat verify` prints nothing, so the
   gate reads `returncode`; a stub that prints a block diff and exits 0 must be read as green,
   and one that prints nothing and exits 1 as red.
6. **The merge gate's fold and the sweep's fold differ deliberately, and only there.** The sweep
   must not report every pane unverified because `showboat` is absent; a merge must not be
   authorised on a transcript it could not check.
7. **The mutations.** Three of them, with FACTORY.md's three preconditions on each: the edit
   **applied**, the mutant **parses**, and the failure is the **stated** one.

## The stubs, and the trap they have to avoid

The brief for this ticket names a trap already paid for in this repository: the `glab` stub in
`test_cleanup_workers.py` accepted *any* flag, so a broken command line passed 27 of 27 tests. A
permissive stub is a test that cannot fail.

So `showboat_stub` here **rejects what the real CLI rejects**, verified against showboat 0.4.0 by
running it:

| invocation                        | real showboat | this stub |
|-----------------------------------|---------------|------------|
| `showboat verify demo.md`, match  | exit 0, silent | exit 0, silent |
| `showboat verify demo.md`, differ | exit 1, `block N:` + expected/actual | same shape |
| `showboat bogus …`                | exit 1, `unknown command: bogus` | same |
| `showboat verify` (no file)       | exit 1, `error: opening file` | same |
| `showboat verify missing.md`      | exit 1, `error: opening file` | same |
| `showboat extract demo.md`        | emits `showboat init` + one `showboat exec` per entry | same |

`test_the_stub_rejects_what_the_real_cli_rejects` drives that table, so a stub that has been
loosened is caught by the suite rather than by the day it matters.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FLEET_WATCH = HERE / "fleet-watch.py"
# Everything `fleet-watch.py` needs beside it in order to run. This is a *set*, and it is
# asserted to be complete, because a harness that copies the script but not its dependency
# produces a mutation that dies at import — which reads exactly like a broken mutation, since
# the observable is a non-zero exit either way. That happened: `first_useful_line.py` was added
# as a sibling import and three mutations here silently stopped testing anything. A dependency
# list that is asserted is the difference between "the guard fired" and "the guard never ran".
SWEEP_DEPENDENCIES = [FLEET_WATCH, HERE / "first_useful_line.py", HERE / "factory_config.py"]


def imported_modules(script: Path) -> set[str]:
    """Every top-level module name `script` imports, from its **syntax tree**.

    Both `import X` and `from X import Y` count, and that is not a detail: the first version of
    the guard below walked only `ast.Import`, so it found nothing to complain about while
    `fleet-watch.py` — which imports its parser with the `from` form — went unexamined. A guard
    that reads the wrong node type reports "nothing to check" as "nothing wrong", which is the
    quietest way to be wrong.

    `from __future__ import …` and relative imports (level > 0) are excluded: neither is a
    sibling module staged by this file.
    """
    tree = ast.parse(script.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def stage_sweep_dependencies(dest: Path) -> None:
    """Put everything `fleet-watch.py` needs to run into `dest`.

    Two call sites need this — a mutant merge-gate run as a subprocess, and a mutant
    `fleet-watch.py` loaded by path — and they need it for different reasons. The subprocess
    has nothing else on `sys.path`. The by-path load has this test's own `sys.path`, which
    happens to contain `HERE`, so the import *would* resolve; relying on that is how the
    dependency set silently becomes wrong later and the failure reappears somewhere else.

    Asserted rather than assumed: a dependency that does not exist is a `FileNotFoundError`
    from `shutil.copy` naming the file, not a mutant that quietly fails to import.
    """
    for dependency in SWEEP_DEPENDENCIES:
        if not dependency.exists():
            raise AssertionError(
                f"{dependency} is listed as a dependency of fleet-watch.py but does not exist. A "
                "harness that stages an incomplete set produces a mutation that dies at import, "
                "which is indistinguishable from a mutation that works.")
        beside = dest / dependency.name
        if not beside.exists():
            shutil.copy(dependency, beside)

# The sibling's fixture machinery, reused rather than re-implemented: the by-path loader and the
# worktree builder live in `test_fleet_watch.py`. A second copy of the fixture would be free to
# disagree with the first, which is the failure this family keeps hitting in every other language.
from test_fleet_watch import load, make_repo_with_a_branch  # noqa: E402

def load_module(name: str, path: Path):
    """Import a *mutated* copy of a sibling script by path, under a name of its own.

    `load()` in `test_fleet_watch.py` takes only a name, because every module it loads is the
    shipped one. A mutant needs the path as well, and it needs to be reachable under a **different**
    module name: re-executing a module under a name already in `sys.modules` returns the cached
    original, so a mutation test that loaded its mutant as `fleet-watch` would silently assert
    against the shipped code and pass for the wrong reason — which is the same class of failure as
    a mutation that did not apply.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} could not be loaded, so the mutation proves nothing")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mg = load("merge-gate")
fw = load("fleet-watch")

MERGE_GATE = HERE / "merge-gate.py"

# What a `demo.md` with one recorded exec entry looks like to `showboat extract`. Kept verbatim in
# shape from the real tool's output, because the gate reads this text and a paraphrase would be a
# fixture testing something that is not there.
EXTRACT_ONE = "showboat init demo.md d\nshowboat exec demo.md bash 'echo one'\n"
EXTRACT_NONE = "showboat init demo.md d\nshowboat note demo.md 'prose only, no code blocks'\n"


def showboat_stub(log: Path | None = None, *, verify_exit: int = 0,
                  entries: int = 1, silent_fail: bool = False) -> str:
    """A `showboat` that behaves like showboat 0.4.0 on the paths this gate uses.

    `verify_exit` is what the *real* command exits with; `entries` is how many exec entries the
    document records. The two are deliberately independent, because the whole point of assertion 3
    is that they can disagree — a document can verify 0 while recording nothing at all.

    It prints **nothing** on a green `verify`, because the real one prints nothing on success and a
    stub that printed a reassuring line would let a gate which checked for output instead of an exit
    status pass.

    It prints a `block N:` diff on a red one, for the same reason in the other direction: the real
    `showboat verify` does, and a stub that failed silently would make a gate that reads the
    *output* for a diff look correct. Measured against showboat 0.4.0 on 2026-10-05:

        $ showboat verify demo.md        # a block whose output no longer matches
        block 1:
          expected: world
          actual:   hello
        $ echo $?
        1
    """
    body = f"""#!/bin/sh
if [ "$1" = "verify" ]; then
  printf 'showboat:verify\\n' >> "{log}" 2>/dev/null || true
  if [ $# -lt 2 ]; then
    echo "error: opening file: open : no such file or directory" >&2
    exit 1
  fi
  if [ ! -f "$2" ]; then
    echo "error: opening file: open $2: no such file or directory" >&2
    exit 1
  fi
  if [ {verify_exit} -ne 0 ] && [ "{silent_fail}" != "True" ]; then
    echo "block 1:"
    echo "  expected: world"
    echo "  actual:   hello"
  fi
  exit {verify_exit}
fi
if [ "$1" = "extract" ]; then
  printf 'showboat:extract\\n' >> "{log}" 2>/dev/null || true
  echo "showboat init $2 d"
  n={entries}
  i=1
  while [ "$i" -le "$n" ]; do
    echo "showboat exec $2 bash 'echo block $i'"
    i=$((i+1))
  done
  exit 0
fi
echo "unknown command: $1" >&2
exit 1
"""
    return body


class Stubs:
    """A temp `PATH` carrying stub `go`, `make` and `showboat`, each logging its subcommand.

    Usable as a context manager, because a test that puts a stub on `PATH` and forgets to take it
    off leaves every later test reading a fake `showboat` — and a test file that passes for the
    wrong reason is worse than one that fails.
    """

    def __init__(self, tmp: Path, log: Path, **showboat_kw):
        self.log = log
        bindir = tmp / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        for program in ("go", "make"):
            stub = bindir / program
            stub.write_text(f'#!/bin/sh\nprintf "{program}:%s\\n" "$1" >> "{log}"\n')
            stub.chmod(0o755)
        sb = bindir / "showboat"
        sb.write_text(showboat_stub(log, **showboat_kw))
        sb.chmod(0o755)
        self.real_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{self.real_path}"

    def restore(self):
        os.environ["PATH"] = self.real_path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.restore()
        return False

    def calls(self) -> list[str]:
        return self.log.read_text().split() if self.log.exists() else []


def worktree(tmp: Path, name: str, *, go: bool = True, demo: bool = True,
             demo_body: str = "# a demo\n") -> str:
    """A real git worktree with a real `demo.md`, under a directory of its own.

    Real git because nothing here reads git, but a real repository means `merge-gate.py`'s
    `current_branch` has something true to report and the fixture cannot accidentally depend on
    being a directory in the repository's own tree. `name` gives each scenario its own
    `<name>/repo`, `<name>/wt`, so two scenarios in one test cannot see each other.
    """
    root = tmp / name
    root.mkdir(parents=True, exist_ok=True)
    repo, wt = root / "repo", root / "wt"
    make_repo_with_a_branch(str(repo), str(wt), gate_files=not go,
                            branch="issue-15-merge-gate")
    if go:
        # Only its presence matters: `go` is a stub on PATH, so nothing is compiled.
        (wt / "go.mod").write_text("module synthetic\n\ngo 1.22\n")
    if demo:
        (wt / "demo.md").write_text(demo_body)
    return str(wt)


def run_gate(wt: str, *args: str) -> tuple[int, str]:
    """Drive the real `scripts/merge-gate.py` as a subprocess, the way a coordinator would."""
    r = subprocess.run([sys.executable, str(MERGE_GATE), wt, *args],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def report_only(wt: str) -> str:
    """Just the report, for assertions that are about what the output *says*.

    The exit status is deliberately not returned: a green and an unverified run both print a
    report, and several assertions here are about the verdict sentence rather than the number.
    Running the gate as a subprocess rather than calling `mg.verify` in process is what makes it
    the shipped program's output, including the formatting in `report()`.
    """
    _, out = run_gate(wt)
    return out


class TheDemoStepIsInTheGate(unittest.TestCase):
    """`showboat verify demo.md` runs as part of the merge gate, not beside it in prose."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"

    def test_a_green_repository_with_a_demo_verifies_it_and_exits_zero(self):
        stubs = Stubs(self.tmp, self.log)
        self.addCleanup(stubs.restore)
        wt = worktree(self.tmp, "green")

        status, out = run_gate(wt)

        self.assertEqual(status, 0, f"a green gate must exit 0:\n{out}")
        self.assertIn("showboat:verify", stubs.calls(),
                      f"the merge gate must run `showboat verify`:\n{stubs.calls()}")
        self.assertIn("VERIFIED", out,
                      f"the report must name the demo state, not omit it:\n{out}")
        print(f"    green       -> exit {status}; {stubs.calls()}")

    def test_the_report_carries_the_exit_status_because_showboat_prints_nothing_on_success(self):
        """`showboat verify` exits 0 **in silence**. "I ran it and saw nothing" is not evidence."""
        stubs = Stubs(self.tmp, self.log)
        self.addCleanup(stubs.restore)
        wt = worktree(self.tmp, "status")

        status, out = run_gate(wt)

        self.assertEqual(status, 0, out)
        self.assertIn("exit status: 0", out,
                      f"the exit status must be printed, not merely returned — a transcript of "
                      f"this run has to carry the number and not the absence of complaints:\n{out}")
        print(f"    exit status -> printed on the green path too")

    def test_a_stale_demo_is_red_and_names_the_block(self):
        """The original defect: the transcript is stale, the code gate is green."""
        stubs = Stubs(self.tmp, self.log, verify_exit=1, entries=7)
        self.addCleanup(stubs.restore)
        wt = worktree(self.tmp, "stale")

        status, out = run_gate(wt)

        self.assertEqual(status, 1, f"a stale transcript must not merge:\n{out}")
        self.assertIn("STALE", out, f"and the state must be named:\n{out}")
        self.assertIn("exit 1", out,
                      f"the reason must carry showboat's exit status:\n{out}")
        print(f"    stale       -> exit {status}; STALE named with exit 1")

    def test_a_demo_recording_zero_exec_entries_is_not_a_pass(self):
        """THE CASE AN EXIT STATUS CANNOT SEE. Measured on showboat 0.4.0, 2026-10-05.

        A `demo.md` of prose with no code blocks: `showboat verify` exits **0**, in silence, having
        compared nothing at all. Read from the exit status alone, that is a green transcript. So
        the gate asks `showboat extract` what the document records — and zero entries is
        `NOT-VERIFIED`, exit 2, never 0.
        """
        stubs = Stubs(self.tmp, self.log, verify_exit=0, entries=0)
        self.addCleanup(stubs.restore)
        wt = worktree(self.tmp, "empty", demo_body="# a demo\n\nprose, no code blocks\n")

        status, out = run_gate(wt)

        self.assertEqual(status, 2,
                         f"a demo.md that records nothing cannot authorise a merge:\n{out}")
        self.assertIn("NOT-VERIFIED", out, f"and the state must be named:\n{out}")
        self.assertIn("0 exec entries", out,
                      f"the reason must say what the document records:\n{out}")
        self.assertNotEqual(status, 0, "exit 0 here is the exact defect this ticket is about")
        print(f"    0 entries   -> exit {status}; NOT-VERIFIED, not a pass")

    def test_the_count_is_anchored_so_prose_quoting_a_command_is_not_an_entry(self):
        """`showboat extract` also emits `showboat note`, and commentary can quote a command.

        Measured across this family on 2026-10-05: an unanchored `grep -c 'showboat exec'` counted
        **14** entries in `contracts/demo.md` where showboat records **13**, and **7**
        where it records **5** in `player-registry/demo.md`. An unanchored count
        over-reports the evidence, which is a report describing a demonstration with a block
        nobody recorded.
        """
        real = "showboat init demo.md d\nshowboat note demo.md 'run `showboat exec demo.md bash` first'\n"
        self.assertEqual(fw.recorded_exec_entries(real), 0,
                         "a command mentioned inside a note is not a recorded entry")
        self.assertEqual(fw.recorded_exec_entries(EXTRACT_ONE), 1)
        self.assertEqual(fw.recorded_exec_entries(EXTRACT_NONE), 0)
        print("    anchoring    -> a quoted command is not an entry")

    def test_showboat_missing_is_unverified_rather_than_green(self):
        """Absent tooling is not the repository's fault, and it is not a pass either.

        Driven **in process** rather than as a subprocess, because the absence being simulated is
        `shutil.which` inside `fleet-watch.py`, and a subprocess would inherit the real PATH and
        find the real `showboat`. So this patches `shutil.which` and calls `merge-gate.verify`
        directly — the same function `main()` calls, so the fold under test is the shipped one.
        """
        real_which = shutil.which
        shutil.which = lambda name: None if name == "showboat" else real_which(name)
        self.addCleanup(lambda: setattr(shutil, "which", real_which))
        wt = worktree(self.tmp, "no-tool")
        # A Makefile rather than a `go.mod`, so the code gate is `make check` — which the stub
        # answers with exit 0. A `go.mod` here would send the *real* `go` to vet an empty module,
        # which is exit 1 with `matched no packages`, and this test would then be asserting about
        # a RED code gate instead of about the demo step.
        (Path(wt) / "go.mod").unlink()
        (Path(wt) / "Makefile").write_text("check:\n\t@echo gate green\n")

        status, records = mg.verify(wt)
        demo = [r for r in records if r["step"] == "demo.md"][0]

        self.assertEqual(status, 2,
                         f"a merge must not be authorised on a transcript nobody checked: "
                         f"{records}")
        self.assertEqual(demo["state"], "NOT-VERIFIED", demo)
        self.assertIn("not on PATH", demo["detail"], demo)
        print(f"    no showboat  -> exit {status}; NOT-VERIFIED")


class ARepositoryWithNoDemoSaysSo(unittest.TestCase):
    """`NO-DEMO` is a state. An absent line is not, and reads as a pass.

    **And a state that is reported is not enough — the verdict line must not contradict it.** That
    is the assertion this class is really about, and it is here because the shipped version got it
    wrong: the step row printed `NO-DEMO` and the verdict then printed *"gate green and demo.md
    verified, over what it records"* as a constant on the `else` branch. The exit status was right
    and the last line was false, on a program written to stop a merge being called green on an
    unverified transcript.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"
        self.stubs = Stubs(self.tmp, self.log)
        self.addCleanup(self.stubs.restore)

    def test_a_repository_with_no_demo_md_reports_NO_DEMO_and_does_not_run_showboat(self):
        wt = worktree(self.tmp, "no-demo", demo=False)

        status, out = run_gate(wt)

        self.assertEqual(status, 0, f"a repository that genuinely has no demo is not a failure:\n{out}")
        self.assertIn("NO-DEMO", out,
                      f"the report must name the absence — a line that is merely missing reads as "
                      f"a pass, which is the defect this ticket is about:\n{out}")
        self.assertNotIn("showboat:verify", self.stubs.calls(),
                         f"there is no demo.md to verify, so showboat must not have run: "
                         f"{self.stubs.calls()}")
        print(f"    no demo.md  -> exit {status}; NO-DEMO named, showboat never ran")

    def verdict(self, out: str) -> str:
        """The verdict line alone — the last thing a reader reads, and where a false claim hides."""
        return next((ln for ln in out.splitlines() if "VERDICT:" in ln), "")

    def test_the_verdict_line_does_not_claim_a_transcript_was_verified_when_there_is_none(self):
        """THE ASSERTION THAT WAS MISSING. The false claim is *additive*.

        Asserting `NO-DEMO` appears is necessary and not sufficient: the step row and the verdict
        are different sentences, and only the second one is read by someone in a hurry. This is the
        shipped defect, reproduced as a check — `demo.md verified` was printed while the row above
        said `NO-DEMO`.
        """
        # `demo=False` explicitly, not left to the default. This test was first written without it,
        # on the strength of the directory being *called* `no-demo-verdict`, and it then reported a
        # VERIFIED verdict on a fixture that did have a demo.md — a fixture whose name and contents
        # disagree is a test that asserts nothing about what its name says, and it would have gone on
        # passing while describing a different repository.
        wt = worktree(self.tmp, "no-demo-verdict", demo=False)

        status, out = run_gate(wt)
        verdict = self.verdict(out)

        self.assertEqual(status, 0, f"the exit status is right and must stay right: {out}")
        self.assertIn("VERDICT: GREEN", verdict, f"precondition: it is green:\n{out}")
        self.assertNotIn("demo.md verified", verdict,
                         f"THE DEFECT. The verdict claims a transcript was verified on a "
                         f"repository that has no demo.md — the exact failure this program exists "
                         f"to stop, in its own output:\n{verdict}")
        self.assertIn("no transcript was verified", verdict.lower(),
                      f"the verdict must say plainly that nothing was verified:\n{verdict}")
        print(f"    no demo.md  -> verdict: {verdict.strip()}")

    def test_the_verdict_claims_verification_only_when_the_demo_step_says_VERIFIED(self):
        """The positive direction, so the fix cannot be "never claim anything"."""
        wt = worktree(self.tmp, "verdict-verified")

        status, out = run_gate(wt)
        verdict = self.verdict(out)

        self.assertEqual(status, 0, out)
        self.assertIn("VERIFIED", out, f"precondition: the demo step ran: {out}")
        self.assertIn("recorded exec entry", verdict,
                      f"a verified transcript must be reported as verified, with the count — "
                      f"otherwise the fix has cost the report its evidence:\n{verdict}")
        print(f"    verified    -> verdict: {verdict.strip()}")

    def test_a_green_verdict_never_contains_the_words_demo_md_verified_unless_it_was(self):
        """The general form of both of the above, as one property over every green outcome.

        `demo.md verified` is the phrase that must never appear without a `VERIFIED` step above it.
        Asserted per state rather than once per fixture, because a guard that names one input is a
        guard on that input: a state added tomorrow is the case nobody tested.
        """
        cases = {
            "VERIFIED": dict(verify_exit=0, entries=3),
            "NO-DEMO": None,                                   # no demo.md at all
            "NOT-VERIFIED-empty": dict(verify_exit=0, entries=0),
        }
        for label, stub_kw in cases.items():
            with self.subTest(state=label):
                with Stubs(self.tmp / f"b-{label}", self.tmp / f"{label}.log",
                           **(stub_kw or {})):
                    wt = worktree(self.tmp, f"matrix-{label}", demo=label != "NO-DEMO")
                    out = report_only(wt)
                verdict = self.verdict(out)
                if "VERDICT: GREEN" not in verdict:
                    self.assertNotIn("demo.md verified", verdict, verdict)
                    continue
                if label == "VERIFIED":
                    self.assertIn("demo.md verified", verdict,
                                  f"the one state that may claim it: {verdict}")
                else:
                    self.assertNotIn("demo.md verified", verdict,
                                     f"a {label} demo step must not be reported as verified:\n"
                                     f"{verdict}")
        print("    matrix      -> the phrase follows the step state, on every green outcome")

    def test_NOT_VERIFIED_cannot_reach_a_green_verdict_and_says_so(self):
        """The check the review asked for, and the answer: it cannot, and here is why.

        `NOT-VERIFIED` is in `UNVERIFIABLE`, so it takes the `elif` branch. The word "verified"
        still appears in that sentence — inside "this is not a pass and not a failure" and inside
        the step's own detail — so the assertion is on the **absence of a green verdict**, not on
        the absence of the word.
        """
        wt = worktree(self.tmp, "not-verified-verdict")
        with Stubs(self.tmp / "b-nv", self.tmp / "nv.log", verify_exit=0, entries=0):
            out = report_only(wt)

        self.assertIn("VERDICT: UNVERIFIED", out,
                      f"`NOT-VERIFIED` must not be folded into a green verdict:\n{out}")
        self.assertNotIn("VERDICT: GREEN", out,
                         f"and this is the check that shows it cannot:\n{out}")
        print("    not-verified-> UNVERIFIED verdict, never GREEN")

    def test_a_step_that_never_ran_is_reported_rather_than_omitted(self):
        """Same principle for a step the gate could not reach.

        Driven in process, because the state being exercised is a `code_gate` of `NONE` and the
        fixture has to reach it without `go` — and the *stub* `go` exits 0, so a repository with
        a `go.mod` and nothing in it reports GREEN rather than NONE. Removing the `go.mod` and
        asking `merge-gate.verify` directly is what puts this on the `NONE` path, and `verify` is
        the same function `main()` calls.
        """
        wt = worktree(self.tmp, "not-reached", demo=False)
        (Path(wt) / "go.mod").unlink()          # no go module and no Makefile -> code_gate NONE

        status, records = mg.verify(wt)
        demo = [r for r in records if r["step"] == "demo.md"][0]

        self.assertEqual(status, 2, f"a gate that could not run is not a pass: {records}")
        self.assertEqual(demo["state"], "not-run", demo)
        self.assertIn("nothing here is evidence about it", demo["detail"],
                      f"and must say it is not evidence, because a missing line reads as a "
                      f"pass: {demo}")
        out = mg.report(records, "issue-15-merge-gate", wt)
        self.assertIn("not-run", out, f"and it must survive into the printed report:\n{out}")
        print(f"    not reached -> exit {status}; reported as not-run")


class TheFoldDiffersFromTheSweepOnPurpose(unittest.TestCase):
    """One verdict, two folds, and the divergence is in one place on purpose.

    The sweep's job is to tell the coordinator where to look; the merge gate's job is to
    authorise one merge. A sweep reporting every pane unverified because `showboat` was not
    installed is a sweep nobody reads, and a merge authorised on a transcript nobody checked is
    the transcript defect. So `NOT-VERIFIED` is `GREEN`+stated in the sweep and exit 2 here.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"

    def _sweep_says(self, state: str) -> str:
        real = fw.demo_verdict
        fw.demo_verdict = lambda wt: (state, "synthetic")
        try:
            steps = fw.gate_steps(worktree(self.tmp, f"sweep-{state}"))
            return fw.gate(worktree(self.tmp, f"sweep2-{state}"))[0]
        finally:
            fw.demo_verdict = real

    def test_NOT_VERIFIED_is_green_in_the_sweep_and_exit_two_in_the_merge_gate(self):
        stubs = Stubs(self.tmp, self.log, verify_exit=0, entries=0)
        self.addCleanup(stubs.restore)
        wt = worktree(self.tmp, "divergence")

        merge_status, merge_out = run_gate(wt)
        sweep_state, sweep_why = fw.gate(wt)

        self.assertEqual(merge_status, 2,
                         f"the merge gate must refuse: {merge_out}")
        self.assertEqual(sweep_state, "GREEN",
                         f"the sweep's tolerance is deliberate and documented — if this ever "
                         f"changes, the two folds have merged and the divergence comment is a "
                         f"lie: {sweep_why}")
        self.assertIn("NOT-VERIFIED", merge_out)
        self.assertIn("NOT-VERIFIED", sweep_why,
                      f"and the sweep must still name what it did not verify, even though it "
                      f"reports the branch as MERGEABLE: {sweep_why}")
        self.assertIn("not a verified transcript", sweep_why,
                      f"in its own words, not only as a state name: {sweep_why}")
        print("    divergence  -> sweep GREEN+stated, merge gate exit 2, on one verdict")

    def test_a_stale_demo_is_red_in_both(self):
        """The one state neither fold softens, which is why it is the one that matters."""
        stubs = Stubs(self.tmp, self.log, verify_exit=1, entries=3)
        self.addCleanup(stubs.restore)
        wt = worktree(self.tmp, "both-red")

        merge_status, _ = run_gate(wt)
        sweep_state, sweep_why = fw.gate(wt)

        self.assertEqual(merge_status, 1)
        self.assertEqual(sweep_state, "RED", sweep_why)
        print("    stale       -> RED in both")


class TheStubsCanFail(unittest.TestCase):
    """A permissive stub is a test that cannot fail — the trap this repository has already paid.

    The `glab` stub in `test_cleanup_workers.py` accepted any flag, so a broken command line
    passed 27 of 27 tests. So this table asserts the stub rejects what the real CLI rejects,
    and `test_the_real_showboat_agrees` checks the table against the installed tool where it is
    available, so a divergence between the stub and reality is visible rather than assumed.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.demo = self.tmp / "demo.md"
        self.demo.write_text("# a demo\n")

    def _stub(self, *args: str) -> subprocess.CompletedProcess:
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        sb = bindir / "showboat"
        sb.write_text(showboat_stub(self.tmp / "log.log"))
        sb.chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run(["showboat", *args], cwd=self.tmp, capture_output=True,
                              text=True, env=env)

    def test_the_stub_rejects_what_the_real_cli_rejects(self):
        cases = [
            (["verify"], 1, "error: opening file"),
            (["verify", "missing.md"], 1, "error: opening file"),
            (["bogus", "demo.md"], 1, "unknown command"),
            (["verify", "demo.md"], 0, None),
        ]
        for args, want_exit, want_text in cases:
            with self.subTest(args=args):
                r = self._stub(*args)
                self.assertEqual(r.returncode, want_exit,
                                 f"`showboat {' '.join(args)}` — the stub must not be more "
                                 f"permissive than the tool: got {r.returncode}, "
                                 f"stdout={r.stdout!r} stderr={r.stderr!r}")
                if want_text:
                    self.assertIn(want_text, r.stdout + r.stderr,
                                  f"the stub's refusal must read like the real one, so a gate "
                                  f"matching on the text is matching something real")

    def test_a_green_verify_prints_nothing_just_like_the_real_one(self):
        r = self._stub("verify", "demo.md")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "",
                         "the real `showboat verify` is silent on success. A stub that printed a "
                         "reassuring line would let a gate which checked for output — instead of "
                         "an exit status — pass, which is the mistake this whole ticket is about")

    def test_the_real_showboat_agrees_with_the_table(self):
        """Cross-check the stub against the installed tool, where there is one.

        Skipped rather than failed when showboat is absent, because the gate block must run on a
        machine that has not installed it — and that machine is exactly the one this program's
        `NOT-VERIFIED` state exists for. The mutation assertions below do not need the real tool.
        """
        if shutil.which("showboat") is None:
            self.skipTest("showboat is not installed here; the stub table is asserted above")
        for args, want_exit, _ in ((["verify"], 1, None), (["verify", "missing.md"], 1, None),
                                   (["bogus", "demo.md"], 1, None)):
            with self.subTest(args=args):
                real = subprocess.run(["showboat", *args], cwd=self.tmp, capture_output=True,
                                      text=True)
                self.assertEqual(real.returncode, want_exit,
                                 f"the real showboat disagrees with the stub table on "
                                 f"`{' '.join(args)}`; update showboat_stub, do not loosen it")


class ShowboatIsNeverWrappedInATimeout(unittest.TestCase):
    """The recorded transcripts bind ports. A timeout mid-run leaves one bound.

    There is a recorded instance of that wedging for eight hours, so this is asserted from the
    **syntax tree** rather than from prose: a comment saying "no timeout" is a convention, and a
    `timeout=` keyword in the call is the thing that would actually hurt.
    """

    def _showboat_calls(self, path: Path) -> list[ast.Call]:
        """Every `subprocess.run` whose argv starts with `"showboat"`.

        Matched on the **argv list**, not on any call whose first argument is the string
        `"showboat"` — which is what a looser matcher finds, and it is worth saying why that is
        wrong rather than merely tightening it: `shutil.which("showboat")` has a bare string as
        its first argument, so a naive matcher reports it as "a call that runs showboat". The guard
        then passes on the strength of a call that cannot possibly bind a port, and the real
        `subprocess.run` — the one that would actually be wrapped — is never examined. This was
        the first version of this function, and it made the assertion below true for the wrong
        reason.
        """
        return self._showboat_calls_text(path.read_text())

    @staticmethod
    def _showboat_calls_text(src: str) -> list[ast.Call]:
        found = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            argv = node.args[0]
            if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
                continue
            head = argv.elts[0]
            if isinstance(head, ast.Constant) and head.value == "showboat":
                found.append(node)
        return found

    def test_the_syntax_tree_has_no_timeout_on_any_call_that_runs_showboat(self):
        # `fleet-watch.py` is where showboat runs: `merge-gate.py` deliberately holds no copy of
        # that step, so this is asserted on the one script that owns it, plus the observation
        # below that the gate does not reach for showboat itself.
        calls = self._showboat_calls(FLEET_WATCH)
        self.assertTrue(calls,
                        "fleet-watch.py no longer calls showboat at all — if the demo step moved, "
                        "point this at the new home rather than deleting it")
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords}
            self.assertNotIn("timeout", kwargs,
                             f"fleet-watch.py:{call.lineno} wraps `showboat` in a timeout. "
                             f"The recorded blocks bind ports (APP=29087, OPA=29187 in "
                             f"agent-hub/demo.md), so a timeout that fires mid-run "
                             f"leaves a port bound and the next run fails on the port rather "
                             f"than on the code")
        self.assertEqual(self._showboat_calls(MERGE_GATE), [],
                         "merge-gate.py must not run showboat itself. It composes the shared "
                         "sequence from fleet-watch.py so there is one demo step between the "
                         "sweep and the gate; a second copy here is the pair of implementations "
                         "that could disagree about whether a transcript was verified")
        # Two calls, not one: `verify` and `extract`. Asserting the number rather than only the
        # absence of a timeout, because a guard that grows quieter as the code grows is a guard
        # that stops covering what it was written for — and `extract` is the call that reads what
        # the document records, so it matters that it is still here and still unwrapped.
        self.assertEqual(len(calls), 2,
                         f"expected `showboat verify` and `showboat extract` in fleet-watch.py, "
                         f"found {len(calls)}: {[c.lineno for c in calls]}")
        print("    no timeout  -> asserted from the syntax tree; one script owns the demo step")

    def test_the_guard_would_notice_a_timeout_that_was_added(self):
        """Prove the assertion above can fail: add `timeout=` and require it to be caught.

        Three preconditions, from AGENTS.md: the edit **applied** (asserted here, because a
        `str.replace` on a string that is not there is a silent no-op that reads as a passing
        guard), the mutant **parses** (`ast.parse`, which is what `go build` is for Go), and the
        failure would name **this** guard.
        """
        src = FLEET_WATCH.read_text()
        needle = ('s = subprocess.run(["showboat", "verify", "demo.md"], cwd=wt, '
                  'capture_output=True, text=True)')
        self.assertEqual(src.count(needle), 1,
                         f"the mutation's needle must be present and unique for this to mean "
                         f"anything; it occurs {src.count(needle)} time(s)")
        mutated = src.replace(needle, needle[:-1] + ", timeout=60)")
        self.assertNotEqual(mutated, src, "MUTATION DID NOT APPLY")
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            self.fail(f"MUTATION DID NOT PARSE: {exc}")

        calls = self._showboat_calls_text(mutated)
        flagged = [c for c in calls if "timeout" in {kw.arg for kw in c.keywords}]
        self.assertEqual(len(flagged), 1,
                         "adding a timeout to the showboat call must be visible to the guard "
                         "above, or the guard cannot fail")

    def test_nothing_else_wraps_the_merge_gate_itself_in_a_timeout_either(self):
        """The gate runs a recorded transcript; the gate must be given room, not a deadline."""
        src = MERGE_GATE.read_text()
        self.assertNotIn("timeout=", src,
                         "scripts/merge-gate.py must not pass a timeout to anything it runs: the "
                         "demo step executes the product's own workflow, which starts servers")


# ------------------------------------------------------------------------ the mutations


class TheStagedDependencySetIsComplete(unittest.TestCase):
    """A harness that stages an incomplete dependency set tests nothing, and looks like it does.

    `fleet-watch.py` imports `first_useful_line.py` from its own directory. When the parser moved
    into a sibling module, three mutations in this file stopped exercising the code under test and
    started dying at import — and the observable was a non-zero exit either way, so the assertions
    could not tell the two apart from the outside.

    So the set is checked against the **import** rather than trusted: whatever `fleet-watch.py`
    imports from a sibling has to be staged. Add a module and this fails until
    `SWEEP_DEPENDENCIES` names it. It is the difference between a mutation that fired and a
    mutation that never ran.
    """

    def test_every_sibling_import_of_the_sweep_is_staged(self):
        sibling_imports = imported_modules(FLEET_WATCH)

        local = {p.stem for p in SWEEP_DEPENDENCIES}
        stdlib = set(sys.stdlib_module_names)
        unaccounted = sorted(sibling_imports - local - stdlib - {"__future__"})

        self.assertEqual(unaccounted, [],
                         f"fleet-watch.py imports {unaccounted}, which is neither staged in "
                         f"SWEEP_DEPENDENCIES nor stdlib. Every local import must be listed there, "
                         "or a mutation that copies the script alone dies at import instead of "
                         "testing anything.")

    def test_every_staged_dependency_actually_exists(self):
        for dependency in SWEEP_DEPENDENCIES:
            self.assertTrue(dependency.exists(),
                            f"{dependency} is staged but does not exist, so staging it raises "
                            "inside the mutation rather than naming the missing file")

    def test_the_sweep_really_does_import_the_shared_parser(self):
        """The guard above would pass vacuously if the sibling import were removed.

        Read from the **syntax tree**, so a commented-out import does not satisfy it. This test
        is also why `imported_modules` handles `from X import Y` as well as `import X`: the first
        version walked only `ast.Import`, passed, and left `fleet-watch.py` — which uses the
        `from` form — entirely unexamined.
        """
        self.assertIn("first_useful_line", imported_modules(FLEET_WATCH),
                      "fleet-watch.py must import the shared parser. If it no longer does, this "
                      "test file's copy of it would be dead code and the dedup undone.")


def mutate(old: str, new: str, tmp: Path, name: str, script: Path = MERGE_GATE):
    """A mutated copy of a script, with FACTORY.md's three preconditions checked.

    A mutation that did not apply is not a finding — it is the worst kind, because the guard then
    correctly passes and the pass is read as evidence. So: assert the needle is there and unique,
    assert the text actually changed, and assert the mutant still parses.
    """
    src = script.read_text()
    if old not in src:
        raise AssertionError(
            f"MUTATION DID NOT APPLY. The needle\n  {old!r}\nis not in {script.name}. A str.replace "
            f"on a string that is not there is a silent no-op that reads as a passing guard.")
    mutated = src.replace(old, new, 1)
    if mutated == src:
        raise AssertionError("MUTATION DID NOT APPLY. The replacement produced identical text.")
    try:
        ast.parse(mutated)
    except SyntaxError as exc:
        raise AssertionError(
            f"MUTATION DID NOT PARSE. {exc}. A mutant that cannot be imported proves nothing about "
            f"the guard it was meant to break.") from exc
    path = tmp / f"{script.stem}-{name}.py"
    path.write_text(mutated)
    return path


def gate_with(path: Path, wt: str) -> tuple[int, str]:
    """Run a *mutant* of the merge gate as a subprocess, exactly as the real one is run.

    The mutant is copied next to the **shipped** `fleet-watch.py` first, because the gate resolves
    its sequence by path relative to its own location and a mutant sitting alone in a temp
    directory fails with `FileNotFoundError` before reaching any of the code under test. That is
    not a nuisance to work around — it is the missing-dependency guard doing its job, and a
    mutation that dies of it has proved nothing at all.

    `fleet-watch.py` itself has a dependency now, which is why this stages a **set** rather than
    one file: it imports `first_useful_line.py` from its own directory, so copying it alone
    produced `ModuleNotFoundError` at import and three mutations died of it instead of exercising
    anything. The symptom was indistinguishable from a broken mutation — all three reported a
    non-zero exit — which is why the dependencies are named explicitly and asserted by
    `TheStagedDependencySetIsComplete`, rather than left to be rediscovered the next time a
    module moves.
    """
    stage_sweep_dependencies(path.parent)
    r = subprocess.run([sys.executable, str(path), wt], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


class TheMutationsBreakTheGuards(unittest.TestCase):
    """A guard nobody has broken is a claim, not a guard.

    Each mutation deletes the demo step — or folds `NOT-VERIFIED` into a pass, or reads output
    instead of an exit status — and asserts the exit status **and** the report both change. Both,
    because a gate that exits right while printing nothing useful is still the defect: the
    coordinator pastes the report into the issue.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"
        self.stubs = Stubs(self.tmp, self.log)
        self.addCleanup(self.stubs.restore)

    def test_removing_the_demo_step_makes_the_gate_green_on_a_stale_transcript(self):
        """THE MUTATION THE TICKET IS ABOUT. Delete `showboat verify demo.md` from the gate.

        Expressed as a deletion of the demo step in `gate_steps` — the sequence both the sweep and
        this gate run — rather than as a second copy of the gate that omits it. A copy would pass
        while the shipped sequence stayed broken, which is the failure mode this file's whole
        method exists to avoid.
        """
        wt = worktree(self.tmp, "mutation-stale")
        # Precondition on the *fixture*: the shipped gate must fail this repository, or the
        # mutation proves nothing. Its own stub log, so the two runs stay distinguishable.
        with Stubs(self.tmp / "b2", self.tmp / "l2.log", verify_exit=1, entries=7):
            status, _ = run_gate(wt)
        self.assertEqual(status, 1, "precondition: the shipped gate is RED on a stale transcript")

        needle = ('    demo_state, demo_why = demo_verdict(wt)\n'
                  '    steps.append(("demo.md", demo_state, demo_why))\n'
                  '    return steps\n')
        path = mutate(needle, "    return steps\n", self.tmp, "no-demo-step", script=FLEET_WATCH)
        # The mutant has to be the one the merge gate loads, which means both files agreeing.
        mutant_mg = self.tmp / "merge-gate-mutated.py"
        mutant_mg.write_text(MERGE_GATE.read_text().replace(
            'path = HERE / "fleet-watch.py"', f'path = Path("{path}")'))

        with Stubs(self.tmp / "b5", self.tmp / "l5.log", verify_exit=1, entries=7):
            status, out = gate_with(mutant_mg, wt)

        self.assertEqual(status, 0,
                         "with the demo step deleted the gate must go green on a stale "
                         f"transcript — that IS the transcript defect, and it is what these assertions "
                         f"hold:\n{out}")
        self.assertNotIn("STALE", out, f"and the report must have lost the STALE line too:\n{out}")
        print(f"    no demo step-> exit {status} on the stale fixture; the assertions above bite")

    def test_a_constant_green_verdict_is_the_defect_reproduced(self):
        """THE MUTATION FOR THE DEFECT THE REVIEW FOUND. Put the constant back.

        The shipped version printed `"VERDICT: GREEN — gate green and demo.md verified, over what
        it records"` as a literal on the `else` branch, so it claimed a verified transcript on a
        repository with no `demo.md` — one line below a step row saying `NO-DEMO`. The exit status
        was correct throughout.

        The mutant below restores exactly that, and the assertions in
        `ARepositoryWithNoDemoSaysSo` must go red on the **verdict line** — not on the step row,
        which the mutant leaves correct, and not on the exit status, which the mutant also leaves
        correct. That is the point: the defect was never in what the gate decided, only in what it
        claimed.
        """
        needle = '        lines.append(f"  VERDICT: GREEN — {demo_verdict_sentence(demo)}")'
        path = mutate(needle,
                      '        lines.append("  VERDICT: GREEN — gate green and demo.md verified, '
                      'over what it records.")',
                      self.tmp, "constant-verdict")

        wt = worktree(self.tmp, "mutation-verdict", demo=False)
        _, mutant_out = gate_with(path, wt)
        _, shipped_out = run_gate(wt)

        def verdict(out: str) -> str:
            return next((ln for ln in out.splitlines() if "VERDICT:" in ln), "")

        # The mutant is not merely different — it is wrong in the specific way reported, on a
        # fixture where the step row still says the truth.
        self.assertIn("NO-DEMO", mutant_out,
                      f"precondition: the step row is still honest in the mutant: {mutant_out}")
        self.assertIn("demo.md verified", verdict(mutant_out),
                      f"precondition: the mutant does reproduce the false claim: {mutant_out}")
        self.assertNotIn("demo.md verified", verdict(shipped_out),
                         f"and the shipped gate does not make it: {verdict(shipped_out)}")
        print("    constant    -> the false claim reproduces; the shipped verdict refuses it")

    def test_folding_NOT_VERIFIED_into_a_pass_breaks_the_divergence_assertion(self):
        """Drop `NOT-VERIFIED` from the merge gate's unverified set and the gate authorises a
        transcript it never checked."""
        path = mutate('UNVERIFIABLE = ("NONE", "REFUSED", "UNPARSEABLE", "NOT-VERIFIED")',
                      'UNVERIFIABLE = ("NONE", "REFUSED", "UNPARSEABLE")',
                      self.tmp, "fold-not-verified")
        wt = worktree(self.tmp, "mutation-notverified")
        # Precondition: the shipped gate refuses this repository. A separate stub directory, so
        # the precondition is observed with the shipped fold and the mutation with the mutant's —
        # sharing one log would make the two runs indistinguishable.
        with Stubs(self.tmp / "b3", self.tmp / "l3.log", verify_exit=0, entries=0):
            status, out = run_gate(wt)
        self.assertEqual(status, 2, f"precondition: the shipped gate refuses — {out}")

        with Stubs(self.tmp / "b4", self.tmp / "l4.log", verify_exit=0, entries=0):
            status, out = gate_with(path, wt)
        self.assertEqual(status, 0,
                         f"dropping NOT-VERIFIED from the unverified set must let a demo.md that "
                         f"records nothing authorise the merge — and that is the defect:\n{out}")
        print(f"    folded      -> exit {status} on the empty-transcript fixture")

    def test_a_failing_verify_that_prints_nothing_is_still_stale(self):
        """The mirror of the silent green, and the one a diff-reading gate gets wrong.

        `showboat verify` prints nothing on success **and** can exit non-zero with nothing on
        stdout — a block that cannot be executed, for instance. So a gate that inferred the
        verdict from the presence of a diff reads that as green. This asserts the shipped gate does
        not, and it is the precondition for the mutation below: the fixture has to be one where the
        two readings genuinely disagree.
        """
        with Stubs(self.tmp / "b6", self.tmp / "l6.log", verify_exit=1, entries=4,
                   silent_fail=True):
            state, why = fw.demo_verdict(worktree(self.tmp, "mutation-silent-fail"))
        self.assertEqual(state, "STALE",
                         f"exit 1 with no output is still a failure: {why}")
        self.assertIn("exit 1", why, f"and the reason must carry the status: {why}")

    def test_reading_output_instead_of_the_exit_status_misses_a_silent_failure(self):
        """THE MUTATION. Replace `returncode != 0` with "did it print anything".

        Under it, a `showboat verify` that exits 1 printing nothing reads as VERIFIED — which is
        the failure this whole ticket is about, arriving from the other direction. The shipped gate
        calls the same fixture STALE, so the two readings genuinely disagree and the mutation is
        about something.

        Three preconditions: the needle is present and unique (**applied**), `ast.parse` succeeds
        (**parses**), and the assertion is on the verdict for this input rather than on the shipped
        function having changed (**the stated failure**).
        """
        src = FLEET_WATCH.read_text()
        needle = "    if s.returncode != 0:\n        # The count is a nicety"
        self.assertEqual(src.count(needle), 1,
                         f"the needle must be present and unique for this to mean anything; it "
                         f"occurs {src.count(needle)} time(s)")
        mutant = src.replace(
            needle,
            '    if (s.stdout + s.stderr).strip():\n        # The count is a nicety', 1)
        self.assertNotEqual(mutant, src, "MUTATION DID NOT APPLY")
        try:
            ast.parse(mutant)
        except SyntaxError as exc:
            self.fail(f"MUTATION DID NOT PARSE: {exc}")
        path = self.tmp / "fleet-watch-reads-output.py"
        path.write_text(mutant)
        # The mutant is loaded by path from this temp directory, and it imports its parser from
        # its own directory — so the parser goes with it. This one used to resolve by accident,
        # through this test's own `sys.path`, which is exactly the kind of accident that stops
        # being an accident the next time the loader changes.
        stage_sweep_dependencies(self.tmp)

        # Both runs happen under the *same* stub, and both inside it. Computing the precondition
        # after the `with` block left the shipped gate running against a different `showboat` on
        # PATH, so the precondition passed for a reason that had nothing to do with the mutation —
        # the fixture was green rather than stale, and the mutant agreed with it. One stub, one
        # PATH, one observation.
        with Stubs(self.tmp / "b7", self.tmp / "l7.log", verify_exit=1, entries=4,
                   silent_fail=True):
            wt = worktree(self.tmp, "mutation-output-read")
            shipped_state, _ = fw.demo_verdict(wt)
            mutant_state, mutant_why = load_module(
                "fleet-watch-output-mutant", path).demo_verdict(wt)

        self.assertEqual(shipped_state, "STALE", "precondition: the shipped gate reads the status")
        self.assertEqual(mutant_state, "VERIFIED",
                         f"reading the output must excuse a silent failure — a demo.md that does "
                         f"not verify, reported green, which is precisely the transcript defect: {mutant_why}")
        print("    output read -> a silent exit 1 becomes VERIFIED; the exit status is the evidence")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--explain":
        print(__doc__)
        sys.exit(0)
    unittest.main(verbosity=2)
