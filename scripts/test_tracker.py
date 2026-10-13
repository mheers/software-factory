#!/usr/bin/env python3
"""The tracker adapter: the exact argv of both flavors, and the two refusals.

What these tests hold is not "an issue was created". It is:

- the **argv** the provider builds — a stub `gh`/`glab` on `PATH` logs every call, so a flag
  that creeps in or a label that gets reordered is a failing line, not a runtime surprise
  (the mutation the GitLab stub was built for: `glab mr list --state opened` shipped and 27
  permissive tests stayed green);
- the **normalisation** — `gh` spells issue states `OPEN`/`CLOSED`, `glab` spells them
  `opened`/`closed`, and the callers compare against `glab`'s spelling;
- the **refusals** — a missing CLI and an answer that does not parse are `unknown`/`None`, never
  a verdict, and the CLI's exit code is 2.

Run: `python3 scripts/test_tracker.py`. Stubs through `PATH`; no network.
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
SCRIPT = HERE / "tracker.py"
sys.path.insert(0, str(HERE))

import factory_config  # noqa: E402
import tracker  # noqa: E402

ENV_KEYS = ("PATH", "CALLLOG", "FACTORY_TRACKER_PROVIDER", "FACTORY_TRACKER_OWNER",
            "FACTORY_CONFIG", "FACTORY_META_REPO", "FLEET_GITLAB_GROUP",
            "GH_ISSUES", "GH_STATE", "GH_STATE_RAW", "GH_CREATE_URL", "GH_CREATE_FAIL",
            "GH_VIEW_FAIL", "GH_PR_FAIL", "GH_PRS",
            "GL_ISSUES", "GL_STATE", "GL_API_RAW", "GL_CREATE_URL", "GL_API_FAIL",
            "GL_MR_FAIL", "GL_MRS")

GH_STUB = r"""#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "$CALLLOG"
case "$1 $2" in
  "issue list")
    [ "${GH_LIST_FAIL:-0}" = 1 ] && exit 1
    printf '%s' "${GH_ISSUES:-[]}"; exit 0 ;;
  "issue create")
    [ "${GH_CREATE_FAIL:-0}" = 1 ] && { echo 'create failed' >&2; exit 1; }
    printf '%s\n' "${GH_CREATE_URL:-https://example.com/issues/1}"; exit 0 ;;
  "issue view")
    [ "${GH_VIEW_FAIL:-0}" = 1 ] && exit 1
    if [ -n "${GH_STATE_RAW:-}" ]; then printf '%s' "$GH_STATE_RAW"
    else printf '{"state":"%s"}' "${GH_STATE:-OPEN}"
    fi; exit 0 ;;
  "pr list")
    [ "${GH_PR_FAIL:-0}" = 1 ] && exit 1
    printf '%s' "${GH_PRS:-[]}"; exit 0 ;;
esac
exit 1
"""

GLAB_STUB = r"""#!/usr/bin/env bash
printf 'glab %s\n' "$*" >> "$CALLLOG"
case "$1 $3 $4" in
  "-R issue list")
    printf '%s' "${GL_ISSUES:-[]}"; exit 0 ;;
  "-R issue create")
    printf '%s\n' "${GL_CREATE_URL:-https://example.com/issues/1}"; exit 0 ;;
esac
case "$1" in
  api)
    [ "${GL_API_FAIL:-0}" = 1 ] && exit 1
    if [ -n "${GL_API_RAW:-}" ]; then printf '%s' "$GL_API_RAW"
    else printf '{"state":"%s"}\n' "${GL_STATE:-opened}"
    fi; exit 0 ;;
  mr)
    [ "${GL_MR_FAIL:-0}" = 1 ] && exit 1
    printf '%s' "${GL_MRS:-[]}"; exit 0 ;;
esac
exit 1
"""


class Harness(unittest.TestCase):
    """A temp `PATH` with stub `gh` and `glab`, and a call log both write to."""

    def setUp(self) -> None:
        self.saved = {key: os.environ.get(key) for key in ENV_KEYS}
        self.tmp = Path(tempfile.mkdtemp(prefix="tracker-test."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        self.calllog = self.tmp / "calls"
        for name, body in (("gh", GH_STUB), ("glab", GLAB_STUB)):
            stub = self.bindir / name
            stub.write_text(body)
            stub.chmod(0o755)
        os.environ["PATH"] = f"{self.bindir}:{os.environ['PATH']}"
        os.environ["CALLLOG"] = str(self.calllog)
        os.environ.setdefault("FACTORY_TRACKER_OWNER", "example.com")
        for key in ENV_KEYS:
            if key not in ("PATH", "CALLLOG", "FACTORY_TRACKER_OWNER"):
                os.environ.pop(key, None)
        # The classes asserting GitHub argv and answer shapes pin `github`; `TheGitLabFlavor`
        # overrides it below. The *default* provider (`gitlab`) is asserted in
        # `test_factory_config.py`, where the shipped defaults live.
        os.environ["FACTORY_TRACKER_PROVIDER"] = "github"
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

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True, env=dict(os.environ))


class TheGitHubProvider(Harness):
    def test_open_issues_asks_the_mirror_of_the_gitlab_argv(self):
        os.environ["GH_ISSUES"] = json.dumps([{"title": "main is red: meta abc123"},
                                              {"title": "something else"}])
        issues = tracker.open_issues("portal")
        self.assertEqual([i["title"] for i in issues],
                         ["main is red: meta abc123", "something else"])
        self.assertEqual(self.calls()[0],
                         "gh issue list --repo example.com/portal --state all --limit 100 "
                         "--json title")

    def test_file_issue_passes_every_label_separately(self):
        os.environ["GH_CREATE_URL"] = "https://example.com/issues/7"
        url = tracker.file_issue("portal", "main is red: x", "the body", 
                                 ["type::bug", "sev::high", "status::todo"])
        self.assertEqual(url, "https://example.com/issues/7")
        call = self.calls()[0]
        self.assertIn("gh issue create --repo example.com/portal --title main is red: x", call)
        self.assertIn("--body the body", call)
        self.assertIn("--label type::bug --label sev::high --label status::todo", call)

    def test_file_issue_reports_none_when_the_tracker_refuses(self):
        os.environ["GH_CREATE_FAIL"] = "1"
        self.assertIsNone(tracker.file_issue("portal", "t", "b", ["type::bug"]))

    def test_issue_state_maps_the_github_spelling(self):
        os.environ["GH_STATE"] = "OPEN"
        self.assertEqual(tracker.issue_state("7"), "opened")
        os.environ["GH_STATE"] = "CLOSED"
        self.assertEqual(tracker.issue_state("7"), "closed")
        self.assertEqual(self.calls()[0], "gh issue view 7 --json state")

    def test_mr_count_counts_open_pull_requests(self):
        os.environ["GH_PRS"] = json.dumps([{"number": 1}, {"number": 2}])
        self.assertEqual(tracker.mr_count("issue-7-fix"), 2)
        self.assertEqual(self.calls()[0],
                         "gh pr list --head issue-7-fix --state open --json number")


class TheGitLabFlavor(Harness):
    """The flavor that shipped first keeps its exact argv — the sweep's stub is strict."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["FACTORY_TRACKER_PROVIDER"] = "gitlab"
        factory_config.reset()

    def test_open_issues_keeps_the_glab_argv(self):
        os.environ["GL_ISSUES"] = json.dumps([{"title": "main is red: meta abc123"}])
        issues = tracker.open_issues("portal")
        self.assertEqual(len(issues), 1)
        self.assertEqual(self.calls()[0],
                         "glab -R example.com/portal issue list --all --per-page 100 "
                         "--output json")

    def test_file_issue_keeps_the_glab_argv_and_label_order(self):
        url = tracker.file_issue("portal", "main is red: x", "the body",
                                 ["type::bug", "sev::high", "status::todo"])
        self.assertEqual(url, "https://example.com/issues/1")
        call = self.calls()[0]
        self.assertIn("glab -R example.com/portal issue create --title main is red: x", call)
        self.assertIn("--label type::bug --label sev::high --label status::todo", call)
        self.assertIn("--description the body", call)

    def test_issue_state_uses_the_project_id_read(self):
        os.environ["GL_STATE"] = "opened"
        self.assertEqual(tracker.issue_state("7"), "opened")
        self.assertEqual(self.calls()[0], "glab api projects/:id/issues/7")

    def test_mr_count_uses_the_source_branch_read(self):
        os.environ["GL_MRS"] = json.dumps([{"iid": 1}])
        self.assertEqual(tracker.mr_count("issue-7-fix"), 1)
        self.assertEqual(self.calls()[0],
                         "glab mr list --source-branch issue-7-fix -F json")


class TheRefusals(Harness):
    def test_a_missing_cli_is_not_a_verdict(self):
        saved = tracker._tool
        tracker._tool = lambda name: None
        self.addCleanup(setattr, tracker, "_tool", saved)
        self.assertEqual(tracker.issue_state("7"), "unknown")
        self.assertIsNone(tracker.mr_count("b"))
        self.assertIsNone(tracker.file_issue("portal", "t", "b", []))
        self.assertEqual(tracker.open_issues("portal"), [])

    def test_an_unparsable_answer_is_not_a_verdict(self):
        os.environ["GH_STATE_RAW"] = "not json"
        self.assertEqual(tracker.issue_state("7"), "unknown")
        os.environ["GH_ISSUES"] = "not json"
        self.assertEqual(tracker.open_issues("portal"), [])

    def test_an_unreachable_tracker_is_not_a_verdict(self):
        os.environ["GH_VIEW_FAIL"] = "1"
        self.assertEqual(tracker.issue_state("7"), "unknown")
        os.environ["GH_PR_FAIL"] = "1"
        self.assertIsNone(tracker.mr_count("b"))

    def test_an_unknown_provider_refuses_rather_than_guessing(self):
        os.environ["FACTORY_TRACKER_PROVIDER"] = "mercurial"
        factory_config.reset()
        with self.assertRaises(SystemExit) as caught:
            tracker.issue_state("7")
        self.assertEqual(caught.exception.code, 2)


class TheCLI(Harness):
    def test_state_prints_the_state_and_exits_0(self):
        os.environ["GH_STATE"] = "CLOSED"
        run = self.run_cli("state", "--iid", "42")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "closed")

    def test_state_prints_unknown_and_exits_2(self):
        os.environ["GH_VIEW_FAIL"] = "1"
        run = self.run_cli("state", "--iid", "42")
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout.strip(), "unknown")

    def test_mr_count_prints_the_number_and_exits_0(self):
        os.environ["GH_PRS"] = json.dumps([{"number": 3}])
        run = self.run_cli("mr-count", "--branch", "issue-9-x")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "1")

    def test_mr_count_prints_unknown_and_exits_2(self):
        os.environ["GH_PR_FAIL"] = "1"
        run = self.run_cli("mr-count", "--branch", "issue-9-x")
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout.strip(), "unknown")

    def test_provider_prints_the_configured_flavor(self):
        run = self.run_cli("provider")
        self.assertEqual(run.stdout.strip(), "github")
        os.environ["FACTORY_TRACKER_PROVIDER"] = "gitlab"
        factory_config.reset()
        run = self.run_cli("provider")
        self.assertEqual(run.stdout.strip(), "gitlab")


if __name__ == "__main__":
    unittest.main(verbosity=2)
