#!/usr/bin/env python3
"""The GitHub CI reader: the same two reds, told apart on GitHub's shapes.

The classifier is pure and tested against both live shapes — a run whose job started and failed,
and a run whose job never started (no `startedAt`) — because a classifier tested only on the
case that is currently breaking cannot tell a discriminator from a constant. The reader's argv,
the unreachable/none distinction and the `--json` shape are stubbed through `PATH`.

Run: `python3 scripts/test_github_ci.py`. Stub `gh`; no network.
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

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "github_ci.py"
sys.path.insert(0, str(HERE))

import factory_config  # noqa: E402
import github_ci  # noqa: E402

ENV_KEYS = ("PATH", "CALLLOG", "FACTORY_TRACKER_PROVIDER", "FACTORY_TRACKER_OWNER",
            "FACTORY_CONFIG", "FACTORY_META_REPO", "FLEET_GITHUB_BRANCH", "FLEET_APPS_JSON",
            "GH_RUNS", "GH_RUN_VIEW", "GH_RUN_LIST_FAIL", "GH_RUN_VIEW_FAIL")

GH_STUB = r"""#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "$CALLLOG"
case "$1 $2" in
  "run list")
    [ "${GH_RUN_LIST_FAIL:-0}" = 1 ] && { echo 'gh: not logged in' >&2; exit 1; }
    printf '%s' "${GH_RUNS:-[]}"; exit 0 ;;
  "run view")
    [ "${GH_RUN_VIEW_FAIL:-0}" = 1 ] && { echo 'gh: run gone' >&2; exit 1; }
    if [ -n "${GH_RUN_VIEW:-}" ]; then printf '%s' "$GH_RUN_VIEW"
    else printf '%s' '{"jobs":[]}'
    fi; exit 0 ;;
esac
exit 1
"""

RAN = {"name": "test", "conclusion": "failure", "startedAt": "2026-10-13T09:00:00Z"}
NEVER = {"name": "test", "conclusion": "failure", "startedAt": None}


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {key: os.environ.get(key) for key in ENV_KEYS}
        # Everything except PATH is stripped; PATH is *replaced* below (stub first) rather than
        # emptied, or the stub could never be found.
        for key in ENV_KEYS:
            if key != "PATH":
                os.environ.pop(key, None)
        self.tmp = Path(tempfile.mkdtemp(prefix="github-ci-test."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        self.calllog = self.tmp / "calls"
        stub = self.bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        os.environ["PATH"] = f"{self.bindir}:{os.environ['PATH']}"
        os.environ["CALLLOG"] = str(self.calllog)
        os.environ["FACTORY_TRACKER_OWNER"] = "example.com"
        factory_config.reset()

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        factory_config.reset()

    def calls(self) -> list[str]:
        if not self.calllog.exists():
            return []
        return [line for line in self.calllog.read_text().splitlines() if line.strip()]


class TheClassifier(Harness):
    """Pure: the run and its jobs in, a verdict and its reason out."""

    def test_a_run_in_flight_is_running(self):
        verdict, why = github_ci.classify({"databaseId": 7, "status": "in_progress",
                                           "conclusion": None}, [])
        self.assertEqual(verdict, github_ci.RUNNING)
        self.assertIn("in_progress", why)

    def test_a_successful_run_is_green(self):
        verdict, _ = github_ci.classify({"databaseId": 7, "status": "completed",
                                         "conclusion": "success"}, [])
        self.assertEqual(verdict, github_ci.GREEN)

    def test_a_job_that_ran_and_failed_is_a_code_defect(self):
        verdict, why = github_ci.classify({"databaseId": 7, "status": "completed",
                                           "conclusion": "failure"}, [RAN])
        self.assertEqual(verdict, github_ci.RED)
        self.assertIn("this IS a code defect", why)

    def test_a_job_that_never_started_is_blocked_and_not_a_code_defect(self):
        verdict, why = github_ci.classify({"databaseId": 7, "status": "completed",
                                           "conclusion": "failure"}, [NEVER])
        self.assertEqual(verdict, github_ci.BLOCKED)
        self.assertIn("no runner ever picked the work up", why)
        self.assertNotIn("code defect", why.replace("not evidence of a code defect", ""))

    def test_a_mixed_run_is_red_and_says_what_was_not_verified(self):
        verdict, why = github_ci.classify({"databaseId": 7, "status": "completed",
                                           "conclusion": "failure"}, [RAN, NEVER])
        self.assertEqual(verdict, github_ci.RED)
        self.assertIn("AND 1 never started", why)

    def test_a_cancelled_run_is_not_a_pass(self):
        verdict, why = github_ci.classify({"databaseId": 7, "status": "completed",
                                           "conclusion": "cancelled"}, [])
        self.assertEqual(verdict, github_ci.NONE)
        self.assertIn("NOT verified", why)

    def test_a_failed_run_with_no_visible_failed_job_is_unknown(self):
        verdict, why = github_ci.classify({"databaseId": 7, "status": "completed",
                                           "conclusion": "failure"}, [])
        self.assertEqual(verdict, github_ci.UNKNOWN)
        self.assertIn("gap in what GitHub returned", why)

    def test_an_unclassifiable_run_is_unknown(self):
        verdict, _ = github_ci.classify({"databaseId": 7, "status": "completed",
                                         "conclusion": "mystery"}, [])
        self.assertEqual(verdict, github_ci.UNKNOWN)


class TheReader(Harness):
    """The argv, the two-row shape, and the none/unreachable distinction."""

    def test_the_argv_and_the_row(self):
        os.environ["GH_RUNS"] = json.dumps([{"databaseId": 42, "status": "completed",
                                             "conclusion": "failure",
                                             "headSha": "abcdef1234567890"}])
        os.environ["GH_RUN_VIEW"] = json.dumps({"jobs": [RAN]})
        row = github_ci.repo_verdict("portal")
        self.assertEqual(row["verdict"], github_ci.RED)
        self.assertEqual(row["pipeline"], 42)
        self.assertEqual(row["sha"], "abcdef12")
        self.assertEqual(row["branch"], "main")
        self.assertEqual(self.calls()[0],
                         "gh run list --repo example.com/portal --branch main --limit 1 "
                         "--json databaseId,status,conclusion,headSha")
        self.assertEqual(self.calls()[1], "gh run view 42 --repo example.com/portal --json jobs")

    def test_no_run_at_all_is_none_not_unknown(self):
        os.environ["GH_RUNS"] = "[]"
        row = github_ci.repo_verdict("portal")
        self.assertEqual(row["verdict"], github_ci.NONE)
        self.assertIn("never been built", row["why"])

    def test_an_unreadable_tracker_is_unknown(self):
        os.environ["GH_RUN_LIST_FAIL"] = "1"
        row = github_ci.repo_verdict("portal")
        self.assertEqual(row["verdict"], github_ci.UNKNOWN)

    def test_unreadable_jobs_cannot_tell_the_two_reds_apart(self):
        os.environ["GH_RUNS"] = json.dumps([{"databaseId": 42, "status": "completed",
                                             "conclusion": "failure", "headSha": "abc"}])
        os.environ["GH_RUN_VIEW_FAIL"] = "1"
        row = github_ci.repo_verdict("portal")
        self.assertEqual(row["verdict"], github_ci.UNKNOWN)
        self.assertIn("cannot be told apart", row["why"])

    def test_a_missing_gh_is_unknown_not_a_pass(self):
        saved = github_ci._gh
        github_ci._gh = lambda: None
        self.addCleanup(setattr, github_ci, "_gh", saved)
        row = github_ci.repo_verdict("portal")
        self.assertEqual(row["verdict"], github_ci.UNKNOWN)
        self.assertIn("not on PATH", row["why"])

    def test_the_branch_is_configuration(self):
        os.environ["FLEET_GITHUB_BRANCH"] = "release"
        os.environ["GH_RUNS"] = "[]"
        row = github_ci.repo_verdict("portal")
        self.assertEqual(row["branch"], "release")
        self.assertIn("no workflow run on release", row["why"])
        self.assertIn("--branch release", self.calls()[0])


class TheCLI(Harness):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True, env=dict(os.environ))

    def test_json_names_every_verdict(self):
        os.environ["GH_RUNS"] = json.dumps([{"databaseId": 42, "status": "completed",
                                             "conclusion": "failure", "headSha": "abcdef123"}])
        os.environ["GH_RUN_VIEW"] = json.dumps({"jobs": [NEVER]})
        run = self.run_cli("--json", "--repo", "portal")
        self.assertEqual(run.returncode, 0, run.stderr)
        doc = json.loads(run.stdout)
        self.assertEqual(doc["branch"], "main")
        self.assertEqual(doc["repos"][0]["verdict"], "BLOCKED")
        self.assertEqual(doc["repos"][0]["repo"], "portal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
