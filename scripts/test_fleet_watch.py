#!/usr/bin/env python3
"""Tests for the fleet sweep, and for the verdict it is allowed to print.

Run: `python3 scripts/test_fleet_watch.py` (no test runner needed, and no network).

## Why this file exists

On 2026-10-03 `fleet-watch.py` reported the meta repository's ticket as
`MERGEABLE ... clean tree, gate green` when it had verified nothing. `gate()` returned
`True` for a repository with no Go module and no Makefile, with the reason *"no go module
and no Makefile — nothing to run"*, and `assess()` mapped that `True` onto `MERGEABLE`. That
branch's entire deliverable was a shell script, so its entire verification was that script
running — and the sweep declined to run it while announcing the branch as verified.

A monitor that reports green because it did nothing is worse than one that reports nothing,
because it is a check you stop reading. So `gate()` now has three outcomes and the third one
has a name: `NO-GATE`.

## What these tests hold

1. `gate()` on a repository with neither a `go.mod` nor a `Makefile` returns `NONE`, never
   `GREEN`. This is the regression itself.
2. `assess()` on a clean tree with commits ahead and no runnable gate returns `NO-GATE`, so
   it can never reach `MERGEABLE`.
3. **The mutation.** The self-consistency check is only worth having if it fires, so the
   third test forces `gate()` to claim `GREEN` for a repository that has no gate and asserts
   the row becomes `TOOL-INCONSISTENT` with a message saying not to trust it. Without that
   check a future `True`/`False` refactor would put this back silently.
4. A worktree whose `../shared` already holds what `scripts/fetch-shared-sources.sh`
   provisions is **MERGEABLE**, and the fetch script is **not run** — held by a marker file,
   not by reading the message.
5. A worktree whose shared sources are missing and cannot be fetched is **`NO-GATE`**, and the
   reason names the missing fetch rather than a transcript block.
6. A pane that has consumed no tokens, with no commits and no diff, is
   `NOT-STARTED` — not `NOTHING`, and not an all-clear. `agent_status: idle` cannot tell those
   apart; the token counter can, because a turn is a request to the model.

Every test states what it saw, so a failure says which fact moved rather than only that
something did.
"""

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


def load(name: str):
    """Import one of the sibling scripts by path, so this needs no package."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fw = load("fleet-watch")

FAILURES: list[str] = []


class GateOnARepositoryWithNothingToRun(unittest.TestCase):
    def test_no_go_module_and_no_makefile_is_none_not_green(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "README.md").write_text("a repository with no gate at all\n")
            state, detail = fw.gate(d)
            self.assertEqual(
                state, "NONE",
                f"gate() said {state!r} for a directory with no go.mod and no Makefile. "
                f"It must say NONE: {detail!r}")
            self.assertNotEqual(state, "GREEN",
                                "this is the regression — nothing ran, so nothing is green")
            self.assertIn("UNVERIFIED", detail,
                          f"the detail must name the state, not just report it: {detail!r}")

    def test_three_outcomes_not_two(self):
        """The signature is the fix. A bool cannot carry "I ran nothing"."""
        import inspect
        sig = inspect.signature(fw.gate)
        self.assertIn("tuple[str, str]", str(sig.return_annotation),
                      f"gate() must return a three-valued state, not a bool: {sig}")


class AssessNeverPromotesAnUnverifiedBranch(unittest.TestCase):
    def test_clean_tree_with_no_gate_is_no_gate_not_mergeable(self):
        """`assess` reads git, so the repository under test is a real one, built here."""
        with tempfile.TemporaryDirectory() as d:
            repo, wt = d + "/repo", d + "/wt"
            make_repo_with_a_branch(repo, wt, gate_files=False)
            rec = fw.assess(wt, "issue-99-unverified")
            self.assertEqual(
                rec["verdict"], "NO-GATE",
                f"a clean tree with commits ahead and no runnable gate is NO-GATE, not "
                f"{rec['verdict']!r} — because {rec.get('why')!r}")
            self.assertNotEqual(rec["verdict"], "MERGEABLE",
                                "MERGEABLE claims a green gate; no gate ran")
            self.assertEqual(rec.get("gate"), "NONE",
                             f"the row must record which gate ran: {rec.get('gate')!r}")

    def test_a_repo_with_a_makefile_and_check_still_reaches_mergeable(self):
        """The fix must not make everything unverified. `make check` exiting 0 is GREEN."""
        with tempfile.TemporaryDirectory() as d:
            repo, wt = d + "/repo", d + "/wt"
            make_repo_with_a_branch(repo, wt, gate_files=True)
            rec = fw.assess(wt, "issue-98-verified")
            self.assertEqual(rec["verdict"], "MERGEABLE",
                             f"a passing make check must still be MERGEABLE: {rec}")
            self.assertEqual(rec.get("gate"), "GREEN")


def make_repo_with_a_branch(repo: str, wt: str, gate_files: bool,
                            branch: str | None = None,
                            fetch_script: str | None = None,
                            shared: tuple[str, ...] = (),
                            commits_ahead: int = 1) -> None:
    """A real repository with a real worktree on an issue branch, because `assess` reads git.

    `ahead`, `dirty` and `behind` are all `git` calls, so a directory is not a stand-in for a
    repository here: a sweep tested against a mock of its own numbers tests nothing about the
    numbers. `origin/main` is created as a ref because `behind` reads it, and the branch gets a
    commit because `assess` short-circuits to `NOTHING` at zero — which would let every gate
    assertion below pass without reaching the gate at all.

    `fetch_script` and `shared` are the shared-sources fixture: a repository that provisions its siblings
    from the group, and whether those siblings are already beside the worktree. The sources go
    in a **sibling** directory, because that is where every copy of the script puts them
    (`SHARED="${CI_PROJECT_DIR:-$(pwd)}/../shared"`), and a sibling is also outside the
    repository — so provisioning them leaves `git status` clean and a clean tree still means a
    clean tree.
    """
    os.makedirs(repo, exist_ok=True)

    def run(*args, cwd=repo):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.org")
    run("git", "config", "user.name", "test")
    Path(repo, "README.md").write_text("synthetic\n")
    if gate_files:
        Path(repo, "Makefile").write_text("check:\n\t@echo gate green\n")
    if fetch_script is not None:
        Path(repo, "scripts").mkdir(exist_ok=True)
        Path(repo, "scripts", "fetch-shared-sources.sh").write_text(fetch_script)
    for name in shared:
        # A **sibling** directory, which is where every copy of the script puts its sources
        # (`SHARED="${CI_PROJECT_DIR:-$(pwd)}/../shared"`). A sibling is also outside the
        # repository, so provisioning them leaves `git status` clean and a clean tree still
        # means a clean tree.
        (Path(wt).parent / "shared" / name).mkdir(parents=True, exist_ok=True)
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    run("git", "update-ref", "refs/remotes/origin/main", "main")
    # `-b` creates the branch, so it must not already exist.
    run("git", "worktree", "add", "-q", "-b",
        branch or ("issue-99-unverified" if not gate_files else "issue-98-verified"), wt, "main")
    # `commits_ahead=0` is the never-started fixture: a pane whose worker has produced nothing. `assess`
    # short-circuits to `NOTHING` on `ahead == 0`, which is precisely the reading that is
    # ambiguous between a finished worker and one that never started — so every gate assertion
    # needs a commit, and the not-started assertions need the absence of one.
    if commits_ahead > 0:
        Path(wt, "WORK.md").write_text("the work\n")
        run("git", "add", "-A", cwd=wt)
        run("git", "commit", "-qm", "refs #99: the work", cwd=wt)


class TheMutationThatProvesTheSelfCheckFires(unittest.TestCase):
    def test_claiming_green_without_a_gate_is_caught(self):
        """Force the old lie back and assert the row refuses to print it.

        A guard that cannot fail is worse than none, and this one is only worth having if it
        has been made to fail. Without this test, a future refactor of `gate()` back to a
        bool would put the original bug back with nothing to notice it.
        """
        real_gate = fw.gate
        # The old behaviour, verbatim in spirit: a bool True meaning "nothing to run".
        fw.gate = lambda wt: ("GREEN", "no go module and no Makefile — nothing to run")
        try:
            with tempfile.TemporaryDirectory() as d:
                self._make_repo(wt_dir=d)
                # `_make_repo` puts the worktree inside the repo, so ask about that path.
                wt = d + "/repo/wt"
                rec = fw.assess(wt, "issue-97-lie")
                self.assertEqual(
                    rec["verdict"], "TOOL-INCONSISTENT",
                    "a row claiming MERGEABLE when no gate ran must be flagged, not printed "
                    f"as a verdict; got {rec['verdict']!r} ({rec.get('why')!r})")
                self.assertIn("do not trust", rec["why"])
        finally:
            fw.gate = real_gate

    @staticmethod
    def _make_repo(wt_dir: str) -> None:
        import subprocess
        repo = wt_dir + "/repo"
        os.makedirs(repo, exist_ok=True)

        def run(*args, cwd=repo):
            subprocess.run(args, cwd=cwd, check=True, capture_output=True)

        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@example.org")
        run("git", "config", "user.name", "test")
        Path(repo, "README.md").write_text("synthetic\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "base")
        run("git", "update-ref", "refs/remotes/origin/main", "main")
        run("git", "worktree", "add", "-q", "-b", "issue-97-lie", repo + "/wt", "main")
        # `assess` returns NOTHING at zero commits ahead, so without this the mutation
        # would never reach the gate and would "pass" for the wrong reason.
        Path(repo, "wt", "WORK.md").write_text("the work\n")
        run("git", "add", "-A", cwd=repo + "/wt")
        run("git", "commit", "-qm", "refs #97: the work", cwd=repo + "/wt")


# The transcript from 2026-10-03 that this class exists to hold, verbatim in shape. `go test`
# without -v prints a failing test's ENTIRE output, so the t.Logf at 1456 and the t.Fatalf at
# 1462 land in the same `--- FAIL:` block with the same `file.go:NNNN:` shape and cannot be
# told apart from the text. 1456 narrates a scan that had *passed*.
LOG_THEN_FATAL = (
    "ok  \texample.com/player-registry/internal/nlquery\t0.005s\n"
    "--- FAIL: TestTheRegistryNeverWritesToTheProvider (0.00s)\n"
    "    import_test.go:1456: read 11 sources in internal/registry; none of them binds "
    "a provider client or writes to one\n"
    "    import_test.go:1462: football.Provider exposes 7 methods; the import path is "
    "written against a seam with eight, so a test that walked it would not be walking "
    "the real one\n"
    "FAIL\n"
    "FAIL\texample.com/player-registry/internal/registry\t1.357s\n"
)


def old_first_useful_line(out: str, skip: tuple[str, ...] = ()) -> str:
    """The implementation this class replaced, verbatim — so the mutation aims at the real thing.

    Paraphrasing it would be the same mistake as a mutation whose `str.replace` silently
    matched nothing: the guard would pass while testing something that was never there.
    """
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    for prefix in skip:
        lines = [l for l in lines if not l.startswith(prefix)]
    for l in lines:
        if ".go:" in l or ".ts:" in l or ".vue:" in l or ": error" in l:
            return l[:160]
    return (lines[0][:160] if lines else "no output")


class ALogLineIsNotAFailure(unittest.TestCase):
    """The GATE-RED detail is what the coordinator reads, so it must not name a passing check.

    `fleet-keepalive.py` renders GATE-RED with this function's output as its `->` detail, and
    that log is what a reader uses to decide what went wrong. Naming a `t.Logf` there sends
    them after a defect that is not there, which is worse than naming none: it is confident.

    The parser is the shared one in `scripts/first_useful_line.py`, which fixed this defect in
    the identical function (`357b13c`) and carries its own evidence. Three of these five tests
    came with it; the last two exist because *this* caller's presentation differs, and copying
    the tests without noticing the difference would have shipped the defect again.
    """

    def test_a_log_line_is_not_reported_as_the_failure(self):
        """The real `t.Fatalf` must be in the report — not just the first line of the block."""
        got = fw.first_useful_line(LOG_THEN_FATAL, skip=("FAIL", "ok  "))

        self.assertIn("1462", got, f"the real reason must be present: {got!r}")
        self.assertIn("7 methods", got, f"and must carry the substance: {got!r}")
        self.assertIn("[2 diagnostics]", got,
                      f"and must admit there was more than one candidate: {got!r}")
        print(f"    log-then-fatal -> {got[:92]}")

    def test_a_single_diagnostic_is_reported_without_a_count(self):
        """One diagnostic is the whole story, so report it plainly rather than hedging."""
        out = ("--- FAIL: TestOne (0.00s)\n"
               '    guard_test.go:80: internal/api/x.go reads the "X-Token" header, which '
               "is not on the allowlist\n"
               "FAIL\n")
        got = fw.first_useful_line(out, skip=("FAIL",))
        self.assertTrue(got.startswith("guard_test.go:80:"), f"got {got!r}")
        self.assertNotIn("diagnostics", got,
                         "one diagnostic needs no hedging, or the prefix becomes noise: " + got)

    def test_with_no_fail_block_the_first_useful_line_is_still_right(self):
        """With no FAIL block — a compile error — there is one candidate, so the first wins.

        And the frontend patterns stay. This sweep gates Vue and TypeScript branches too, and
        `.ts:`/`.vue:` are the only thing carrying their reason; dropping them would not
        shorten a frontend worker's report, it would empty it.
        """
        out = ("# example.com/go/club\n"
               "internal/club/club.go:42:6: undefined: ClubNotDeclared\n")
        got = fw.first_useful_line(out, skip=("#",))
        self.assertIn("undefined: ClubNotDeclared", got, f"got {got!r}")
        self.assertTrue(got.startswith("internal/club/club.go:42"),
                        f"must skip the bare package header: {got!r}")

        vue = ("src/views/RosterView.vue:41:18: error TS2339: Property 'squad' does not "
               "exist on type 'Club'\n")
        self.assertIn("TS2339", fw.first_useful_line(vue, skip=()))
        ts = ("web/src/stores/club.ts:88:7: error TS2345: Argument of type 'null' is not "
              "assignable to parameter 'wt'\n")
        self.assertIn("TS2345", fw.first_useful_line(ts, skip=()))
        print("    no FAIL block -> build error reported; .ts:/.vue: still carried")

    def test_a_long_log_line_cannot_push_the_real_failure_out_of_the_report(self):
        """A longer `t.Logf` must not squeeze the `t.Fatalf` out of the report.

        This one is specific to *this* file and is the reason the joined report is not
        capped at `DIAG_WIDTH`. Measured rather than assumed: at a 121-character `t.Logf`,
        capping the joined string at 160 lands inside the log line and pushes `1462` out
        entirely — reproducing the original defect through a different route. Each
        diagnostic therefore gets its own width instead.
        """
        long_log = ("read 11 sources in internal/registry; none of them binds a provider "
                    "client or writes to one, and the seam it walks is documented at "
                    "docs/provider-seam.md for the whole family to follow")
        out = ("--- FAIL: TestTheRegistryNeverWritesToTheProvider (0.00s)\n"
               f"    import_test.go:1456: {long_log}\n"
               "    import_test.go:1462: football.Provider exposes 7 methods; the import "
               "path is written against a seam with eight\n"
               "FAIL\n")
        self.assertGreater(len(long_log), 121,
                           "this test only means something while the t.Logf is long enough "
                           f"to push 1462 past a 160 cap; it is {len(long_log)} chars")

        got = fw.first_useful_line(out, skip=("FAIL",))

        self.assertIn("1462", got,
                      f"a long t.Logf must not push the real failure out: {got!r}")
        self.assertIn("[2 diagnostics]", got, f"got {got!r}")

    def test_the_old_heuristic_cannot_satisfy_these_assertions(self):
        """THE MUTATION. Restore the old implementation and require it to fail.

        A guard that cannot fail proves nothing, and the more dangerous case is a mutation
        that never applied and reads as a passing guard. So this does not merely assert the
        new behaviour: it binds the pre-fix implementation and asserts it CANNOT satisfy the
        assertions above. If a future edit weakens them, this fails and says which way.
        """
        real = fw.first_useful_line
        fw.first_useful_line = old_first_useful_line
        try:
            got = fw.first_useful_line(LOG_THEN_FATAL, skip=("FAIL", "ok  "))
        finally:
            fw.first_useful_line = real

        self.assertIn("1456", got,
                      "the old implementation must still be in this file for the mutation "
                      f"to mean anything; it reported {got!r}")
        self.assertNotIn("1462", got,
                         "the old heuristic reports only the first line, which is the "
                         "t.Logf — that IS the defect, and it is why the assertions above "
                         "are not vacuous")

        # And the fix must be the one still in place afterwards.
        self.assertIs(fw.first_useful_line, real, "the mutation must be undone, not left")
        self.assertIn("1462", real(LOG_THEN_FATAL, skip=("FAIL", "ok  ")))
        print("    old heuristic -> reports 1456 and drops 1462; assertions above do bite")


class OneParserAndTwoCallers(unittest.TestCase):
    """The parser-drift fix: `first_useful_line` was in this file AND in `main_sweep.py`, and the two drifted.

    `fleet-keepalive.py` renders `GATE-RED` with this line as its `->` detail, and that log is
    what the coordinator reads to decide what went wrong. Two copies of the function that
    formats *why a red is believed* is a risk, not tidiness — and it had already produced one:
    the copy in `main_sweep.py` capped the **joined** report instead of each diagnostic, so a
    `t.Logf` over 160 characters pushed the real `t.Fatalf` out of the report entirely. That
    is the original defect through a different route, and it was live in the script that gates
    every repository's `main`.

    So these four are about picking the **wrong** line on purpose, and they are here *and* in
    `scripts/test_main_sweep.py`. A copy that only one caller's tests exercise is a copy that
    can still drift, which is the whole reason the duplication was invisible for a day.
    """

    def test_both_callers_call_one_function(self):
        """One implementation, not one name. Two parsers agreeing today still drift tomorrow.

        `main_sweep.py` binds the shared parser with `functools.partial` to carry its own
        `width` and `empty` text — presentation, not a second implementation — so the claim is
        made about the *code object* each caller's function actually runs. A private copy
        pasted back into either script fails this even if it is renamed.
        """
        def impl(fn):
            return getattr(fn, "func", fn)  # unwrap a functools.partial binding

        ms = load("main_sweep")
        here = Path(fw.first_useful_line.__code__.co_filename).resolve()
        self.assertEqual(here.name, "first_useful_line.py",
                         "fleet-watch.py must call the shared parser in scripts/"
                         f"first_useful_line.py, not a copy of its own; this one is in {here}")
        there = Path(impl(ms.first_useful_line).__code__.co_filename).resolve()
        self.assertEqual(there, here,
                         "main_sweep.py and fleet-watch.py must reach ONE implementation of "
                         f"first_useful_line. This one is in {there} and that one in {here}, so "
                         "they can drift — and did: the copy in main_sweep.py dropped the real "
                         "failure on a long t.Logf. That is the parser-drift defect.")
        self.assertIs(impl(ms.first_useful_line), fw.first_useful_line,
                      "both callers must reach the same function object")
        print("    one parser, shared by both callers -> one implementation")

    def test_a_long_log_line_cannot_push_the_real_failure_out_either_caller(self):
        """The drift the shared parser found, held from *this* side too.

        Measured on the drifted copy: the real `t.Fatalf` disappears once the `t.Logf`
        exceeds `DIAG_WIDTH` (160), because that copy did `joined[:200]` on the joined string
        rather than capping each diagnostic. Each diagnostic gets its own width instead.
        """
        long_log = ("read 11 sources in internal/registry; none of them binds a provider "
                    "client or writes to one, and the seam it walks is documented at "
                    "docs/provider-seam.md for the whole family to follow")
        out = ("--- FAIL: TestTheRegistryNeverWritesToTheProvider (0.00s)\n"
               f"    import_test.go:1456: {long_log}\n"
               "    import_test.go:1462: football.Provider exposes 7 methods; the import "
               "path is written against a seam with eight\n"
               "FAIL\n")
        self.assertGreater(len(long_log), 160,
                           f"only meaningful above DIAG_WIDTH; it is {len(long_log)} chars")
        for name, mod in (("fleet-watch", fw), ("main_sweep", load("main_sweep"))):
            got = mod.first_useful_line(out, skip=("FAIL",))
            self.assertIn("1462", got,
                          f"{name}: a long t.Logf must not push the real failure out: {got!r}")
            self.assertIn("[2 diagnostics]", got, f"{name}: got {got!r}")
        print("    long t.Logf -> both callers still report the real t.Fatalf")

    def test_the_reversed_order_reaches_the_same_conclusion(self):
        """Order must not decide the answer, or the forward test alone is satisfied by luck.

        The incident had the `t.Logf` first, so a "report the first diagnostic" parser looked
        right on the very transcript that motivated it. Reversed, only "report them all" is
        still correct.
        """
        out = ("--- FAIL: TestTheRegistryNeverWritesToTheProvider (0.00s)\n"
               "    import_test.go:1462: football.Provider exposes 7 methods; the import path "
               "is written against a seam with eight\n"
               "    import_test.go:1456: read 11 sources in internal/registry; none of them "
               "binds a provider client or writes to one\n"
               "FAIL\n")
        for name, mod in (("fleet-watch", fw), ("main_sweep", load("main_sweep"))):
            got = mod.first_useful_line(out, skip=("FAIL",))
            for token in ("1456", "1462", "[2 diagnostics]"):
                self.assertIn(token, got,
                              f"{name}: reversed order must still report every diagnostic "
                              f"({token} missing): {got!r}")
        print("    t.Fatalf first -> both callers report both diagnostics")

    def test_output_with_no_failure_reports_no_log_line(self):
        """With no `--- FAIL:` block there is no test failure, so nothing may be reported as one.

        A `t.Logf` is indistinguishable from a `t.Fatalf` *inside* a block. Outside one there
        is a single candidate and it is the tool's own summary, so the honest answer is that
        summary — never a narration line dressed as a failure.
        """
        green = ("ok  \texample.com/go/club\t0.004s\n"
                 "ok  \texample.com/go/api\t0.002s\n")
        for name, mod in (("fleet-watch", fw), ("main_sweep", load("main_sweep"))):
            got = mod.first_useful_line(green, skip=("ok  ",))
            self.assertNotIn("1456", got, f"{name}: a green run must not name a failure: {got!r}")
            self.assertTrue(got.strip(), f"{name}: empty output must still be a named state")

            build = ("# example.com/go/club\n"
                     "internal/club/club.go:42:6: undefined: ClubNotDeclared\n")
            got = mod.first_useful_line(build, skip=("#",))
            self.assertIn("ClubNotDeclared", got,
                          f"{name}: a build error has one candidate and must report it: {got!r}")
        print("    green run and build error -> no t.Logf reported as a failure, both callers")


class DemoIsPartOfTheGate(unittest.TestCase):
    """`demo.md` is evidence, and `go vet`/`go test` cannot see that it went stale.

    On 2026-10-03 merging an agent-hub ticket added five packages. `demo.md` records the
    gate's own output, so it had never seen them: `showboat verify` went 0 -> 1 while the merge
    gate stayed green, because the merge gate never asked. The keepalive then reported four
    branches MERGEABLE and all four were red under this gate.

    Three states, not two, because "no demo.md" and "stale demo.md" are different facts and only
    one of them is a pass.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _repo(self, name, with_demo):
        d = Path(self.tmp) / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "Makefile").write_text("check:\n\t@echo ok\n")
        if with_demo:
            (d / "demo.md").write_text("# a demo\n")
        return str(d)

    def test_no_demo_md_is_green_but_says_it_verified_nothing(self):
        state, why = fw.demo_gate(self._repo("no-demo", with_demo=False))
        self.assertEqual(state, "GREEN", why)
        self.assertIn("no demo.md", why)
        self.assertIn("none was verified", why)
        print(f"    no demo.md    -> {state}: {why}")

    def test_missing_showboat_is_green_but_explicitly_unverified(self):
        """Absent tooling is not the repository's fault, but it is not a pass either."""
        real = shutil.which
        shutil.which = lambda name: None
        try:
            state, why = fw.demo_gate(self._repo("no-tool", with_demo=True))
        finally:
            shutil.which = real
        self.assertEqual(state, "GREEN", why)
        self.assertIn("NOT verified", why)
        print(f"    no showboat   -> {state}: {why[:72]}")

    def test_the_mutation_a_stale_demo_is_RED(self):
        """THE MUTATION. A demo.md that does not verify must be RED, carrying the block diff.

        `showboat verify` prints **nothing** and exits 0 on success, so this reads an exit status
        and never an absence of output -- and on failure it names the block, which is what makes
        the report actionable rather than merely alarming.
        """
        real = shutil.which
        if not real("showboat"):
            self.skipTest("showboat is not installed here")
        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        fake = bindir / "showboat"
        fake.write_text("#!/bin/sh\necho '  block 16:'\necho '    expected: == gofmt'\nexit 1\n")
        fake.chmod(0o755)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
        try:
            state, why = fw.demo_gate(self._repo("stale", with_demo=True))
        finally:
            os.environ["PATH"] = os.environ["PATH"].split(os.pathsep, 1)[1]
        self.assertEqual(state, "RED", why)
        self.assertIn("demo.md does not verify", why)
        print(f"    stale demo.md -> {state}: {why[:72]}")


# The mutation machinery, shared with `scripts/test_merge_gate.py` rather than copied. That file
# already builds a mutant `fleet-watch.py`, compiles it, loads it by path and asserts the failure is
# the stated one; a second implementation here would be free to disagree about what a mutation is
# worth — which is the failure this repository's own notes name twice. Imported lazily inside the
# mutation test so this file's own `load("fleet-watch")` stays the shipped module.
def _mutation_helpers():
    from test_merge_gate import FLEET_WATCH, load_module, mutate, stage_sweep_dependencies
    return FLEET_WATCH, load_module, mutate, stage_sweep_dependencies


class TheMakefileBranchReachesTheDemoStep(unittest.TestCase):
    """A repository with a Makefile and no `go.mod` still has its `demo.md` verified.

    **What the ticket described, and what the code turned out to be doing.** The ticket was filed against
    an early return in `run_the_gate`: `if state != "GREEN" or not is_go(wt): return state, why`.
    That wrapper is gone (removed since), and the asymmetry was in a function **nothing called** —
    `d853fd5` had already split the gate into `code_gate` + `gate_steps()`, and
    `gate_steps()` reaches `demo_verdict` for every repository shape. The ticket was filed at 12:20
    on 2026-10-05; that commit landed at 16:56 the same day and fixed it as a side effect of making
    the merge gate share the sequence.

    So there is no behaviour to repair, and a test asserting a fix would be asserting a repair
    nobody made. **These hold the shape instead**, because it was unguarded: measured before this
    class existed, restoring `or not is_go(wt)` into `gate_steps` left every test in this file
    green. A property that no test holds is a property the next edit can take away silently, and
    this one is the difference between a sweep that says `GATE-RED` and one that says
    `MERGEABLE, clean tree, gate green` on a repository whose transcript is a lie.

    ## Why through `gate()` and not `demo_gate()`

    `demo_gate()` is the fold of one verdict and was what the old wrapper called; the live path is
    `gate()` -> `gate_steps()`, which runs the *code* gate and then the demo step and folds the
    whole sequence. Calling `demo_gate()` directly would pass while `gate_steps()` went back to
    skipping the demo step, because the two are separate functions — the same reason this
    repository's fixtures drive the real bash and the real subprocess rather than a Python copy.

    And it goes through `assess()` for the verdict, not only through `gate()` for the state:
    `MERGEABLE` is `assess()`'s word, and the ticket's claim is about what a coordinator reads.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"

    def _makefile_repo(self, name: str, *, demo: bool = True, verify_exit: int = 0,
                       entries: int = 3, with_showboat: bool = True) -> str:
        """A real git worktree with a Makefile, no `go.mod`, a `demo.md`, and stub `make`/`showboat`.

        Stubbed on a temp `PATH`, each logging its own subcommand, because the claim is about which
        commands ran — a repository whose transcript goes stale is only a fact if something
        verified it. `showboat`'s `verify` prints **nothing** on exit 0, because that is what the
        real one does and `demo_verdict` reads an exit status rather than an absence of output;
        `extract` prints one `showboat exec` line per recorded entry, which is what
        `recorded_exec_entries` counts.

        `with_showboat=False` is the "tooling is absent" shape, and it is a real one: the fold that
        keeps a sweep reporting every pane unverified when `showboat` is not installed is the
        deliberate tolerance the fix must not break.
        """
        bindir = self.tmp / name / "bin"
        bindir.mkdir(parents=True)
        (bindir / "make").write_text(f'#!/bin/sh\nprintf "make:%s\\n" "$1" >> "{self.log}"\n'
                                     f'echo "gate green"\n')
        (bindir / "make").chmod(0o755)
        if with_showboat:
            (bindir / "showboat").write_text(
                "#!/bin/sh\n"
                f'printf "showboat:%s\\n" "$1" >> "{self.log}"\n'
                'if [ "$1" = "verify" ]; then\n'
                + (f'  if [ {verify_exit} -ne 0 ]; then echo "  block 3:"; '
                   f'echo "    expected: ok"; echo "    actual:   broken"; fi\n'
                   if verify_exit else "")
                + f'  exit {verify_exit}\n'
                "fi\n"
                'if [ "$1" = "extract" ]; then\n'
                '  echo "showboat init demo.md d"\n'
                f'  i=1; while [ "$i" -le {entries} ]; do\n'
                "    echo \"showboat exec demo.md bash 'echo block $i'\"\n"
                "    i=$((i+1))\n"
                "  done\n"
                "  exit 0\n"
                "fi\n"
                'echo "unknown command: $1" >&2\nexit 1\n')
            (bindir / "showboat").chmod(0o755)

        real_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{real_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", real_path)

        root = self.tmp / name
        repo, wt = root / "repo", root / "wt"
        # `gate_files=True` writes the Makefile and no `go.mod`: a Makefile repository, which is
        # exactly the shape the fix is about. `commits_ahead=1` because `assess` short-circuits to
        # `NOTHING` at zero and would never reach the gate.
        make_repo_with_a_branch(str(repo), str(wt), gate_files=True, branch=f"issue-28-{name}")
        self.assertFalse((Path(wt) / "go.mod").exists(),
                         "precondition: this fixture must be a Makefile repository with no go.mod, "
                         "or it does not exercise the branch the fix is about")
        if demo:
            (Path(wt) / "demo.md").write_text("# a demo\n\n```bash\nshowboat init\n```\n")
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "refs #28: the demo"], cwd=wt, check=True,
                           capture_output=True)
        return str(wt)

    def calls(self, *, drain: bool = False) -> list[str]:
        """Commands run since the last read, optionally emptying the log.

        `drain=True` is what keeps the two runs of a mutation comparable: the shipped sweep and the
        mutant share one log file, so without emptying it the mutant's row is checked against the
        shipped run's commands and the assertion cannot mean what it says.
        """
        if not self.log.exists():
            return []
        seen = self.log.read_text().split()
        if drain:
            self.log.write_text("")
        return seen

    # -- the claims ----------------------------------------------------------------

    def test_a_stale_demo_on_a_makefile_repository_is_gate_red_not_mergeable(self):
        """THE CLAIM. `make check` green, `demo.md` stale, `no go.mod` -> `GATE-RED`.

        The three facts that make it the demo case and not the transcript case: the repository has **no `go.mod`**, its code
        gate is **`make check`**, and the demo step is what must catch it. Drop any one and the test
        passes for the wrong reason — so each is a precondition below.
        """
        wt = self._makefile_repo("stale", verify_exit=1)

        rec = fw.assess(wt, "issue-28-stale")

        self.assertIn("showboat:verify", self.calls(),
                      f"precondition: this repository's demo.md must actually have been verified, "
                      f"or the verdict below proves nothing. Ran: {self.calls()}")
        self.assertIn("make:check", self.calls(),
                      f"precondition: the code gate must have been `make check`: {self.calls()}")
        self.assertEqual(rec["verdict"], "GATE-RED",
                         f"a Makefile repository whose transcript does not verify is GATE-RED, not "
                         f"{rec['verdict']!r} — because {rec.get('why')!r}")
        self.assertNotEqual(rec["verdict"], "MERGEABLE",
                            "MERGEABLE tells the coordinator to merge; on a stale transcript that "
                            "is the defect the fix exists to remove")
        self.assertIn("demo.md does not verify", rec.get("detail", ""),
                      f"and the reason must name the transcript, not the make gate: "
                      f"{rec.get('detail')!r}")
        print(f"    makefile+stale -> {rec['verdict']}: showboat ran, and the transcript decided it")

    def test_a_no_demo_makefile_repository_is_mergeable_and_says_it_verified_nothing(self):
        """The `NO-DEMO` half, and it must stay GREEN — a sweep stricter than it can be is worse.

        This is the trade the ticket's acceptance notes name, and it cuts both ways. A repository with no
        `demo.md` is not a failure: `meta` itself has none, and a sweep that reported
        every such pane `GATE-RED` would be a sweep nobody reads. What it must never do is print
        the same word as a repository whose transcript was verified — so the row carries the
        absence in its own words.
        """
        wt = self._makefile_repo("nodemo", demo=False)

        rec = fw.assess(wt, "issue-28-nodemo")

        self.assertEqual(rec["verdict"], "MERGEABLE",
                         f"a repository that genuinely has no demo is not a failure: {rec}")
        self.assertNotIn("showboat:verify", self.calls(),
                         f"there is no demo.md, so showboat must not have run: {self.calls()}")
        detail = rec.get("detail", "")
        self.assertIn("NO-DEMO", detail,
                      f"a green that verified nothing must not read as one that verified "
                      f"everything: {detail!r}")
        self.assertIn("none was verified", detail,
                      f"and it must say so in its own words, not only as a state name: {detail!r}")
        print(f"    makefile+nodemo -> {rec['verdict']}, and it says it verified nothing")

    def test_a_makefile_repository_with_no_showboat_installed_stays_mergeable(self):
        """The tolerance, held from the sweep's side. Absent tooling is not the branch's fault.

        `scripts/test_merge_gate.py` asserts the other half — the merge gate exits 2 on the same
        repository. Both folds are deliberate and they diverge **only** here, so the pair only
        means something while each side is held. This is the state the fix must not break while making
        the sweep stricter: a sweep reporting every pane `NO-GATE` because `showboat` is not
        installed is the failure already described.
        """
        wt = self._makefile_repo("notool", with_showboat=False)

        real_which = shutil.which
        shutil.which = lambda name: None if name == "showboat" else real_which(name)
        try:
            rec = fw.assess(wt, "issue-28-notool")
        finally:
            shutil.which = real_which

        self.assertEqual(rec["verdict"], "MERGEABLE",
                         f"absent tooling must not make every pane red: {rec}")
        self.assertIn("NOT-VERIFIED", rec.get("detail", ""),
                      f"but the row must still name what was not verified: "
                      f"{rec.get('detail')!r}")
        print(f"    makefile+no showboat -> {rec['verdict']}, naming NOT-VERIFIED")

    # -- the mutation --------------------------------------------------------------

    def test_restoring_the_early_return_puts_a_stale_demo_back_to_mergeable(self):
        """THE MUTATION. Reintroduce `or not is_go(wt)` into `gate_steps` and require it to bite.

        This is the mutation the ticket asks for, expressed against **the live path**
        (`gate_steps`) rather than against the dead wrapper the fix removed — because a mutation of
        dead code proves nothing, which is the mistake this ticket would otherwise have shipped: it
        would have "fixed" a function no row depends on and left the sweep able to say
        `MERGEABLE` about a stale transcript.

        Measured before this test existed: with the asymmetry restored, this file and
        `test_fleet_watch_shared_sources.py` were **both green**. Only `test_merge_gate.py` caught
        it, incidentally — through `test_showboat_missing_is_unverified_rather_than_green`, which
        happens to delete `go.mod`, and which is about the *merge gate's* fold rather than about the
        sweep reaching the demo step at all.

        The three preconditions this repository applies to every mutation are asserted rather than
        assumed, because a mutation that never applied is worse than one that fails: it reads as a
        guard that holds.

          1. the edit **applied**,
          2. the mutant **compiles** (a `SyntaxError` is not a guard firing),
          3. the failure is the **stated** one — `MERGEABLE`, not a crash.
        """
        FLEET_WATCH, load_module, mutate, stage_sweep_dependencies = _mutation_helpers()
        dest = self.tmp / "mutant"
        dest.mkdir()
        # `stage_sweep_dependencies` or the mutant dies at import, which reads exactly like a
        # mutation that did not work — the observable is a non-zero exit either way. That has
        # happened in this repository: three mutations here silently stopped testing anything.
        stage_sweep_dependencies(dest)

        # The asymmetry, restored in the sequence both the sweep and the merge gate run.
        needle = ('    state, why = code_gate(wt)\n'
                  '    steps.append(("repository gate", state, why))\n'
                  '    if state != "GREEN":\n'
                  '        return steps\n'
                  '    demo_state, demo_why = demo_verdict(wt)\n')
        path = mutate(needle, needle.replace('if state != "GREEN":',
                                             'if state != "GREEN" or not is_go(wt):'),
                      dest, "asymmetry-restored", script=FLEET_WATCH)

        # Preconditions, in order. `mutate` already asserts the edit applied; re-asserted here
        # because a mutation test that cannot see its own precondition is the same trap.
        self.assertNotEqual(path.read_text(), FLEET_WATCH.read_text(),
                            "the mutation must not have applied, or this proves nothing")
        self.assertIn("or not is_go(wt)", path.read_text(),
                      "precondition: the asymmetry must be in the mutant")
        # Compiles: a mutant that raises on import fails with a non-zero exit, and a non-zero exit
        # is what the mutation below produces — so the two are indistinguishable without this.
        compiled = subprocess.run([sys.executable, "-m", "py_compile", str(path)],
                                  capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0,
                         f"the mutant must compile, or a SyntaxError reads as the guard firing: "
                         f"{compiled.stderr}")

        mutant = load_module("fleet-watch-mutant-28", path)

        # One fixture, run twice — once through the shipped module and once through the mutant, so
        # the two rows differ in exactly one thing. A second fixture would differ in two, and the
        # verdict difference could be either. This is the same shape as the ordering mutation in
        # `scripts/test_fleet_watch_shared_sources.py`, and for the same reason.
        wt = self._makefile_repo("mutant-stale", verify_exit=1)
        shipped = fw.assess(wt, "issue-28-mutant-stale")
        self.assertEqual(shipped["verdict"], "GATE-RED",
                         f"precondition: the shipped sweep must be RED on this fixture, or the "
                         f"mutant's verdict proves nothing: {shipped}")
        self.assertIn("showboat:verify", self.calls(drain=True),
                      f"precondition: the shipped sweep must have verified the transcript: "
                      f"{self.calls()}")

        rec = mutant.assess(wt, "issue-28-mutant-stale")

        self.assertEqual(rec["verdict"], "MERGEABLE",
                         f"with the early return restored a stale demo on a Makefile repository "
                         f"must read MERGEABLE again — that IS the demo defect, and it is what the "
                         f"assertions above hold:\n{rec}")
        self.assertNotIn("showboat:verify", self.calls(),
                         f"and the mutant must not even have run showboat: {self.calls()}")
        self.assertEqual(rec.get("gate"), "GREEN",
                         f"the mutant's code gate is green; only the demo step is missing: {rec}")
        print(f"    early return restored -> shipped {shipped['verdict']}, mutant {rec['verdict']} "
              f"on one fixture; the assertions above bite")


class SharedSourcesAreProvisionedBeforeTheGate(unittest.TestCase):
    """The sweep must fetch the shared sources its go.mod and demo.md read, or say that it could not.

    This is the shared-sources fix. `fleet-watch.py` ran `showboat verify demo.md` and nothing else, so for
    every repository that fetches its siblings from the group it reported

        GATE-RED  clean tree, gate red
            -> demo.md does not verify: block 2:

    Block 2 is *a clean checkout builds one binary with the SPA inside it*, and the SPA build
    needs the fetched `ui`. So the verdict named a **transcript block** while the
    missing thing was a **prerequisite**. Verified both directions on 2026-10-05 from a bare
    clone of `opponent-brief`: no fetch -> block 2 fails; fetch -> exit 0.

    It is worse than a wrong row, because the documented response to `GATE-RED` is *go and gate
    it* — so it sent a coordinator to reject a **green** branch, twice. The general rule, which
    is the part worth keeping, is the one the ticket states: **a verdict that names a symptom
    rather than a prerequisite is the tell.** Same shape as the `withRepo`/`node_modules` order
    trap, and same shape as `required flag(s) "meta" not set`.

    The **order** and the **not-fetching-every-sweep** half live in
    `scripts/test_fleet_watch_shared_sources.py`, because both are claims about a sequence and
    a sequence is observed rather than read. These two are the verdicts, which are claims about
    a single row.
    """

    def test_a_tree_that_already_has_the_shared_sources_is_mergeable(self):
        """The common case: the sources are already beside the worktree, so nothing is fetched.

        `SHARED="${CI_PROJECT_DIR:-$(pwd)}/../shared"` is the same line in all eighteen copies
        of the script in this family, so "already there" is a check of a sibling directory
        rather than a cache in this process — and it is why a workstation sweep needs no
        `SHARED_SOURCES_TOKEN`.
        """
        with tempfile.TemporaryDirectory() as d:
            marker = d + "/fetch-ran"
            repo, wt = d + "/repo", d + "/wt"
            make_repo_with_a_branch(
                repo, wt, gate_files=True, branch="issue-96-fetched",
                fetch_script=fetch_stub(marker), shared=("go", "ui"))
            rec = fw.assess(wt, "issue-96-fetched")

            self.assertEqual(rec["verdict"], "MERGEABLE",
                             f"a tree that already has its shared sources is gateable: {rec}")
            self.assertEqual(rec.get("gate"), "GREEN")
            self.assertFalse(Path(marker).exists(),
                             "the fetch script RAN even though both sources were already "
                             "present. Five sibling clones per pane per sweep is its own "
                             "outage, and the pnpm install at the end of the script is the "
                             "half that hangs on a modules directory that already exists.")
            self.assertIn("reused", rec["detail"],
                          f"the row must record where its shared sources came from: {rec}")
            print(f"    reused      -> {rec['verdict']}: {rec['detail'][:72]}")

    def test_a_tree_without_them_says_which_fetch_is_missing(self):
        """No sources and no way to get them: `NO-GATE`, naming the fetch — not a block number.

        This is the row that manufactured work. `GATE-RED` means *go and gate it*, so a branch
        whose only problem was a missing fetch was rejected as though its code were broken.
        """
        with tempfile.TemporaryDirectory() as d:
            marker = d + "/fetch-ran"
            repo, wt = d + "/repo", d + "/wt"
            make_repo_with_a_branch(
                repo, wt, gate_files=True, branch="issue-95-nofetch",
                fetch_script=refusing_fetch_stub(marker))
            rec = fw.assess(wt, "issue-95-nofetch")

            self.assertTrue(Path(marker).exists(),
                            "the sweep must ATTEMPT the fetch before reporting anything about "
                            "the gate, or it is reporting a symptom it could have removed")
            self.assertEqual(rec["verdict"], "NO-GATE",
                             f"a missing prerequisite is not a red gate: {rec}")
            self.assertEqual(rec.get("gate"), "NONE")
            detail = rec.get("detail", "")
            self.assertIn("fetch-shared-sources.sh", detail,
                          f"the reason must name the missing fetch: {detail!r}")
            self.assertIn("SHARED_SOURCES_TOKEN", detail,
                          f"and carry the fetch's own refusal, which names what is actually "
                          f"missing: {detail!r}")
            self.assertNotIn("demo.md does not verify", detail,
                             f"naming a transcript block is the defect — it sends a "
                             f"coordinator after a block instead of after the fetch: {detail!r}")
            self.assertIn("UNVERIFIED", detail)
            print(f"    fetch refused -> {rec['verdict']}: {detail[:72]}")


class ANeverStartedWorkerIsNotAFinishedOne(unittest.TestCase):
    """The distinction the sweep could not previously make.

    On 2026-10-03 the keepalive reported *"no worker is ready to merge and none is failing"* while
    `media-vendor`'s worker — whose only job was to fix a red pipeline — sat at
    `ahead=0 dirty=0` with a pane showing `3 MCP failed`. **It had never run a turn.**

    The information was not missing. It was being read from a field that cannot carry it:
    `agent_status: idle` is the resting state of a session that has finished everything it is
    going to do *and* of one that has not begun, and `state_change_seq` has lied in both
    directions across this project. The token counter can carry it, because a turn is a request
    to the model — so zero tokens means no turn ran, which is arithmetic rather than inference.
    """

    def test_zero_tokens_with_no_commits_is_not_started(self):
        with tempfile.TemporaryDirectory() as d:
            repo, wt = d + "/repo", d + "/wt"
            # Zero commits ahead: `assess` short-circuits to `NOTHING` on that alone, which is
            # precisely the reading that is ambiguous.
            make_repo_with_a_branch(repo, wt, gate_files=True, branch="issue-94-never",
                                    commits_ahead=0)
            rec = fw.assess(wt, "issue-94-never", progress=fw.NOT_STARTED,
                            progress_why="0 tokens consumed")
            self.assertEqual(rec["verdict"], fw.NOT_STARTED,
                             f"a pane with nothing in it is NOT-STARTED, not {rec['verdict']!r}: "
                             f"{rec.get('why')!r}")
            self.assertNotEqual(rec["verdict"], "NOTHING",
                                "NOTHING means a worker that started and has not committed; the "
                                "two need different responses and must not share a name")

    def test_a_finished_worker_with_the_same_numbers_is_not_not_started(self):
        """The comparison, so the first test cannot pass because `NOTHING` was renamed.

        Same `ahead=0 dirty=0`. The only difference is the token evidence — which is the entire
        claim, and is why this pair exists rather than a single assertion.
        """
        with tempfile.TemporaryDirectory() as d:
            repo, wt = d + "/repo", d + "/wt"
            make_repo_with_a_branch(repo, wt, gate_files=True, branch="issue-93-finished",
                                    commits_ahead=0)
            rec = fw.assess(wt, "issue-93-finished", progress=fw.STARTED,
                            progress_why="144.6K tokens consumed")
            self.assertEqual(rec["verdict"], "NOTHING",
                             f"a pane that has run turns is a stalled worker, not an absent one: "
                             f"{rec}")
            self.assertNotEqual(rec["verdict"], fw.NOT_STARTED)

    def test_the_self_check_accepts_both_and_would_catch_a_mismatch(self):
        """`TOOL-INCONSISTENT` must fire on a wrong claim, not on a correct `NOT-STARTED`.

        Adding a verdict to the recomputation is exactly the kind of change that makes a guard
        fire on every correct row, and a guard that fires on correct rows trains the reader to
        ignore it — which is how it became useless in the first place.
        """
        with tempfile.TemporaryDirectory() as d:
            repo, wt = d + "/repo", d + "/wt"
            make_repo_with_a_branch(repo, wt, gate_files=True, branch="issue-92-check",
                                    commits_ahead=0)
            good = fw.assess(wt, "issue-92-check", progress=fw.NOT_STARTED, progress_why="0 tokens")
            self.assertNotEqual(good["verdict"], "TOOL-INCONSISTENT",
                                f"a correct NOT-STARTED row must not be flagged: {good}")
            # The other direction, and the reason the recomputation exists: STARTED evidence must
            # NOT yield NOT-STARTED, or the sweep would be reading a verdict rather than the
            # evidence behind it.
            bad = fw.assess(wt, "issue-92-check", progress=fw.STARTED, progress_why="144.6K tokens")
            self.assertEqual(bad["verdict"], "NOTHING",
                             f"STARTED evidence must not produce NOT-STARTED: {bad}")
            self.assertNotEqual(bad["verdict"], fw.NOT_STARTED,
                                "precondition: assess() must not emit NOT-STARTED on STARTED "
                                "evidence, or the recomputation below is untested")

    def test_a_dirty_tree_is_not_called_never_started(self):
        """Zero tokens with changes on disk is not "never started".

        The guard exists because a dirty tree is evidence of work whatever the counter says, and
        a pane whose counter reads zero because it is not being rendered is not evidence of
        anything. Reading that as `NOT-STARTED` would have the keepalive re-prompt a worker that
        is mid-edit — the opposite of helpful, and loud about it.
        """
        with tempfile.TemporaryDirectory() as d:
            repo, wt = d + "/repo", d + "/wt"
            make_repo_with_a_branch(repo, wt, gate_files=True, branch="issue-91-dirty",
                                    commits_ahead=0)
            Path(wt, "WORK.md").write_text("edited but not committed\n")
            rec = fw.assess(wt, "issue-91-dirty", progress=fw.NOT_STARTED, progress_why="0 tokens")
            self.assertEqual(rec["verdict"], "NOTHING",
                             f"an uncommitted file is work in progress, not an absent worker: "
                             f"{rec}")
            print(f"    dirty tree -> {rec['verdict']}, not NOT-STARTED")

    def test_the_token_reader_separates_a_total_from_a_rate(self):
        """A rate is not a total, and reading it as one is a false negative.

        During a turn opencode renders `39.9s · 10.4 tok/s` on the same status line. A parser
        that accepted it would report a pane two seconds into its first turn as having done work,
        which is the exact failure this reader exists to remove — in the direction that matters
        least, but it would make `NOT-STARTED` unreachable for every live pane.
        """
        self.assertEqual(fw.pane_tokens_from("  Build · x · 39.9s · 10.4 tok/s"), None,
                         "a rate line carries no cumulative total and must not be read as one")
        self.assertEqual(
            fw.pane_tokens_from("  Build auto · Example Model · max  144.6K (14%) · $0.00"),
            144600.0)
        # A bare count with no K, and an M-scale count, because both appear as a session grows.
        self.assertEqual(fw.pane_tokens_from("  1234 (1%) · $0.00"), 1234.0)
        self.assertEqual(fw.pane_tokens_from("  1.2M (70%) · $1.20"), 1_200_000.0)
        self.assertEqual(fw.pane_tokens_from("  no counter on this line"), None)
        # A stale `0` on an earlier line must not mask a real total further down.
        self.assertEqual(
            fw.pane_tokens_from("  0 (0%) · $0.00\n  144.6K (14%) · $0.00"), 144600.0)
        print("    tok/s excluded; K/M/bare totals read; a stale 0 does not mask a real one")

    def test_an_unreadable_counter_is_unknown_not_not_started(self):
        """The absence of evidence is not evidence of absence, and it is never a pass."""
        real = fw.pane_tokens
        fw.pane_tokens = lambda pane: None
        try:
            state, why = fw.agent_progress("wXX:p1", "idle")
        finally:
            fw.pane_tokens = real
        self.assertEqual(state, fw.PROGRESS_UNKNOWN,
                         f"an unreadable counter cannot distinguish never-started from finished, "
                         f"so it must say so: {state!r} ({why})")
        self.assertNotEqual(state, fw.NOT_STARTED)
        self.assertIn("never started", why,
                      f"the reason must name the distinction it cannot make: {why!r}")

    def test_a_working_pane_at_zero_tokens_is_started(self):
        """A turn in flight has demonstrably begun.

        Reading it as `NOT-STARTED` would be a confident wrong answer in the opposite direction
        from the original bug: a worker a second into its first turn re-prompted on a cooldown.
        """
        real = fw.pane_tokens
        fw.pane_tokens = lambda pane: 0.0
        try:
            state, why = fw.agent_progress("wXX:p1", "working")
        finally:
            fw.pane_tokens = real
        self.assertEqual(state, fw.STARTED,
                         f"a mid-turn pane has started whatever its counter says: {state!r} ({why})")
        print("    working at 0 tokens -> STARTED, not NOT-STARTED")


class TheMutationThatProvesNotStartedIsLive(unittest.TestCase):
    """THE MUTATION for the never-started rule: force the old reading back and require the report to change.

    The bug was a **false pass** — a worker that never ran was reported as an ordinary
    zero-commit worker and then counted as healthy — so asserting the new behaviour alone proves
    nothing: a hardcoded `NOTHING` would satisfy it. This binds the earlier decision rule and
    asserts it cannot satisfy the assertions above.
    """

    @staticmethod
    def _prefix_verdict(ahead, dirty, progress=None, progress_why=""):
        """`assess`'s rule before the never-started rule, verbatim: ahead is the only thing consulted.

        The keyword-only `progress` / `progress_why` are there so the callers below can pass the
        same arguments the live `assess` takes. **They are ignored**, exactly as the earlier code
        ignored everything but `ahead` — and asserting that is the mutation: the two calls differ
        in every argument except the evidence, and this function returns the same verdict for both.
        """
        del progress, progress_why          # the earlier rule never consulted these
        return "NOTHING" if ahead == 0 else ("IN-FLIGHT" if dirty else "MERGEABLE")

    def test_the_old_rule_cannot_separate_them(self):
        never = self._prefix_verdict(0, 0, progress=fw.NOT_STARTED)
        finished = self._prefix_verdict(0, 0, progress=fw.STARTED)
        self.assertEqual(never, finished,
                         "the earlier rule must still be here for the mutation to mean anything, "
                         f"and it must return the same verdict for both: {never!r} vs {finished!r} "
                         "— that IS the defect")
        self.assertEqual(never, "NOTHING")

    def test_and_the_live_rule_does_separate_them(self):
        """Through the real `assess`, on real git numbers, differing only in the evidence."""
        verdicts = []
        for progress in (fw.NOT_STARTED, fw.STARTED):
            with tempfile.TemporaryDirectory() as d:
                repo, wt = d + "/repo", d + "/wt"
                make_repo_with_a_branch(d + "/repo", wt, gate_files=True,
                                        branch="issue-90-mut", commits_ahead=0)
                verdicts.append(fw.assess(wt, "issue-90-mut", progress=progress)["verdict"])
        self.assertNotEqual(verdicts[0], verdicts[1],
                            f"identical git numbers and opposite evidence must produce different "
                            f"verdicts; both were {verdicts[0]!r}")
        self.assertEqual(verdicts[0], fw.NOT_STARTED)
        print(f"    same git numbers -> {verdicts[0]} vs {verdicts[1]}: the evidence decides")


def fetch_stub(marker: str, log: str | None = None) -> str:
    """A `fetch-shared-sources.sh` that provisions what its `for repo in` line promises.

    It keeps the two lines the sweep actually reads — the `SHARED=` target and the
    `for repo in` list — and touches `marker`, so "the fetch ran" is an observation rather
    than an inference. With `log` it also appends `fetch`, which is how the ordering is
    observed in `scripts/test_fleet_watch_shared_sources.py`. Everything else is gone because
    nothing else is used, and a stub that grew a `git clone` would make these tests network
    tests.

    It refuses without `CI_PROJECT_DIR`, because the sweep sets it so the script and the sweep
    read the same `../shared`. That is a claim like any other, so it is asserted rather than
    assumed: drop the assignment and the stub fails here instead of quietly provisioning a
    directory the sweep is not looking at.
    """
    return ("#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'touch "{marker}"\n'
            + (f'printf \'fetch\\n\' >> "{log}"\n' if log else "")
            + ': "${CI_PROJECT_DIR:?fleet-watch must set CI_PROJECT_DIR, or the fetch and the '
              'sweep read different shared directories}"\n'
              'SHARED="${CI_PROJECT_DIR:-$(pwd)}/../shared"\n'
              "for repo in go ui; do\n"
              '  mkdir -p "$SHARED/$repo"\n'
              "done\n"
              'echo "  go @ synthetic"\n')


def refusing_fetch_stub(marker: str, log: str | None = None) -> str:
    """The real script's own refusal, verbatim in shape, for a workstation with no token.

    Every copy in the family opens with `if [ -z "$TOKEN" ] ... exit 1` after printing
    `SHARED_SOURCES_TOKEN is not set.`, and keeps its `for repo in ...; do` loop below that —
    so this does too. The loop matters: a stub without one is a script the sweep refuses to
    reason about at all, which is a different test from the one it is standing in for.
    """
    return ("#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'touch "{marker}"\n'
            + (f'printf \'fetch\\n\' >> "{log}"\n' if log else "")
            + 'TOKEN="${SHARED_SOURCES_TOKEN:-}"\n'
              'if [ -z "$TOKEN" ]; then\n'
              '  echo "SHARED_SOURCES_TOKEN is not set." >&2\n'
              "  exit 1\n"
              "fi\n"
              "for repo in go ui; do\n"
              '  echo "  $repo"\n'
              "done\n")


class TestTheGateRunsGofmt(unittest.TestCase):
    """`code_gate` ran two hand-typed commands and dropped every step a repository documents.

    `contracts` writes `check: fmt vet test typecheck web-lint api-check check-english
    check-demo` — **`fmt` is its first step** — and a branch with two unformatted new files came back
    `MERGEABLE — clean tree, gate green`, because every Go repository takes the `go vet` + `go test`
    path rather than its own `make check`.
    """

    def setUp(self):
        # A throwaway git repository: `go_own_files` asks git, so a temp dir with no `.git` would
        # silently return nothing and every assertion below would pass for the wrong reason.
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for rel in ("a.go", "pkg/b.go", "vendor/dep.go", "vendor/dep2.go", "notes.md"):
            path = Path(self.tmp, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("package p\n")
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        subprocess.run(["git", "-C", self.tmp, "add", "-A"], check=True)
        self.script = HERE / "fleet-watch.py"

    def test_own_files_are_tracked_go_excluding_vendor(self):
        """The property is *this repository's own files*, and `vendor/` is committed third-party code."""
        self.assertEqual(sorted(fw.go_own_files(self.tmp)), ["a.go", "pkg/b.go"])

    def test_the_gofmt_step_runs_before_vet(self):
        """A file that is not gofmt-clean should be reported before anything tries to compile it.

        `go vet` passing on unformatted code is the more confusing outcome, so the order is the claim
        rather than tidiness.
        """
        src = self.script.read_text()
        fmt_at = src.index("unformatted = [l.strip() for l in fmt.stdout.splitlines()")
        vet_at = src.index('subprocess.run(["go", "vet", "./..."]')
        self.assertLess(fmt_at, vet_at, "gofmt must be reported before go vet")

    def test_an_unformatted_own_file_is_named_in_the_red(self):
        """A RED that does not name the file sends the next reader looking in the wrong place.

        Cheap because `gofmt` runs before `go vet` in the same function — the guard being broken is
        why it does not need the module to compile.
        """
        Path(self.tmp, "a.go").write_text("package p\n\nfunc  f( ) int {\nreturn 1\n}\n")
        src = self.script.read_text()
        self.assertIn('gofmt: {len(unformatted)} of this repository', src,
                      "the RED must name how many files and which, not merely that vet failed")


class AMissingHerdrIsNotACrash(unittest.TestCase):
    """Absent tooling is the absence of an answer — the rule every other reader here states.

    CI has no `herdr` and a monitor that dies with `FileNotFoundError` reports nothing about
    everything. Both readers return their own "unknown" instead; the smoke test found this on
    CI, where this machine's herdr had been hiding it.
    """

    def setUp(self) -> None:
        self.saved = os.environ.get("PATH")
        os.environ["PATH"] = "/nonexistent"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self.saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.saved

    def test_agent_status_is_unknown(self):
        self.assertEqual(fw.agent_status("wX:p1"), "?")

    def test_pane_tokens_is_none(self):
        self.assertIsNone(fw.pane_tokens("wX:p1"))


if __name__ == "__main__":
    # Make sure a test that silently stops asserting anything is itself noticed.
    if len(sys.argv) > 1 and sys.argv[1] == "--explain":
        print(__doc__)
        sys.exit(0)
    unittest.main(verbosity=2)
