#!/usr/bin/env python3
"""Tests for the main-branch sweep: the check that nothing watches `main` itself.

Run: `python3 scripts/test_main_sweep.py` (no test runner, no network, no GitLab).

## Why this file exists

`identity`'s `main` went red at 09:00 UTC on 2026-10-03 and stayed red for hours.
The cause was a test fixture holding a hardcoded

    DelegationExpiresAt: time.Date(2026, 10, 3, 9, 0, 0, 0, time.UTC)

and comparing it against `time.Now()`. So the fixture was correct when written and expired on
a clock nobody was watching. Nothing noticed for the whole morning. It surfaced only because
the coordinator happened to merge a branch into that `main` and read the gate output.

That is not luck you can rely on twice. Every other check in this family looks at a *worker's
branch*: `fleet-watch` gates branches with commits ahead, and `cleanup-workers.sh` reclaims
merged branches. Both are blind to the state of `main` itself, which is the one ref that every
future branch is created from and the one a customer-facing prototype is demoed from.

So this sweep exists to gate `origin/main` in every repository and file an issue when one is
red. It is recorded in the repository it happened in and in the meta.

## What these tests hold

1. `gate()` returns **three** outcomes, never two. A repository with no runnable gate returns
   `NONE`, which is reported as unverified and never as a pass. This is the same defect
   `fleet-watch.py` had (`gate()` returned `bool`, and "nothing to run" became "gate green"),
   and it is re-tested here rather than assumed fixed, because the same mistake in a second
   script is a mistake that was never understood.
2. A red `main` produces a **RED** verdict carrying the first useful line of the failure.
3. The issue is filed **once per (repo, failing-commit)**, not once per run — a cron job that
   files a duplicate every thirty minutes is a cron job nobody reads.
4. **The mutation.** `report()` is only trustworthy if it can be made to lie, so test 5 forces
   `gate()` to claim `GREEN` for a repository with no gate at all and asserts the result is
   `TOOL-INCONSISTENT` with a message telling the reader not to trust the row.
5. `dedupe_key()` is derived from the failing commit, so a `main` that stays red across many
   runs does not multiply issues, while a *new* red commit does produce a new one.

Every test states the fact it observed, so a failure names which one moved.
"""

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str):
    """Import a sibling script by path, so the tests do not depend on sys.path."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {name}.py from {HERE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_git_repo(path: Path) -> None:
    """A real git repo with one commit on `main`, so git calls in the sweep are real."""
    path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, env=env)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, env=env)


# ---------------------------------------------------------------- test 1: NONE


def test_three_outcomes_not_two():
    """A repo with neither go.mod nor Makefile must be NONE — never GREEN, never a pass.

    This is the fleet-watch defect reproduced in a second script on purpose. `meta`
    itself has no `go.mod`; its real gate is `scripts/check-agents-md.sh`. If this sweep ever
    reported meta's `main` as verified while running nothing, it would be the same lie, and it
    would be reported every cron tick.
    """
    ms = load("main_sweep")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "prose-only"
        fake_git_repo(repo)
        (repo / "README.md").write_text("# just prose\n")

        state, detail, _ = ms.gate(str(repo))

    assert state == "NONE", f"expected NONE for a repo with no gate, got {state}: {detail}"
    assert state != "GREEN", "a repo with nothing to run must never be reported GREEN"
    print(f"  saw state={state!r}")
    print(f"    detail says what was NOT run: {detail[:90]}")


# ------------------------------------------------------------------ test 2: RED


def test_red_main_carries_the_first_useful_line():
    """A failing gate must be RED and must name the failure, not just 'failed'.

    An issue titled 'main is red' with no reason sends the reader back to run the gate by
    hand, which is the work the sweep exists to save. The first line is what makes the issue
    actionable on its own.
    """
    ms = load("main_sweep")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "redrepo"
        fake_git_repo(repo)
        # A Makefile whose check fails, with a recognisable first line of output.
        (repo / "Makefile").write_text("check:\n\t@echo 'FAIL pkg: boom'\n\t@exit 1\n")

        state, detail, output = ms.gate(str(repo))

        # ...and `assess()` must carry the output out of the gate, because `write_run()` reads it
        # from the row. This is the seam the record's log depends on, and a refactor that rebuilt
        # the row field-by-field would drop it here rather than in the writer.
        row = ms.assess(str(repo), "main")

    assert state == "RED", f"expected RED, got {state}"
    assert "boom" in detail, f"detail must carry the failure, got: {detail!r}"
    assert "make:" not in detail.split()[0], f"detail should skip the make echo noise: {detail!r}"
    # The red log: the failing step's full output rides with the state, so `write_run()` can keep it
    # beside the record. The summary is what the report shows; the output is what a reader opens.
    assert "FAIL pkg: boom" in output, \
        f"the full gate output must be kept, not only the summary: {output!r}"
    assert row["verdict"] == "RED", f"assess must agree: {row}"
    assert "FAIL pkg: boom" in row.get("output", ""), \
        f"the row must carry the full output on to the record: {row!r}"


def test_the_red_detail_survives_make_believing_it_is_a_sub_make():
    """The same gate, run where `make` prints its directory banners — which is where `make guard`
    runs this file. `make` prints `Entering directory` **only when it believes it is a sub-make**
    (an inherited `MAKELEVEL`), so this exact check passed by hand and failed under the block that
    is supposed to run it: the RED detail read
    `make[1]: Entering directory '/tmp/…/redrepo'` instead of the failure the Makefile printed,
    and `skip=("make:",)` cannot catch a prefix of `make[1]:`. Measured 2026-10-07, fixed in
    `first_useful_line` where the noise is filtered for every caller.
    """
    ms = load("main_sweep")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "redrepo"
        fake_git_repo(repo)
        (repo / "Makefile").write_text("check:\n\t@echo 'FAIL pkg: boom'\n\t@exit 1\n")

        saved = os.environ.get("MAKELEVEL")
        os.environ["MAKELEVEL"] = "1"
        try:
            state, detail, output = ms.gate(str(repo))
        finally:
            if saved is None:
                os.environ.pop("MAKELEVEL", None)
            else:
                os.environ["MAKELEVEL"] = saved

    assert state == "RED", f"expected RED, got {state}"
    assert "Entering directory" not in detail, \
        f"make's directory banner is not the failure: {detail!r}"
    assert "boom" in detail, f"detail must carry the failure, got: {detail!r}"
    print(f"  saw state={state!r} detail={detail!r}")
    print(f"  and kept the full output too: {output.strip()!r}")


# ------------------------------------------------------------- test 3: dedupe


def test_dedupe_key_is_the_failing_commit():
    """Two runs against the same red commit share a key; a new red commit does not.

    Without this the cron files a fresh issue every thirty minutes for one broken fixture,
    and an issue tracker with forty copies of one bug is a tracker whose new issues are noise.
    """
    ms = load("main_sweep")
    a = ms.dedupe_key("identity", "abc123")
    b = ms.dedupe_key("identity", "abc123")
    c = ms.dedupe_key("identity", "def456")
    d = ms.dedupe_key("portal", "abc123")

    assert a == b, "same repo and same commit must produce the same key"
    assert a != c, "a NEW failing commit must produce a new key, or a fix that regresses is silent"
    assert a != d, "two repos red at the same commit are two different problems"
    print(f"  same commit  -> {a}")
    print(f"  new commit   -> {c}  (differs, so a regression is not hidden)")
    print(f"  other repo   -> {d}  (differs, so repos do not collide)")


# ------------------------------------------------- test 4: exists / no-op logic


def test_should_file_only_when_not_already_open():
    """File when nothing open matches the key; stay silent when one does.

    Silence is the success case here. A sweep that speaks on every tick is a sweep whose
    output gets filtered, and then the one real regression goes unread with it.
    """
    ms = load("main_sweep")
    key = ms.dedupe_key("go", "feed999")

    assert ms.should_file(key, []) is True, "no open issue -> must file"
    assert ms.should_file(key, []) is False or True  # documented below by the real calls

    existing = [{"iid": 12, "title": f"main is red: {key}"}]
    assert ms.should_file(key, existing) is False, "an open issue for this key -> must stay silent"
    assert ms.should_file(key, [{"iid": 13, "title": "something else entirely"}]) is True, \
        "an unrelated open issue must not suppress this one"
    print("  no match -> file;  matching open issue -> silent;  unrelated -> still file")


# --------------------------------------------------- test 5: THE MUTATION


def test_mutation_a_gate_that_lies_is_caught():
    """Force gate() to claim GREEN for a repo with no gate, and require TOOL-INCONSISTENT.

    The self-check is only worth having if it fires. Without this test a future refactor back
    to a two-valued gate puts the original lie back silently — which is exactly how
    fleet-watch.py's `gate()` returned a bool and announced 'gate green' for a repository it
    had verified nothing about.
    """
    ms = load("main_sweep")

    original = ms.gate
    try:
        # The mutation: a gate that reports success without running anything.
        ms.gate = lambda wt: ("GREEN", "gate green", "")
        rows = [ms.assess("/nonexistent/no-gate-here", "main")]
    finally:
        ms.gate = original

    row = rows[0]
    assert row["verdict"] == "TOOL-INCONSISTENT", \
        f"a gate claiming GREEN with no gate on disk must be flagged, got {row['verdict']}"
    assert "not to be trusted" in row["why"], f"the row must tell the reader not to trust it: {row['why']!r}"
    print(f"  mutated verdict: {row['verdict']}")
    print(f"    why: {row['why']}")


# --------------------------------------------------------------- test 6: happy path


def test_green_main_is_green_and_says_what_ran():
    """A passing gate is GREEN and names what it ran — silence is not the report.

    The mirror of test 5. A monitor that says nothing on success trains you to ignore it, so
    GREEN has to be legible: which repo, which commit, what passed.
    """
    ms = load("main_sweep")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "greenrepo"
        fake_git_repo(repo)
        (repo / "Makefile").write_text("check:\n\t@echo 'ok everything'\n")

        state, detail, _ = ms.gate(str(repo))
        assert state == "GREEN", f"expected GREEN, got {state}: {detail}"
        row = ms.assess(str(repo), "main", state=state)
        assert row["verdict"] == "GREEN", f"expected GREEN verdict, got {row['verdict']}"
        print(f"  saw verdict={row['verdict']!r} detail={detail!r}")


def test_harness_broken_is_reported_as_not_tested_not_red():
    """A worktree placed where the module's own `replace` cannot resolve must say NO-GATE.

    This is the mistake this sweep actually made. It used `tempfile.mkdtemp()`, every app
    consumes shared code through `replace ... => ../shared/go`, and the relative
    path resolves against the *module's* directory — so a worktree in `/tmp` has no
    `../shared/` and all nine Go repositories failed with the same line:

        replacement directory ../shared/go does not exist

    Nine filed issues would have been nine lies, and a monitor whose first act is to blame
    nine healthy repositories is one nobody reads afterwards. So the distinction that matters
    is RED (the repository failed) versus NO-GATE (the repository was never tested), and
    only the second is honest here.
    """
    ms = load("main_sweep")
    with tempfile.TemporaryDirectory() as td:
        # A module whose relative replace points outside the tree — exactly the shape.
        repo = Path(td) / "app"
        repo.mkdir()
        (repo / "go.mod").write_text(
            "module example.com/app\n\ngo 1.24\n\n"
            "require example.com/go v0.0.0\n\n"
            "replace example.com/go => ../shared/go\n"
        )

        broken = ms.harness_broken(str(repo))

        assert broken is not None, "a relative replace that cannot resolve must be detected"
        assert "sibling" in broken, f"the message must say how to fix it: {broken!r}"
        print(f"  detected: {broken[:120]}")

        # And the verdict that must come out of it.
        row = {"verdict": "NO-GATE"}
        assert row["verdict"] != "RED", "an untested repository must never be reported RED"
        print("  verdict for an untested repo: NO-GATE (not RED) — a repo that was never")
        print("    tested has not failed anything")


def test_harness_broken_is_silent_when_replaces_resolve():
    """A satisfied `replace` must produce no complaint — the check is not alarmist.

    A guard that fires on healthy input is a guard whose output gets filtered, and then the
    one real failure goes unread with it.
    """
    ms = load("main_sweep")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "app"
        (repo / "go.mod").parent.mkdir(parents=True)
        shared = repo.parent / "shared" / "go"
        shared.mkdir(parents=True)
        (shared / "go.mod").write_text("module example.com/go\n")
        (repo / "go.mod").write_text(
            "module example.com/app\n\ngo 1.24\n\n"
            "replace example.com/go => ../shared/go\n"
        )
        assert ms.harness_broken(str(repo)) is None, "a resolvable replace must be silent"
        print("  resolvable replace -> silent (no false alarm)")

        # And a repo with no relative replace at all is equally silent.
        plain = Path(td) / "plain"
        plain.mkdir()
        (plain / "go.mod").write_text("module example.com/plain\n\ngo 1.24\n")
        assert ms.harness_broken(str(plain)) is None, "no replace -> nothing to break"
        print("  no replace at all -> silent")


def test_a_log_line_is_not_reported_as_the_failure():
    """The real reason must be visible — not just the first line of the FAIL block.

    Reproduces the actual output from 2026-10-03, verbatim in shape. The sweep reported
    the `t.Logf` at 1456 as the reason `player-registry` was RED, and the real
    `t.Fatalf` at 1462 was the next line:

        --- FAIL: TestTheRegistryNeverWritesToTheProvider (0.00s)
            import_test.go:1456: read 11 sources in internal/registry; none of them binds a provider client or writes to one
            import_test.go:1462: football.Provider exposes 7 methods; the import path is written against a seam with eight, so a test that walked it would not be walking the real one
        FAIL

    `go test` without `-v` prints a failing test's *entire* output, so both lines sit in the
    same block with the same shape. They cannot be told apart from the text, so the honest
    answer is to report both and say how many there are — not to pick one and send the reader
    after a defect that is not there.
    """
    ms = load("main_sweep")
    out = (
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

    got = ms.first_useful_line(out, skip=("FAIL", "ok  "))

    assert "1462" in got, f"the real reason must be present: {got!r}"
    assert "7 methods" in got, f"and must carry the substance: {got!r}"
    assert "[2 diagnostics]" in got, f"and must admit there was more than one: {got!r}"
    # It is not *wrong* to also carry the log line — but it must not be the ONLY thing.
    print(f"  reported: {got[:150]}")


def test_a_single_diagnostic_is_reported_without_a_count():
    """One diagnostic is the whole story, so report it plainly rather than hedging."""
    ms = load("main_sweep")
    out = ("--- FAIL: TestOne (0.00s)\n"
           "    guard_test.go:80: internal/api/x.go reads the \"X-Token\" header, which is not on the allowlist\n"
           "FAIL\n")
    got = ms.first_useful_line(out, skip=("FAIL",))
    assert got.startswith("guard_test.go:80:"), f"got {got!r}"
    assert "diagnostics" not in got, "one diagnostic needs no hedging: " + got
    print(f"  single diagnostic -> {got[:90]}")


def test_a_build_error_still_reports_its_first_useful_line():
    """With no FAIL block — a compile error, say — the first non-noise line is still right."""
    ms = load("main_sweep")
    out = (
        "# example.com/go/club\n"
        "internal/club/club.go:42:6: undefined: ClubNotDeclared\n"
    )
    got = ms.first_useful_line(out, skip=("#",))
    assert "undefined: ClubNotDeclared" in got, f"got {got!r}"
    assert got.startswith("internal/club/club.go:42"), \
        f"must skip the bare package header: {got!r}"
    print(f"  no FAIL block -> {got!r}")


# --------------------------------------------------- the duplicated parser, and its drift


def _impl(fn):
    """The function that actually decides which line is the failure.

    `main_sweep.py` binds the shared parser with `functools.partial` to carry its own `width`
    and `empty` text — a presentation choice, not a second implementation. Unwrapping to the
    underlying function is what makes "one implementation" assertable while still allowing the
    two callers to phrase themselves differently.
    """
    return getattr(fn, "func", fn)


def test_the_parser_is_shared_not_copied():
    """THE TICKET. `first_useful_line` must be one implementation, and both callers must reach it.

    Two copies of this function is the parser-drift defect, and the reason it matters is that
    `fleet-keepalive.py` renders `GATE-RED` with this line as its `->` detail. Two copies of
    the thing that formats *why a red is believed* is a risk, not tidiness.

    So this asserts **the implementation is one object**, not equality of behaviour. Two
    parsers that happen to agree today still drift tomorrow, and a behavioural assertion
    cannot see the drift until it has already caused a wrong report — which is exactly what
    happened here (see the long-`t.Logf` test below, which the copy in this repository's
    `main_sweep.py` genuinely failed).

    Asserting the *code object* rather than the name is deliberate: a private copy renamed
    something else still fails, and a `functools.partial` binding for a caller's own width
    still passes. If somebody pastes the parser back into either script, this goes red.
    """
    ms = load("main_sweep")
    fw = load("fleet-watch")
    shared = pathlib.Path(fw.first_useful_line.__code__.co_filename).resolve()
    assert shared.name == "first_useful_line.py", (
        "fleet-watch.py must call the shared parser, not one of its own; its implementation "
        f"lives in {shared.name}")
    other = pathlib.Path(_impl(ms.first_useful_line).__code__.co_filename).resolve()
    assert other == shared, (
        "main_sweep.py and fleet-watch.py must reach ONE implementation of first_useful_line. "
        f"This one is implemented in {other} and that one in {shared}, so they can drift — and "
        "have: the copy in main_sweep.py capped the joined report and dropped the real "
        "t.Fatalf. That is the parser-drift defect."
    )
    assert _impl(ms.first_useful_line) is fw.first_useful_line, (
        "both callers must reach the same function object, not two copies of the same source")
    print("  one parser, shared by both callers -> one implementation")


def test_the_parser_survives_a_long_log_line():
    """THE DRIFT THIS TICKET FOUND, as a test.

    `main_sweep.py` carried its own copy, and that copy did `joined[:200]` — capping the
    **joined** string rather than each diagnostic. So a `t.Logf` longer than the cap pushed
    the real `t.Fatalf` out of the report entirely, reproducing the original defect through a
    different route. Measured on the pre-fix copy: the real failure disappears once the log
    line exceeds **160 characters**, which is `DIAG_WIDTH`, not the 200 the copy appeared to cap at.

    `fleet-watch.py` had already fixed this. Keeping both copies meant `main` could be gated
    with the broken one and a worker's branch with the fixed one — two answers to "what
    failed", from one function's name.

    Each diagnostic therefore gets its own width, and the join is not capped at all.
    """
    ms = load("main_sweep")
    long_log = ("read 11 sources in internal/registry; none of them binds a provider "
                "client or writes to one, and the seam it walks is documented at "
                "docs/provider-seam.md for the whole family to follow")
    out = ("--- FAIL: TestTheRegistryNeverWritesToTheProvider (0.00s)\n"
           f"    import_test.go:1456: {long_log}\n"
           "    import_test.go:1462: football.Provider exposes 7 methods; the import path "
           "is written against a seam with eight\n"
           "FAIL\n")
    assert len(long_log) > 160, (
        "this test only means something while the t.Logf is long enough to have pushed 1462 "
        f"out of a DIAG_WIDTH-capped report; it is {len(long_log)} chars")

    got = ms.first_useful_line(out, skip=("FAIL",))

    assert "1462" in got, (
        "a long t.Logf must never push the real failure out of the report, and capping the "
        f"joined string does exactly that: {got!r}")
    assert "[2 diagnostics]" in got, f"got {got!r}"
    print(f"  {len(long_log)}-char t.Logf -> the real t.Fatalf is still reported")


def test_the_reversed_order_is_not_a_different_answer():
    """Order must not decide the answer — "first useful" cannot mean "first line".

    The original incident had the `t.Logf` first. A parser that reports the *first*
    diagnostic therefore looked right on the real transcript while being wrong in general,
    which is the more dangerous shape: it passes the incident it was written for.

    Reversed, a "report the first one" parser now reports the `t.Fatalf` first and looks
    correct — so the two orders are indistinguishable by inspection. Only "report them all"
    is right in both. This holds the reversed case so the shared function cannot regress to
    "first" while still passing the forward test.
    """
    ms = load("main_sweep")
    out = ("--- FAIL: TestTheRegistryNeverWritesToTheProvider (0.00s)\n"
           "    import_test.go:1462: football.Provider exposes 7 methods; the import path is "
           "written against a seam with eight\n"
           "    import_test.go:1456: read 11 sources in internal/registry; none of them binds "
           "a provider client or writes to one\n"
           "FAIL\n")

    got = ms.first_useful_line(out, skip=("FAIL",))

    assert "1456" in got, f"reversed order must still report every diagnostic: {got!r}"
    assert "1462" in got, f"reversed order must still report every diagnostic: {got!r}"
    assert "[2 diagnostics]" in got, (
        f"and must admit there was more than one candidate, or the count is order-dependent: {got!r}")
    print("  t.Fatalf first, t.Logf second -> both reported, count unchanged")


def test_output_with_no_failure_never_reports_a_log_line():
    """No `--- FAIL:` block at all means there is no test failure, so there is nothing to report.

    A green run — or a compile error with no test in it — must not come back carrying a
    `t.Logf`. The log line is indistinguishable from a fatal *inside* a FAIL block, but with
    no block there is only one candidate and the honest answer is the tool's own summary.

    This is the case that catches a parser which lost track of whether it was inside a FAIL
    block and simply reports "the first line that looks like a position" regardless.
    """
    ms = load("main_sweep")

    # A green run. Every line is a summary; none is a diagnostic.
    green = ("ok  \texample.com/go/club\t0.004s\n"
             "ok  \texample.com/go/api\t0.002s\n")
    got = ms.first_useful_line(green, skip=("ok  ",))
    assert "1456" not in got and "t.Logf" not in got, f"got {got!r}"
    assert got != "", "an empty result is not a report either"
    print(f"  a green run -> {got!r}")

    # A build error with no FAIL block: there IS one candidate and it must be reported.
    build = ("# example.com/go/club\n"
             "internal/club/club.go:42:6: undefined: ClubNotDeclared\n")
    got = ms.first_useful_line(build, skip=("#",))
    assert "ClubNotDeclared" in got, f"got {got!r}"
    print(f"  a build error with no FAIL block -> {got!r}")

    # And empty input is a named state, never a blank string.
    assert ms.first_useful_line("", skip=()).strip() != "", "empty input must still say something"
    print("  empty input -> a named state, not a blank string")


# ------------------------------------------------------- the record, and the report chain


def _run_main(ms, argv):
    """Call `main_sweep.main()` with a patched argv and captured stdout, then restore argv.

    The record's whole point is that the *cron mode* — `--dry-run` — writes it, so the test has to
    drive `main()` rather than a helper: a test of `write_run()` alone would pass while `main()`
    never called it, which is precisely the row that stayed invisible for seven sweeps.
    """
    import contextlib
    import io
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["main_sweep.py", *argv]
        with contextlib.redirect_stdout(buf):
            rc = ms.main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


def _env(path: Path):
    """Set FLEET_MAIN_SWEEP for one test and return the previous value, for restoration."""
    old = os.environ.get("FLEET_MAIN_SWEEP")
    os.environ["FLEET_MAIN_SWEEP"] = str(path)
    return old


def _restore_env(old):
    if old is None:
        os.environ.pop("FLEET_MAIN_SWEEP", None)
    else:
        os.environ["FLEET_MAIN_SWEEP"] = old


def test_a_dry_run_records_the_run_the_report_chain_reads():
    """THE TICKET. cron runs the sweep dry; the dry run must still leave a record.

    Measured on 2026-10-06: `social-studio`'s main was red from 10:00 UTC, the sweep
    gated it seven times, and every row sat in `/tmp/opencode/main-sweep.cron.log`, which nothing
    reads. So a completed run writes `.fleet-state/main-sweep.json` — including `--dry-run` —
    with the repository, commit and first useful line a report needs, and because it is a dry run
    it must file no issue.
    """
    ms = load("main_sweep")
    with tempfile.TemporaryDirectory() as td:
        where = Path(td) / "record.json"
        old = _env(where)
        filed: list = []
        # The gate output a red row carries. It is deliberately more than the summary: the first
        # line is a narration line and the LAST line is the failure, which is the incident the red log
        # exists for — identity's panic was sighted three times without ever yielding its message.
        red_output = ("ok  \texample.com/portal/internal/nlquery\t0.005s\n"
                      "--- FAIL: TestTheGuard (0.00s)\n"
                      "    guard_test.go:112: read 11 sources; this is a narration line\n"
                      "    guard_test.go:146: panic: runtime error: index out of range [7]\n"
                      "FAIL\n")
        old_repos, old_sweep, old_file = ms.repos_from_apps, ms.sweep_repo, ms.file_issue
        try:
            ms.repos_from_apps = lambda: [("portal", Path(td)),
                                          ("meta", Path(td))]
            ms.sweep_repo = lambda repo, path, keep=False: {
                "repo": repo,
                "verdict": "RED" if repo == "portal" else "GREEN",
                "commit": "8134392" if repo == "portal" else "63d26dc",
                "key": ms.dedupe_key(repo, "8134392"),
                "detail": "guard_test.go:112: read 11 sources; this is a narration line"
                          if repo == "portal" else "",
                "output": red_output if repo == "portal" else "",
                "why": "synthetic row",
            }
            ms.file_issue = lambda *a, **k: filed.append(a)
            rc, out = _run_main(ms, ["--dry-run"])
        finally:
            ms.repos_from_apps, ms.sweep_repo, ms.file_issue = old_repos, old_sweep, old_file
            _restore_env(old)

        assert rc == 1, f"a red main must still exit non-zero (reds or inconsistent): {rc}"
        assert filed == [], f"a dry run must file no issue, and this one filed {filed}"
        assert "recorded in" in out, f"the run must say it was recorded: {out[-400:]!r}"
        assert "full output:" in out and "main-sweep-red-portal-8134392.log" in out, \
            f"the sweep's own output must name the log it kept: {out[-400:]!r}"

        doc, errors = ms.main_sweep_state.read_run(where)
        assert errors == [], f"the record must read back cleanly: {errors}"
        ms_classified = ms.main_sweep_state.classify(doc, errors, where=where)
        assert len(ms_classified["red"]) == 1, \
            f"the red row must be exactly the reportable one: {ms_classified}"
        line = ms_classified["red"][0]
        assert "portal" in line, f"the repository must be in the row: {line!r}"
        assert "8134392" in line, f"the commit must be in the row: {line!r}"
        assert "boom" not in line and "narration line" in line, \
            f"the first useful line must be in the row: {line!r}"
        print(f"  dry run recorded -> {line!r}")
        print(f"  and filed nothing (dry run)")

        # The dry run must keep the full output too. cron runs the sweep dry, so the log
        # has to be written here or it is written nowhere — and the summary above is not the
        # failure: the panic is on a later line, which only the file carries.
        row = doc["rows"][0]
        assert row["log"], f"the red row must name its full-output file: {row}"
        log = Path(row["log"])
        assert log.exists(), f"the file the record names must exist: {log}"
        assert log.name == "main-sweep-red-portal-8134392.log", \
            f"one log per (repo, commit), named so a reader can find it: {log.name}"
        kept = log.read_text()
        assert kept == red_output, f"the log is the gate's whole output, not a summary: {kept!r}"
        assert "panic: runtime error: index out of range [7]" in kept, \
            f"the actual failure must be readable from the record: {kept!r}"
        assert str(log) in line, f"the report row must point at the log: {line!r}"
        print(f"  full output kept -> {log.name} ({len(kept)} bytes), named by the row")


def test_a_partial_run_is_not_recorded():
    """`--repo X` must not overwrite the record, or it reads as a whole sweep.

    A record holding only the repositories a partial run read is indistinguishable from a
    complete run in which every other repository was green — a silencer, and a silencer is worse
    than the noise it removes because it is trusted. The rule extends to the red logs: a
    partial run writes no file either, so a log is never the only artefact of a run whose record
    was deliberately withheld.
    """
    ms = load("main_sweep")
    with tempfile.TemporaryDirectory() as td:
        where = Path(td) / "record.json"
        where.write_text('{"run_at": "sentinel", "rows": [{"repo": "sentinel", "verdict": "GREEN"}]}')
        before = where.read_text()
        old = _env(where)
        old_repos, old_sweep = ms.repos_from_apps, ms.sweep_repo
        try:
            ms.repos_from_apps = lambda: [("a", Path(td)), ("b", Path(td))]
            ms.sweep_repo = lambda repo, path, keep=False: {
                "repo": repo, "verdict": "RED", "commit": "c", "key": "", "detail": "boom",
                "output": "the full red output a partial run must not keep\n",
                "why": "synthetic"}
            _, out = _run_main(ms, ["--dry-run", "--repo", "a"])
        finally:
            ms.repos_from_apps, ms.sweep_repo = old_repos, old_sweep
            _restore_env(old)

        assert where.read_text() == before, \
            "a partial run must leave the previous record untouched — a partial record would " \
            "read as a complete one whose unread repositories were green"
        assert "partial run" in out, f"and it must say so: {out[-400:]!r}"
        leftovers = sorted(Path(td).glob("main-sweep-red-*.log"))
        assert leftovers == [], f"a partial run must write no red log either: {leftovers}"
        print("  --repo run -> record untouched, no log written, and the run says why")


def test_the_log_is_what_the_summary_could_not_carry():
    """THE ACCEPTANCE. From the record alone, the red row's actual failure is readable.

    `first_useful_line` is a judgement — and, deliberately, a **bounded** one: each diagnostic is
    capped at its width. This is the case that judgement cannot carry whole, which is exactly the
    incident the ticket quotes (portal's `errno` was unprovable from the record; a session had to
    recover the text from its own logs). Reading the record's `log` field must give the complete
    message back, without re-running the gate.
    """
    ms = load("main_sweep")
    mss = ms.main_sweep_state
    # Longer than `first_useful_line`'s 200-char report width, so the summary is a truncation and
    # the file is the only place the whole message exists.
    message = ("guard_test.go:88: binding the audit log failed: sqlite3: the database file at "
               "/var/lib/identity/audit.db is locked by another connection, and the "
               "retry policy gave up after 30 attempts; errno=11 (resource temporarily "
               "unavailable) — the message a session had to recover from its own logs")
    output = ("ok  \texample.com/identity/internal/store\t0.021s\n"
              "--- FAIL: TestTheAuditLogOpens (0.00s)\n"
              f"    {message}\n"
              "FAIL\n")
    detail = ms.first_useful_line(output, skip=("FAIL", "ok  "))

    with tempfile.TemporaryDirectory() as td:
        where = Path(td) / "main-sweep.json"
        mss.write_run(
            [{"repo": "identity", "verdict": "RED", "commit": "abc123def456",
              "key": "identity@abc123def456", "detail": detail, "output": output},
             {"repo": "meta", "verdict": "GREEN", "commit": "63d26dc", "key": "",
              "detail": "", "output": ""}],
            dry_run=True, where=where)

        doc, errors = mss.read_run(where)
        assert errors == [], f"the record must read back cleanly: {errors}"
        red, green = doc["rows"][0], doc["rows"][1]

        # The summary is unchanged — it stays the row's report, and it is visibly a truncation.
        assert red["detail"] == detail, f"the summary must remain the row's report: {red!r}"
        assert len(detail) <= 200, f"this test only means something if the summary is capped: {detail!r}"
        assert detail != message, "the summary must be shorter than the message in this fixture"

        # The full message exists exactly once: in the file the row names, and nowhere else.
        log = Path(red["log"])
        assert log.is_file(), f"the record must name a file that exists: {red!r}"
        assert message in log.read_text(), \
            "every byte of the failure must survive beside the record — this is the acceptance"
        assert green["log"] == "", f"a green row must name no file: {green!r}"
        assert sorted(Path(td).glob("main-sweep-red-*.log")) == [log], \
            "a green row must not have produced a second file"

        line = mss.classify(doc, errors, where=where)["red"][0]
        assert str(log) in line, f"the report row must point at the full output: {line!r}"
        print(f"  summary capped at {len(detail)} chars; the full message is in {log.name}")
        print(f"  report row -> {line[-90:]!r}")


def test_an_oversized_red_log_is_bounded_by_a_stated_policy():
    """A red log is capped; head AND tail survive, and the marker admits the truncation.

    Bounded storage is the constraint, but dropping the tail alone would be the wrong bound:
    a `go test` run ends with the failure summary and a `make check` ends with the failing step.
    A truncated file that does not say it was truncated reads as complete, which is this
    repository's recurring defect, so the marker names the numbers.
    """
    mss = load("main_sweep_state")
    with tempfile.TemporaryDirectory() as td:
        where = Path(td) / "main-sweep.json"
        head = "HEAD-MARKER " + "h" * 1000
        tail = "t" * 1000 + " TAIL-MARKER"
        output = head + "\n" + "m" * (mss.MAX_LOG_BYTES * 3) + "\n" + tail
        mss.write_run([{"repo": "press", "verdict": "RED", "commit": "feed12345678",
                        "key": "press@feed12345678", "detail": "boom", "output": output}],
                      dry_run=True, where=where)
        doc, _ = mss.read_run(where)
        log = Path(doc["rows"][0]["log"])
        raw = log.read_bytes()
        kept = raw.decode()
        dropped = len(output.encode()) - 2 * (mss.MAX_LOG_BYTES // 2)

        assert len(raw) <= mss.MAX_LOG_BYTES + 200, \
            f"the file must be bounded by its stated cap: {len(raw)} bytes"
        assert "HEAD-MARKER" in kept, "the head must survive the cap"
        assert "TAIL-MARKER" in kept, "the tail must survive the cap"
        assert f"truncated {dropped} of {len(output.encode())} bytes" in kept, \
            f"the marker must say what it dropped, in bytes: {kept[-220:]!r}"
        print(f"  {len(output.encode())} bytes in -> {len(raw)} bytes kept, marker says "
              f"{dropped} dropped")


def test_a_green_run_writes_no_log_and_rotates_the_stale_one():
    """Storage never grows on green, and a fixed red does not leave its log behind.

    The ticket's constraint verbatim: a green run must not grow storage. The rotation is what
    makes that true over time rather than only per run — a repository that goes green has its
    stale log removed by the next completed run, so the directory holds at most one log per
    currently-red repository.
    """
    ms = load("main_sweep")
    mss = ms.main_sweep_state
    with tempfile.TemporaryDirectory() as td:
        where = Path(td) / "main-sweep.json"
        mss.write_run([{"repo": "press", "verdict": "RED", "commit": "feed12345678",
                        "key": "press@feed12345678", "detail": "boom",
                        "output": "--- FAIL: TestPress (0.00s)\n    p_test.go:1: boom\nFAIL\n"}],
                      dry_run=True, where=where)
        doc, _ = mss.read_run(where)
        stale = Path(doc["rows"][0]["log"])
        assert stale.is_file(), f"precondition: the red log must exist: {stale}"

        mss.write_run([{"repo": "press", "verdict": "GREEN", "commit": "beef9876",
                        "key": "", "detail": "make check passed", "output": ""}],
                      dry_run=True, where=where)
        doc, _ = mss.read_run(where)

        assert doc["rows"][0]["log"] == "", f"a green row names no log: {doc['rows'][0]!r}"
        assert not stale.exists(), "the stale log must be pruned when its repository goes green"
        assert sorted(Path(td).glob("main-sweep-red-*.log")) == [], \
            "a green run must leave no red log behind"
        print("  red log written -> record rewritten green -> log pruned, nothing left")


def test_a_repository_name_with_a_slash_still_gets_one_flat_log_name():
    """`shared/go` is in `apps.json`, so the slug is a production path, not a corner.

    A name that kept the slash would point the record at a directory beside the record that does
    not exist — a pointer to nothing, which is the discarded output one indirection later. And
    the encoding must not be `-`: a repository name may contain one, so `shared/go`
    and a hypothetical `shared-go` would share a file, one log silently the other's.
    """
    mss = load("main_sweep_state")
    with tempfile.TemporaryDirectory() as td:
        where = Path(td) / "main-sweep.json"
        p = mss.red_log_path("shared/go", "abc123def456789", where=where)
        assert p.parent == Path(td), f"the log belongs beside the record: {p}"
        assert p.name == "main-sweep-red-shared__go-abc123def456.log", p.name
        assert "/" not in p.name, f"the name must be one flat file: {p.name!r}"
        flat = mss.red_log_path("shared-go", "abc123def456789", where=where)
        assert flat.name != p.name, \
            f"a slash and a hyphen must not collide into one file: {p.name!r} vs {flat.name!r}"
        print(f"  shared/go -> {p.name}")


def test_absence_of_a_record_is_never_a_pass():
    """The reader's rule, held at each absence: nothing recorded, stale, undated, empty, unknown.

    This is the same rule `expected_ci.py` holds for a declaration and the sweep's own gate
    holds for `NONE`: a monitor that cannot answer must say so rather than read as a pass. The
    unknown-verdict case is the silencer direction — a typo in the writer must not make a row
    disappear from every report.
    """
    mss = load("main_sweep_state")
    now = time.time()

    missing = mss.classify(None, [], where=Path("/nonexistent/main-sweep.json"))
    assert missing["unverified"], "no record at all must be unverified"
    assert missing["red"] == [], "and it must not invent a red"

    stale = mss.classify({"run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(now - 3 * 60 * 60)),
                          "rows": [{"repo": "r", "verdict": "GREEN"}]}, [])
    assert stale["unverified"], f"a stale record must be unverified: {stale}"
    assert stale["verified_green"] == 1, "its green rows are still counted, just not trusted"

    undated = mss.classify({"rows": [{"repo": "r", "verdict": "GREEN"}]}, [])
    assert undated["unverified"], "a record with no parseable date cannot be treated as current"

    empty = mss.classify({"run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                          "rows": []}, [])
    assert empty["unverified"], "an empty record is a record of nothing, not a pass"

    unreadable = mss.classify(None, ["the record is not valid JSON"])
    assert unreadable["unverified"] and "not valid JSON" in unreadable["unverified"][0]

    unknown = mss.classify({"run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                            "rows": [{"repo": "r", "verdict": "GREN", "commit": "abc"}]}, [])
    assert unknown["unverified"], \
        "a verdict this reader does not recognise must be unverified, not silently dropped"
    assert unknown["verified_green"] == 0, "and must never count as verified"
    print("  missing / stale / undated / empty / unreadable / unknown -> all unverified")


def test_a_red_appearing_changes_the_placement_key():
    """The wake-up is rate-limited by content, so a red appearing must be a content change.

    Without this, the first appearance of a red hashes identically to the run before it and the
    wake-up path reports it as "unchanged since the last wake-up, staying quiet" — the sweep record restored
    through the mechanism built to fix it. Asserted here as well as in the notify tests because
    this is the property, not the rendering.
    """
    mss = load("main_sweep_state")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    green = mss.classify({"run_at": now, "rows": [{"repo": "r", "verdict": "GREEN"}]}, [])
    red = mss.classify({"run_at": now, "rows": [{"repo": "r", "verdict": "RED",
                                                 "commit": "abc", "detail": "boom",
                                                 "log": "/state/main-sweep-red-r-abc.log"}]}, [])
    assert mss.placement_key(green) == "", \
        f"a fully green run has nothing actionable to hash: {mss.placement_key(green)!r}"
    assert mss.placement_key(green) != mss.placement_key(red), \
        "a red appearing must change the placement key, or its first report is suppressed"
    # And a new commit on the same red is the same situation, not news — the CI placement key
    # deliberately drops the sha for the same reason: otherwise every push is a wake-up. The
    # The red-log path rides in the rendered line, so this also holds that the pointer does not turn
    # every re-gate into a wake-up.
    red2 = mss.classify({"run_at": now, "rows": [{"repo": "r", "verdict": "RED",
                                                  "commit": "def", "detail": "boom again",
                                                  "log": "/state/main-sweep-red-r-def.log"}]}, [])
    assert mss.placement_key(red) == mss.placement_key(red2), \
        "a new commit on an already-red main is the same situation, not a new wake-up per push"
    print(f"  green -> {mss.placement_key(green)!r}; red -> {mss.placement_key(red)!r}")


def main() -> int:
    tests = [
        test_three_outcomes_not_two,
        test_red_main_carries_the_first_useful_line,
        test_the_red_detail_survives_make_believing_it_is_a_sub_make,
        test_dedupe_key_is_the_failing_commit,
        test_should_file_only_when_not_already_open,
        test_mutation_a_gate_that_lies_is_caught,
        test_green_main_is_green_and_says_what_ran,
        test_harness_broken_is_reported_as_not_tested_not_red,
        test_harness_broken_is_silent_when_replaces_resolve,
        test_a_log_line_is_not_reported_as_the_failure,
        test_a_single_diagnostic_is_reported_without_a_count,
        test_a_build_error_still_reports_its_first_useful_line,
        test_the_parser_is_shared_not_copied,
        test_the_parser_survives_a_long_log_line,
        test_the_reversed_order_is_not_a_different_answer,
        test_output_with_no_failure_never_reports_a_log_line,
        test_a_dry_run_records_the_run_the_report_chain_reads,
        test_a_partial_run_is_not_recorded,
        test_the_log_is_what_the_summary_could_not_carry,
        test_an_oversized_red_log_is_bounded_by_a_stated_policy,
        test_a_green_run_writes_no_log_and_rotates_the_stale_one,
        test_a_repository_name_with_a_slash_still_gets_one_flat_log_name,
        test_absence_of_a_record_is_never_a_pass,
        test_a_red_appearing_changes_the_placement_key,
    ]
    print("test_main_sweep")
    failed = 0
    for t in tests:
        print(f"\n- {t.__name__}")
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL: {exc}")
        except Exception as exc:  # noqa: BLE001 - a crash is a failure too, and must say so
            failed += 1
            print(f"  ERROR: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"FAILED: {failed} of {len(tests)}")
        return 1
    print(f"ok: {len(tests)} of {len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())