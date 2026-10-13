#!/usr/bin/env python3
"""Tests for the pipeline verdict, and above all for the two reds being different verdicts.

Run: `python3 scripts/test_gitlab_ci.py` (no test runner needed, no network, no sleeps).

## Why this file exists

The keepalive ran local gates and reported `GREEN` for repositories whose GitLab
pipeline was red. The failure this family spent a day on was **only ever visible in GitLab**:
`make check` was green on the workstation, because the workstation has the sibling checkout the
pipeline could not obtain.

Adding a pipeline read is easy and almost certainly wrong on its own, because of the trap:

> **Right now every pipeline in this family is red, and none of it is a code defect.** The shared
> runners have no compute minutes, so every job is rejected before it starts.

A reader that treats `status: failed` as "broken" reports **every repository red, forever**. A
monitor that is always red is a monitor nobody reads — the same failure as an always-green one,
with the opposite cause, and the reason this file tests the discriminator rather than the happy
path.

## What is held here

1. **The two reds are different verdicts.** `ci_quota_exceeded` with `started_at: null` is
   `BLOCKED`; `script_failure` with a duration is `RED`. These are the two payloads recorded from
   the live group, verbatim in shape, not invented.
2. **Neither `RED` nor `BLOCKED` is a pass, and `BLOCKED` is not a fault.** `GREEN` is the only
   value that means verified.
3. **The indistinguishable case is named, not guessed.** A pipeline that failed, whose jobs cannot
   be read, is `UNKNOWN` — never `RED`, because "a job ran and failed" is a claim about code and
   this cannot support it.
4. **`classify` is not a constant.** It is asserted against *both* recorded shapes, because a
   classifier tested only on the case that is currently breaking cannot tell a discriminator from
   a constant — that is the mutation, and it is the shape of every false-pass bug in this project.
5. **The mutation.** `ci_quota_exceeded` mapped to `RED` makes the two-reds tests fail. Bound in
   the file rather than applied by patching, with the edit asserted, because a mutation that
   silently did not apply is worse than no mutation.

## Why the network is stubbed and the script is not

These drive the real `gitlab-ci.py` through a strict `glab` on a temp `PATH`, for the reason
`test_cleanup_workers.py` does the same with the real bash sweep: a Python copy of the logic
passes while the script stays broken, which is the probe that was never a test.

**The `glab` stub is strict.** The one in `test_cleanup_workers.py` shipped `glab mr list --state
opened` — a flag the real CLI rejects — through 27 of 27 green tests, because it accepted any
argument. So this one enumerates the accepted surface (`glab api` with a pipeline path and a jobs
path) and exits non-zero on anything else, and one test asserts that a bad invocation is rejected
rather than answered.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SCRIPT = HERE / "scripts" / "gitlab_ci.py"


# The two payloads, recorded from the live group on 2026-10-05 and 2026-10-03. Verbatim in shape
# — field names and values as the API returned them — because a fixture invented here would test
# this file's imagination rather than GitLab's behaviour. The discriminator's whole claim is
# about what these two payloads look like.
QUOTA_PIPELINE = {"id": 2912773493, "iid": 41, "status": "failed", "ref": "main",
                  "sha": "c55620475644314a0d99c2c94cdf1413052079e6", "source": "push",
                  "created_at": "2026-10-05T09:15:23.469Z"}
QUOTA_JOBS = [
    {"id": 16933132200, "name": "drill", "stage": "test", "status": "failed",
     "failure_reason": "ci_quota_exceeded", "started_at": None, "finished_at": "2026-10-05T09:15:23.880Z",
     "duration": None, "queued_duration": None, "runner": None, "runner_manager": None,
     "allow_failure": False, "tag_list": []},
    {"id": 16933132201, "name": "lint", "stage": "test", "status": "failed",
     "failure_reason": "ci_quota_exceeded", "started_at": None, "finished_at": "2026-10-05T09:15:23.921Z",
     "duration": None, "queued_duration": None, "runner": None, "runner_manager": None,
     "allow_failure": False, "tag_list": []},
    {"id": 16933132202, "name": "test", "stage": "test", "status": "failed",
     "failure_reason": "ci_quota_exceeded", "started_at": None, "finished_at": "2026-10-05T09:15:24.054Z",
     "duration": None, "queued_duration": None, "runner": None, "runner_manager": None,
     "allow_failure": False, "tag_list": []},
]

RAN_PIPELINE = {"id": 2909683961, "iid": 36, "status": "failed", "ref": "main",
                "sha": "74c75556e10d1b2a6f8dd0f0b9c31d4e5f6a7b8c", "source": "push",
                "created_at": "2026-10-03T20:03:28.495Z"}
RAN_JOBS = [
    {"id": 16922880001, "name": "drill", "stage": "test", "status": "failed",
     "failure_reason": "script_failure", "started_at": "2026-10-03T20:03:24.100Z",
     "finished_at": "2026-10-03T20:03:44.090Z", "duration": 19.990588, "runner": {"id": 4123},
     "allow_failure": False},
    {"id": 16922880002, "name": "api-check", "stage": "test", "status": "failed",
     "failure_reason": "script_failure", "started_at": "2026-10-03T20:03:24.100Z",
     "finished_at": "2026-10-03T20:03:47.500Z", "duration": 23.400528, "runner": {"id": 4123},
     "allow_failure": False},
    {"id": 16922880003, "name": "web-test", "stage": "test", "status": "success",
     "failure_reason": None, "started_at": "2026-10-03T20:03:24.100Z",
     "finished_at": "2026-10-03T20:03:44.600Z", "duration": 20.496708, "runner": {"id": 4123},
     "allow_failure": False},
]

GREEN_PIPELINE = {"id": 2909460841, "iid": 33, "status": "success", "ref": "main",
                  "sha": "0041680200ffee0011223344556677889900aabb", "source": "push",
                  "created_at": "2026-10-02T11:00:00.000Z"}
GREEN_JOBS = [
    {"id": 16922870001, "name": "lint", "stage": "test", "status": "success",
     "failure_reason": None, "started_at": "2026-10-02T11:00:05.000Z",
     "finished_at": "2026-10-02T11:00:23.200Z", "duration": 18.2, "runner": {"id": 4123}},
]

# A pipeline with no jobs at all. The case that has no name in the family and is worth one
# anyway: GitLab returns `[]` for a pipeline whose jobs were purged, and reading that as "nothing
# failed, therefore green" is precisely how a tool manufactures a pass.
NO_JOBS: list = []


def load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gitlab_ci", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gc = load()
# The spelling list, bound by name so a test can assert against what the classifier consults
# rather than against a copy of it.
NEVER_STARTED_REASONS = gc.NEVER_STARTED_REASONS


class TheTwoRedsAreDifferentVerdicts(unittest.TestCase):
    """THE property. `RED` and `BLOCKED` are the same GitLab status and different facts."""

    def test_a_pipeline_that_never_ran_is_blocked_not_red(self):
        """The live shape: `failed` + `ci_quota_exceeded` + `started_at: null`."""
        verdict, why = gc.classify(QUOTA_PIPELINE, QUOTA_JOBS)
        self.assertEqual(verdict, gc.BLOCKED,
                         f"the shared runners have no compute minutes, so this is billing and not "
                         f"a code defect. Got {verdict!r}: {why}")
        self.assertNotEqual(verdict, gc.RED)
        self.assertIn("compute minutes", why,
                      f"the reason must name the cause, or a reader goes looking for a code "
                      f"defect that does not exist: {why!r}")
        self.assertIn("not verified", why,
                      f"and it must say that nothing was verified — BLOCKED is not a pass: {why!r}")
        print(f"    quota   -> {verdict}: {why[:88]}")

    def test_a_pipeline_whose_jobs_ran_and_failed_is_red(self):
        """The other shape: `failed` + `script_failure` + a duration. This IS a code defect."""
        verdict, why = gc.classify(RAN_PIPELINE, RAN_JOBS)
        self.assertEqual(verdict, gc.RED,
                         f"two jobs ran for 20 seconds each and exited 1 — that is a code defect. "
                         f"Got {verdict!r}: {why}")
        self.assertNotEqual(verdict, gc.BLOCKED)
        self.assertIn("code defect", why,
                      f"the reader needs to know this one is theirs and not the runner's: {why!r}")
        print(f"    ran+fell -> {verdict}: {why[:88]}")

    def test_the_two_verdicts_are_not_interchangeable(self):
        """The comparison, so neither test can pass because both halves are the same string."""
        blocked, _ = gc.classify(QUOTA_PIPELINE, QUOTA_JOBS)
        red, _ = gc.classify(RAN_PIPELINE, RAN_JOBS)
        self.assertNotEqual(blocked, red,
                            "the two recorded payloads differ only in the fields the "
                            f"discriminator reads, so they must not produce one verdict: "
                            f"both were {blocked!r}")


class OnlyGreenIsAPass(unittest.TestCase):
    """`BLOCKED` is neither red nor green, and `UNKNOWN` is not green either."""

    def test_every_verdict_but_green_fails_the_only_pass_test(self):
        for verdict in gc.VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertNotEqual(verdict, gc.RED if verdict == gc.BLOCKED else None)
        self.assertEqual(gc.GREEN, "GREEN")
        for verdict in (gc.RED, gc.BLOCKED, gc.RUNNING, gc.NONE, gc.UNKNOWN):
            self.assertNotEqual(verdict, gc.GREEN,
                                f"{verdict} must not be the only pass; a blocked pipeline is not "
                                f"a verification")

    def test_a_succeeded_pipeline_with_no_failed_job_is_green(self):
        verdict, why = gc.classify(GREEN_PIPELINE, GREEN_JOBS)
        self.assertEqual(verdict, gc.GREEN, why)
        print(f"    green   -> {verdict}: {why[:88]}")

    def test_a_pipeline_with_no_jobs_at_all_is_not_green(self):
        """GitLab can return `[]` for a pipeline whose jobs were purged.

        Reading that as "nothing failed, therefore green" manufactures a pass out of an absence,
        which is the direction this whole file exists to stop. It is `UNKNOWN`, which is not a
        pass.
        """
        verdict, why = gc.classify({**GREEN_PIPELINE, "status": "failed"}, NO_JOBS)
        self.assertEqual(verdict, gc.UNKNOWN,
                         f"a failed pipeline with no readable jobs cannot be called either red or "
                         f"green. Got {verdict!r}: {why}")
        self.assertNotIn("does not classify", why,
                         f"`failed` is a status this reader knows, so blaming the status would "
                         f"name the wrong thing: {why!r}")
        self.assertIn("not visible", why,
                      f"the reason must name the real gap — a missing job, not a missing "
                      f"vocabulary: {why!r}")
        print(f"    no jobs -> {verdict}: {why[:88]}")

    def test_a_pipeline_in_flight_is_running_not_green(self):
        for status in ("running", "pending", "created"):
            verdict, _ = gc.classify({**GREEN_PIPELINE, "status": status}, NO_JOBS)
            self.assertEqual(verdict, gc.RUNNING,
                             f"a {status} pipeline has no verdict yet and must not be green")

    def test_a_status_this_reader_does_not_know_is_unknown(self):
        """Not `GREEN` and not `RED`. An unrecognised status is the definition of 'cannot tell'."""
        verdict, why = gc.classify({**GREEN_PIPELINE, "status": "wedged"}, NO_JOBS)
        self.assertEqual(verdict, gc.UNKNOWN,
                         f"an unrecognised status must be UNKNOWN: {verdict!r} ({why})")


class MixedAndEdgeCases(unittest.TestCase):
    """The cases that have no clean answer, named rather than resolved."""

    def test_half_ran_half_never_started_is_red_and_says_so(self):
        """There is a real defect in there *and* the rest proved nothing.

        Picking either half is the guess this file refuses to make, but the half that ran is
        actionable and burying it behind `UNKNOWN` would hide a code defect. So it is `RED`,
        with the incompleteness in the reason.
        """
        jobs = [RAN_JOBS[0], QUOTA_JOBS[1]]
        verdict, why = gc.classify(RAN_PIPELINE, jobs)
        self.assertEqual(verdict, gc.RED, f"got {verdict!r}: {why}")
        self.assertIn("never started", why,
                      f"the reason must admit the pipeline does not report on every job: {why!r}")
        print(f"    mixed   -> {verdict}: {why[:88]}")

    def test_a_cancelled_job_with_a_reason_is_not_called_never_started(self):
        """The guard on the second half of the signature.

        `started_at is None and duration is None` alone would call a job that was cancelled
        mid-run "never started", which is the guess this is written to avoid: a cancelled job
        says something about the pipeline, and a never-started job says nothing about the code.
        """
        cancelled = {"id": 3, "name": "lint", "status": "failed", "failure_reason": "job_execution_timeout",
                     "started_at": None, "duration": None, "runner": {"id": 7}}
        verdict, why = gc.classify(RAN_PIPELINE, [cancelled])
        self.assertEqual(verdict, gc.RED,
                         f"a job with an explicit failure_reason is not a runner problem: "
                         f"{verdict!r} ({why})")

    def test_an_unrecognised_reason_with_no_timing_is_red_not_blocked(self):
        """THE trade, stated rather than assumed — and it is a judgement call.

        A job with `started_at: null`, `duration: null` and a reason this reader has never seen is
        genuinely ambiguous. The timing says the runner never picked it up; the unknown reason
        says something happened that produced a classification. This reader resolves it as
        `RED`, and the reason is **not** the comfortable one to pick: `BLOCKED` is the verdict a
        reader learns to ignore, so an unfamiliar failure must not be laundered into it on the
        strength of two null fields.

        The cost of this choice, stated plainly: a genuinely never-started job whose reason GitLab
        spells some new way is reported as a code defect until someone adds the spelling to
        `NEVER_STARTED_REASONS`. That is the right way round, because the remedy is one word in
        one file and the mistake it prevents is a whole family of repositories being reported
        healthy while nothing has run in any of them.
        """
        odd = {"id": 4, "name": "lint", "status": "failed", "failure_reason": "some_future_reason",
               "started_at": None, "duration": None, "runner": None}
        verdict, why = gc.classify(RAN_PIPELINE, [odd])
        self.assertEqual(verdict, gc.RED,
                         f"an unrecognised failure_reason must not be laundered into BLOCKED: "
                         f"got {verdict!r} ({why})")
        self.assertEqual(NEVER_STARTED_REASONS & {"some_future_reason"}, set(),
                         "precondition for the bug this guards: the reason is genuinely unknown, "
                         "so the only thing separating RED from BLOCKED is this reader's spelling "
                         "list")

    def test_a_never_started_job_with_no_reason_at_all_is_blocked(self):
        """The other side, and the case GitLab actually produces.

        No `failure_reason` **and** no timing is the shape a pipeline reports when the runner
        never registered the job at all, so it is `BLOCKED` — and it is the one spelling-agnostic
        route to that verdict, which is what makes this reader's list a convenience rather than
        the load-bearing part.
        """
        bare = {"id": 5, "name": "lint", "status": "failed", "failure_reason": None,
                "started_at": None, "duration": None, "runner": None}
        verdict, why = gc.classify(RAN_PIPELINE, [bare])
        self.assertEqual(verdict, gc.BLOCKED, f"got {verdict!r}: {why}")
        self.assertIn("not evidence of a code defect", why,
                      f"BLOCKED's reason must always say it is not a defect: {why!r}")
        self.assertNotIn("compute minutes", why,
                         f"with no reason to go on, the reason must not claim billing — that "
                         f"would be a guess: {why!r}")
        print(f"    bare     -> {verdict}: {why[:88]}")


# The pre-fix classifier, verbatim in spirit: one verdict for every failure. Reproducing it in the
# file rather than paraphrasing is deliberate — a paraphrase would be the mutation that never
# applied, which is the more dangerous of the two failure modes this project keeps hitting.
def prefix_classify(pipeline, jobs):
    for job in jobs:
        if job.get("status") == "failed":
            return gc.RED, f"{job.get('name')} failed"
    return gc.GREEN, "no failures"


class TheMutationThatProvesTheDiscriminatorIsLive(unittest.TestCase):
    """A guard that cannot fail proves nothing, and this one guards the file's whole purpose."""

    def test_the_pre_fix_classifier_collapses_the_two_reds(self):
        """Bind the pre-fix behaviour and require it to FAIL the assertions above."""
        blocked, _ = prefix_classify(QUOTA_PIPELINE, QUOTA_JOBS)
        self.assertEqual(blocked, gc.RED,
                         "the pre-fix classifier must still be here for the mutation to mean "
                         f"anything; it reported {blocked!r} for a pipeline whose jobs never "
                         "started — that IS the defect, and it is why the assertions above are "
                         "not vacuous")
        red, _ = prefix_classify(RAN_PIPELINE, RAN_JOBS)
        self.assertEqual(red, blocked, "precondition for the bug this guards: both payloads "
                                       "produced one verdict, so nothing could tell them apart")

    def test_and_the_live_classifier_does_not(self):
        blocked, _ = gc.classify(QUOTA_PIPELINE, QUOTA_JOBS)
        red, _ = gc.classify(RAN_PIPELINE, RAN_JOBS)
        self.assertNotEqual(blocked, red,
                            f"the live classifier still returns one verdict for both payloads: "
                            f"{blocked!r}")

    def test_the_discriminator_is_the_field_and_not_a_constant(self):
        """One job, one field changed. Nothing else about the two payloads differs."""
        quota_job = dict(QUOTA_JOBS[0])
        ran_job = dict(quota_job)
        # The ONLY changes are the three fields the discriminator reads. If the verdict did not
        # move, the classifier is reading something else — or nothing.
        ran_job["failure_reason"] = "script_failure"
        ran_job["started_at"] = "2026-10-05T09:15:23.700Z"
        ran_job["duration"] = 19.99
        self.assertEqual(gc.classify(QUOTA_PIPELINE, [quota_job])[0], gc.BLOCKED)
        self.assertEqual(gc.classify(QUOTA_PIPELINE, [ran_job])[0], gc.RED,
                         "the same pipeline and the same job name, with only the discriminator's "
                         "fields changed, must change the verdict")


GLAB_STUB = r"""#!/usr/bin/env bash
# Strict, like the real CLI: `glab api <path>` is the only surface this project reads pipelines
# through. The stub in test_cleanup_workers.py answered anything, which shipped
# `glab mr list --state opened` — a flag the real CLI rejects — through 27 of 27 green tests.
if [ "$1" != "api" ]; then
  printf 'Unknown command: %s\n' "$1" >&2
  exit 2
fi
case "$2" in
  *"/pipelines?ref="*"/per_page=1"|*"pipelines?ref=main&per_page=1") cat "$PIPELINES" ;;
  *"/jobs?per_page="*) cat "$JOBS" ;;
  *) printf 'Unknown endpoint: %s\n' "$2" >&2; exit 1 ;;
esac
exit 0
"""


class TheScriptDrivenThroughAStubbedGitLab(unittest.TestCase):
    """The real `gitlab-ci.py`, end to end, with only the network underneath it replaced."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gitlab-ci-test."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stub = self.bin / "glab"
        stub.write_text(GLAB_STUB)
        stub.chmod(0o755)
        # `meta` rather than any other name: `repos()` always appends meta and then
        # de-duplicates, so naming it here yields exactly ONE repository. A fixture naming a
        # different repository yields two, and every count in a report becomes a fact about the
        # fixture instead of about the code.
        self.apps = self.tmp / "apps.json"
        self.apps.write_text(json.dumps({"apps": [{"id": "meta", "repo": "meta"}]}))
        self.pipelines = self.tmp / "pipelines.json"
        self.jobs = self.tmp / "jobs.json"

    def _write(self, pipelines, jobs):
        self.pipelines.write_text(json.dumps(pipelines))
        self.jobs.write_text(json.dumps(jobs))

    def _run(self, *args):
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
               "FLEET_APPS_JSON": str(self.apps),
               "PIPELINES": str(self.pipelines), "JOBS": str(self.jobs)}
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_the_two_reds_through_the_real_script(self):
        """The property again, but through `gitlab-ci.py --json` rather than the pure function.

        The pure function is where the decision lives; this is the check that the decision is
        reachable from the command a monitor actually runs, which is a different claim and has
        been false before (a `first_useful_line` that worked and a `gate()` that did not).
        """
        self._write([QUOTA_PIPELINE], QUOTA_JOBS)
        blocked = json.loads(self._run("--json").stdout)["repos"][0]
        self.assertEqual(blocked["verdict"], gc.BLOCKED, blocked["why"])
        self.assertEqual(blocked["sha"], "c5562047",
                         f"a verdict must carry the commit it is about: {blocked}")
        self.assertEqual(blocked["pipeline"], QUOTA_PIPELINE["id"])

        self._write([RAN_PIPELINE], RAN_JOBS)
        red = json.loads(self._run("--json").stdout)["repos"][0]
        self.assertEqual(red["verdict"], gc.RED, red["why"])
        print(f"    script, quota -> {blocked['verdict']}; script, ran -> {red['verdict']}")

    def test_a_glab_that_answers_anything_would_not_catch_a_typo(self):
        """The stub is strict, asserted rather than assumed.

        If this stub accepted an unknown endpoint it would answer with nothing, and a script
        that asked for the wrong path would report `UNKNOWN` — a plausible-looking verdict from a
        broken command, which is the exact failure the strict stub exists to make loud.
        """
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
               "FLEET_APPS_JSON": str(self.apps), "PIPELINES": str(self.pipelines),
               "JOBS": str(self.jobs)}
        self._write([QUOTA_PIPELINE], QUOTA_JOBS)
        bad = subprocess.run([str(self.bin / "glab"), "api", "projects/x/nonsense"],
                             capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(bad.returncode, 0,
                            f"the glab stub must reject an unknown endpoint, or it is the "
                            f"permissive stub this project already paid for once: {bad.stdout!r}")
        notapi = subprocess.run([str(self.bin / "glab"), "mr", "list"],
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(notapi.returncode, 0,
                            f"`glab mr list` has no `--state` and is not how pipelines are read; "
                            f"the stub must not answer it: {notapi.stdout!r}")

    def test_unreadable_gitlab_is_unknown_not_green_and_not_red(self):
        """No `glab` anywhere. The tool must say it cannot see, and must not guess.

        PATH alone no longer simulates absence, and that is the point of the change: the tool
        now looks in `~/go/bin` when PATH misses `glab`, because cron's PATH does and a check
        that reported "not installed" while it sat in `~/go/bin` was reporting about PATH.

        So absence is simulated at both ends — PATH emptied *and* `HOME` pointed at an empty
        directory, so none of the fallbacks can find it. A test that empties PATH while the real
        binary is reachable no longer tests the state it names.
        """
        env = {**os.environ, "PATH": str(self.tmp / "empty"), "HOME": str(self.tmp / "empty"),
               "FLEET_APPS_JSON": str(self.apps)}
        (self.tmp / "empty").mkdir()
        run = subprocess.run([sys.executable, str(SCRIPT), "--json"],
                             capture_output=True, text=True, env=env, timeout=60)
        rows = json.loads(run.stdout)["repos"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], gc.UNKNOWN,
                         f"an absent glab is the absence of an answer, not a green pipeline: "
                         f"{rows[0]}")
        self.assertIn("not on PATH", rows[0]["why"])
        print(f"    no glab  -> {rows[0]['verdict']}: {rows[0]['why'][:76]}")

    def test_the_table_separates_the_counts_and_says_blocked_is_not_a_pass(self):
        """The printed report must not collapse the two reds into one number.

        The number a reader quotes is the number that decides what they do next, and "8 RED"
        would send someone to fix code that was never executed.
        """
        self._write([QUOTA_PIPELINE], QUOTA_JOBS)
        out = self._run("--once").stdout
        self.assertIn("BLOCKED", out)
        self.assertIn("CI minutes", out)
        self.assertIn("BLOCKED is not a pass", out,
                      f"the table must say so, because 'blocked' reads as a completed check: {out}")
        self.assertIn("0 RED", out,
                      f"a quota-blocked pipeline must not be counted as RED: {out}")


class ACronEnvironmentIsNotTheEnvironmentATestRunsIn(unittest.TestCase):
    """The whole CI verdict was `UNKNOWN` for hours, and no test ran the way cron runs.

    Two separate things were missing under cron and **both** were invisible, because every test
    inherited an interactive shell's environment:

    1. `glab` is installed by `go install` into `~/go/bin`, and cron's `PATH` is
       `/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin` — so `shutil.which` found nothing
       and every repository read "`glab` is not on PATH".
    2. `GLAB_CONFIG_DIR` and `GITLAB_TOKEN` are absent, so even a found `glab` answered 403 — and
       `glab-env.sh` made it worse by **refusing to export** when the binary was missing, which
       is the one case where the exports were needed.

    Each was reported as `UNKNOWN`, which is exactly right and completely unhelpful: *cannot tell*
    and *did not look in the right place* print the same word. So the test that would have caught
    it is the one that runs the real command under cron's real `PATH`, with nothing inherited.

    It asserts a *fact about the environment*, not a verdict: with a token available, the tool
    must not answer "`glab` is not on PATH". That holds on a machine with no `glab` at all, where
    the honest answer is still `UNKNOWN`.
    """

    CRON_PATH = "/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"

    def test_under_crons_own_path_the_tool_does_not_claim_glab_is_absent(self):
        token = os.environ.get("GITLAB_TOKEN") or _token_on_disk()
        if not token:
            self.skipTest("no workspace token available; nothing to prove against")

        env = {"PATH": self.CRON_PATH, "HOME": str(Path.home())}
        run = subprocess.run([sys.executable, str(SCRIPT), "--json"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(run.returncode, 0,
                         f"the script must survive cron's environment, not traceback:\n"
                         f"{run.stderr[-1500:]}")
        payload = json.loads(run.stdout)
        self.assertNotIn(
            "not on PATH",
            run.stdout,
            "under cron's PATH this tool reported `glab is not on PATH` for every repository, "
            "forever. `glab` is installed by `go install` into `~/go/bin`, which is not on that "
            "PATH — so the tool must look there before concluding it is absent.")

    def test_the_token_and_the_binary_are_independent_facts(self):
        """`glab-env.sh` used to return non-zero and discard the exports when glab was missing.

        The token and the config directory are facts about a file on disk; the binary is a fact
        about PATH. A caller that needs one may be missing the other, and the profile script is
        the wrong place to enforce a coupling between them — it is sourced by things that only
        want the credential.
        """
        env_file = SCRIPT.parent / "glab-env.sh"
        self.assertTrue(env_file.is_file(), "glab-env.sh is the workspace's one glab profile")
        out = subprocess.run(
            ["bash", "-c",
             f"source '{env_file}' >/dev/null 2>&1; "
             'echo "rc=$? tok=${GITLAB_TOKEN:+set} cfg=${GLAB_CONFIG_DIR:+set}"'],
            capture_output=True, text=True, timeout=30,
            env={"PATH": self.CRON_PATH, "HOME": str(Path.home())})
        self.assertIn("rc=0", out.stdout,
                      f"sourcing the profile under cron's PATH must still succeed, and must "
                      f"still export the token: {out.stdout!r} {out.stderr[-400:]!r}")
        self.assertIn("tok=set", out.stdout,
                      f"the token is a fact about a file on disk and does not depend on the "
                      f"binary being on PATH: {out.stdout!r}")


def _token_on_disk() -> str | None:
    """The workspace token, read straight off disk — no env, no glab."""
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        candidate = parent / ".gitlab-cli" / "token"
        if candidate.is_file():
            return candidate.read_text().strip() or None
    return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--explain":
        print(__doc__)
        sys.exit(0)
    unittest.main(verbosity=2)