#!/usr/bin/env python3
"""Tests for the keepalive's never-started behaviour: never-started workers, and GitLab CI.

Run: `python3 scripts/test_fleet_keepalive.py` (no test runner, no network, no sleeps).

## What is held here

1. A pane that has never run a turn is re-prompted **on the first sighting**, not after
   `--stall` unchanged checks — and the reason it is separate from the stall nudge is that a
   stall is a duration while this state has no duration to measure.
2. A red `main` is reported as a fault; a `BLOCKED` `main` (no compute minutes) is reported
   under its own heading and **not** counted as a fault.
3. The reassuring sentence is not printed while a worker never started or `main` is red, and when
   it is printed it names what it checked.
4. **A declared CI state relocates a line and never clears the report.** Two classes, and both
   exist because of the same argument — a declaration is a *claim*, so the mechanism that reads it
   is exactly the mechanism a coordinator could misuse to silence a monitor:
   - `ADeclaredStateRelocatesAndNeverClears` — declaring everything known must still not produce
     an all-clear, declared states are still reported and counted, and the file is data.
   - `AStaleDeclarationIsWorseThanNoDeclaration` — a declaration that no longer matches is
     reported, and one that turned into a real `RED` is **louder** than no declaration at all.

## Why it drives the real script

A Python copy of the decision loop would pass while `main()` stayed broken, which is the probe
that was never a test — and has happened twice in this project's history. So these run
`fleet-keepalive.py` as a subprocess against a stub `herdr` and a stub `fleet-watch.py`, and read
what it actually prompted.

**The real `gitlab_ci.py` is copied in beside it, not stubbed.** Stubbing the verdict would make
every assertion about the two reds a claim about a Python literal rather than about the
classifier; `scripts/test_gitlab_ci.py` holds the classifier's own cases against payloads
recorded from the live group.

**The `glab` stub is strict**, for the reason the one in `test_cleanup_workers.py` was rewritten:
that stub accepted any argument and shipped `glab mr list --state opened` — a flag the real CLI
rejects — through 27 of 27 green tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "fleet-keepalive.py"


def sweep_run(repos=("meta",), kind="green"):
    """One recorded main-sweep run, in the shape `main_sweep_state.write_run()` writes.

    The default is a **fresh, green** record, because that is the normal operating
    state: cron records a run hourly and the keepalive reads the last one. A harness that shipped
    no record would make every all-clear test an assertion about the missing record rather than
    about the thing the test names — so the tests that want the sweep dimension ask for it.
    """
    now = time.time()
    if kind == "stale":
        run_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 60 * 60))
        kind = "green"
    else:
        run_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    rows = []
    for repo in repos:
        first = repo == repos[0]
        if kind == "red" and first:
            # The red log: a red row names the full-output file written beside the record, so the row in
            # the report is a pointer to the failure itself and not only to a 200-char summary.
            rows.append({"repo": repo, "verdict": "RED", "commit": "deadbee1234",
                         "key": f"{repo}@deadbee1234",
                         "detail": "guard_test.go:112: synthetic main failure",
                         "log": f"/synthetic/.fleet-state/main-sweep-red-{repo}-deadbee1234.log"})
        elif kind == "inconsistent" and first:
            rows.append({"repo": repo, "verdict": "TOOL-INCONSISTENT", "commit": "deadbee1234",
                         "key": "", "detail": "the gate reported GREEN with no gate on disk"})
        elif kind == "nogate" and first:
            rows.append({"repo": repo, "verdict": "NO-GATE", "commit": "deadbee1234", "key": "",
                         "detail": "no go module, no Makefile and no check-agents-md.sh"})
        else:
            rows.append({"repo": repo, "verdict": "GREEN", "commit": "6f1a2b3c",
                         "key": "", "detail": "make check passed"})
    return {"run_at": run_at, "dry_run": True, "rows": rows if kind != "empty" else []}


def pane(name="wPE:p1", verdict="NOTHING", ahead=0, dirty=0, status="idle", progress="STARTED",
         branch="issue-24-thing", worktree="/tmp/wt/thing", progress_why="144.6K tokens"):
    """One `fleet-watch.py --json` row, in the shape the sweep emits.

    `progress` and `progress_why` are the never-started fields and default to a pane that HAS worked, so a
    test that is about something else cannot accidentally be about a never-started worker.
    """
    return {
        "pane": name, "branch": branch, "worktree": worktree, "verdict": verdict,
        "ahead": ahead, "dirty": dirty, "behind": 0, "status": status,
        "progress": progress, "progress_why": progress_why,
        "why": f"synthetic row for {verdict}",
    }


# The two job payloads recorded from the live group. Kept as the same shapes `test_gitlab_ci.py`
# holds, and for the same reason: a discriminator tested against an invented payload tests the
# test's imagination. `ci_quota_exceeded` + `started_at: null` is the current family-wide state;
# `script_failure` + a duration is what a real failure looks like.
QUOTA_JOBS = json.dumps([
    {"id": 1, "name": "lint", "status": "failed", "failure_reason": "ci_quota_exceeded",
     "started_at": None, "duration": None, "runner": None},
    {"id": 2, "name": "test", "status": "failed", "failure_reason": "ci_quota_exceeded",
     "started_at": None, "duration": None, "runner": None},
])
RAN_JOBS = json.dumps([
    {"id": 1, "name": "lint", "status": "failed", "failure_reason": "script_failure",
     "started_at": "2026-10-03T20:03:24.100Z", "duration": 19.99, "runner": {"id": 7}},
])
GREEN_JOBS = json.dumps([
    {"id": 1, "name": "lint", "status": "success", "failure_reason": None,
     "started_at": "2026-10-05T17:00:00.000Z", "duration": 18.2, "runner": {"id": 7}},
])

GLAB_STUB = r"""#!/usr/bin/env bash
# Strict about its surface, like the real CLI. `glab api` is the only way this project reads
# pipeline state; the stub in test_cleanup_workers.py answered anything, which is how
# `glab mr list --state opened` shipped through 27 of 27 green tests.
#
# An unhandled request prints `Unknown endpoint` and exits 1, which is what makes a broken
# invocation loud instead of plausible: `gitlab_ci.py` turns a non-zero exit into `UNKNOWN` with
# the message attached, so a typo shows up as "cannot be read" rather than as a verdict.
if [ "$1" != "api" ]; then
  printf 'Unknown command: %s\n' "$1" >&2
  exit 2
fi

# PER REPOSITORY FIXTURES. One CI_FIXTURE for a whole run could only express one verdict at
# a time, so every declaration-mechanism test would have had to isolate a single repository to say anything — and the
# claim under test is about a declaration applied to a *set* of them. The path is
# `projects/example.com%2F<repo>/pipelines?...`, so the repository name is what follows
# the last encoded separator; a flat `<repo>=<fixture>` file is read for it and CI_FIXTURE stays
# the fallback, which is why every earlier fixture is unchanged.
REPO=""
case "$2" in
  *"example.com%2F"*) REPO="${2#*example.com%2F}"; REPO="${REPO%%/*}" ;;
esac
FIXTURE="$CI_FIXTURE"
if [ -n "$REPO" ] && [ -f "$REPO_FIXTURES" ]; then
  # `while ... done < file` runs in the CURRENT shell in bash, so FIXTURE survives the loop. An
  # `if` rather than `[ ... ] && FIXTURE=...` so the body never ends on a false test status.
  while IFS='=' read -r NAME FX; do
    case "$NAME" in ''|\#*) continue ;; esac
    if [ "$NAME" = "$REPO" ]; then FIXTURE="$FX"; fi
  done < "$REPO_FIXTURES"
fi

# The patterns are QUOTED where they contain `&`. An unquoted `&` in a `case` arm is a shell
# syntax error, and the symptom is a stub that fails every invocation — which reads as "GitLab is
# unreadable" and quietly turns every test into an assertion about the stub. Quoting also stops
# `?` acting as a glob, which is what makes the arm match the literal query string.
case "$2" in
  *"pipelines?ref=main&per_page=1"*)
      case "$FIXTURE" in
        green)  cat "$PIPELINE_GREEN" ;;
        red)    cat "$PIPELINE_FAILED" ;;
        blocked) cat "$PIPELINE_FAILED" ;;
        none)   printf '[]' ;;
        unreadable) printf '404 Project Not Found\n' >&2; exit 1 ;;
        *) printf 'unknown CI fixture: %s\n' "$FIXTURE" >&2; exit 2 ;;
      esac ;;
  *"/pipelines/700/jobs?"*) cat "$JOBS_GREEN" ;;
  *"/pipelines/701/jobs?"*)
      case "$FIXTURE" in
        red)     cat "$JOBS_RAN" ;;
        blocked) cat "$JOBS_QUOTA" ;;
        *) printf 'no jobs fixture for %s\n' "$FIXTURE" >&2; exit 2 ;;
      esac ;;
  *) printf 'Unknown endpoint: %s\n' "$2" >&2; exit 1 ;;
esac
exit 0
"""


class KeepaliveRun:
    """One real invocation of `fleet-keepalive.py` against a synthetic fleet and CI state."""

    def __init__(self, case, panes, *, ci="green", args=(), manifest=None, state=None,
                 agent_status="idle", repos=("meta",), per_repo_ci=None, declared=None,
                 declared_raw=None, sweep="green", sweep_raw=None):
        self.ci = ci
        self.agent_status = agent_status
        self.tmp = Path(tempfile.mkdtemp(prefix="keepalive-test."))
        case.addCleanup(shutil.rmtree, self.tmp, True)

        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        glab = self.bindir / "glab"
        glab.write_text(GLAB_STUB)
        glab.chmod(0o755)

        self.prompt_log = self.tmp / "prompts.log"
        # The agent row is written as JSON from Python rather than assembled inside the shell
        # script. Brace-counting by hand inside `printf` is how the first version of this stub
        # emitted one `}` too many — and the symptom was not a parse error, it was
        # `agent_alive()` answering False, so the keepalive took the *restart* path and every
        # test paid a 20-second sleep for a fixture that had silently changed meaning.
        agent_json = self.tmp / "agent.json"
        agent_json.write_text(json.dumps({"result": {"agent": {
            "agent_status": agent_status,
            "agent_session": {"value": "ses_synthetic"},
        }}}))
        herdr = self.bindir / "herdr"
        # `agent get` answers with a status AND a session, because `agent_alive()` reads both —
        # a stub returning only `agent_status` would exercise a different function.
        herdr.write_text(
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            f'"agent get") cat "{agent_json}" ;;\n'
            f'"agent prompt") shift 2; printf "%s\\n" "$*" >> "{self.prompt_log}" ;;\n'
            '"agent start") : ;;\n'
            "*) exit 0 ;;\n"
            "esac\n")
        herdr.chmod(0o755)

        # The real `gitlab_ci.py`, beside the keepalive — which resolves it as a sibling. Copied
        # rather than stubbed so the two-reds behaviour under test is the classifier's.
        self.scripts = self.tmp / "scripts"
        self.scripts.mkdir()
        shutil.copy(SCRIPT, self.scripts / "fleet-keepalive.py")
        # Under its REAL name. Copying it to `gitlab_ci.py` made the fixture agree with a
        # caller that was itself wrong, so the production path was never exercised: the
        # keepalive reported "GitLab could not be read" on every run and its own tests
        # were green. A test that reconstructs the filename it is testing is a test
        # asserting itself.
        shutil.copy(HERE / "gitlab_ci.py", self.scripts / "gitlab_ci.py")
        # The shared declaration reader, beside it for the same reason. `fleet-keepalive.py` imports
        # `expected_ci.py` by path, so a harness that copied only the keepalive would fail at import
        # — and a harness that shipped its *own* copy would be asserting against the fixture rather
        # than the module both scripts share.
        shutil.copy(HERE / "expected_ci.py", self.scripts / "expected_ci.py")
        # And the shared main-sweep reader, which the keepalive imports the same way and
        # which is what turns the sweep's record into the report's red section.
        shutil.copy(HERE / "main_sweep_state.py", self.scripts / "main_sweep_state.py")
        # And the configuration seam every one of them resolves through, for the same reason:
        # imported by path, so the fixture directory must carry it.
        shutil.copy(HERE / "factory_config.py", self.scripts / "factory_config.py")
        # The repository set, so a test can hold a `NONE` and a `BLOCKED` repository in one run.
        # `repos()` always appends `meta` and de-duplicates, so naming it here is a
        # no-op rather than a second row — which is why the single-repository default is safe and
        # every earlier count in this file still means "the whole survey".
        self.apps = self.tmp / "apps.json"
        self.apps.write_text(json.dumps({"apps": [{"id": n, "repo": n} for n in repos]}))

        # A stub fleet-watch.py. Named `watch_stub` and not `sweep`: `sweep` is the sweep-record parameter
        # naming this run's main-sweep *record*, and a local with the same name shadows it —
        # which it did, silently, until a test looked for a red row.
        watch_stub = self.bindir / "fleet-watch.py"
        watch_stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps({{'panes': json.loads({json.dumps(json.dumps(panes))})}}))\n")
        watch_stub.chmod(0o755)
        shutil.copy(watch_stub, self.scripts / "fleet-watch.py")

        self.state = self.tmp / "keepalive.json"
        if state is not None:
            self.state.write_text(json.dumps(state))
        self.manifest = self.tmp / "fleet.json"
        self.manifest.write_text(json.dumps(manifest
                                            or {p["worktree"]: p["pane"] for p in panes}))

        self.fixtures = {
            "PIPELINE_GREEN": json.dumps([{"id": 700, "status": "success", "ref": "main",
                                           "sha": "aaaa1111bbbb2222cccc3333dddd4444eeee5555"}]),
            "PIPELINE_FAILED": json.dumps([{"id": 701, "status": "failed", "ref": "main",
                                            "sha": "ffff1111bbbb2222cccc3333dddd4444eeee5555"}]),
            "JOBS_GREEN": GREEN_JOBS, "JOBS_RAN": RAN_JOBS, "JOBS_QUOTA": QUOTA_JOBS,
        }
        for name, body in self.fixtures.items():
            (self.tmp / name).write_text(body)

        # Per-repository CI verdicts. Absent by default, so `CI_FIXTURE` answers every request and
        # the earlier single-repository fixtures behave exactly as before.
        self.repo_fixtures = self.tmp / "repo-fixtures"
        if per_repo_ci:
            self.repo_fixtures.write_text("".join(f"{k}={v}\n" for k, v in per_repo_ci.items()))

        # The declared-CI state file. `declared_raw` writes bytes verbatim so a test can present
        # the file malformed, which is a different fact from declaring nothing and is reported
        # differently. `declared` writes the well-formed shape. Writing neither is the ordinary
        # case: no file means nothing declared, and the path points into this run's own temporary
        # directory so a file left behind by another run cannot leak in.
        self.expected_ci = self.tmp / "expected-ci.json"
        if declared_raw is not None:
            self.expected_ci.write_text(declared_raw)
        elif declared is not None:
            self.expected_ci.write_text(json.dumps(
                {"_comment": "synthetic", "repositories": declared}))

        # The main sweep's record. `sweep="green"` is the ordinary operating state — the
        # sweep ran recently and found nothing red — so every earlier fixture behaves as it did.
        # `sweep=None` writes no file at all (the sweep has never run), `sweep_raw` writes bytes
        # verbatim, and the named kinds write what `main_sweep_state.write_run()` produces.
        self.main_sweep = self.tmp / "main-sweep.json"
        if sweep_raw is not None:
            self.main_sweep.write_text(sweep_raw)
        elif sweep is not None:
            self.main_sweep.write_text(json.dumps(sweep_run(repos, sweep)))

        self.env = {
            **os.environ,
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            # The fixtures stage and stub the GitLab flavor; the provider pins the reader that
            # these tests were written for (the GitHub reader has its own test file).
            "FACTORY_TRACKER_PROVIDER": "gitlab",
            "FLEET_KEEPALIVE_STATE": str(self.state),
            "FLEET_KEEPALIVE_LOG": str(self.tmp / "keepalive.log"),
            "FLEET_MANIFEST": str(self.manifest),
            "FLEET_EXPECTED_RED": str(self.tmp / "expected-red.json"),
            "FLEET_EXPECTED_CI": str(self.expected_ci),
            "FLEET_MAIN_SWEEP": str(self.main_sweep),
            "FLEET_APPS_JSON": str(self.apps),
            "CI_FIXTURE": self.ci,
            "REPO_FIXTURES": str(self.repo_fixtures),
            **{name: str(self.tmp / name) for name in self.fixtures},
        }
        self.args = list(args)

    def run(self, *extra):
        self.proc = subprocess.run(
            [sys.executable, str(self.scripts / "fleet-keepalive.py"), *self.args, *extra],
            capture_output=True, text=True, env=self.env, timeout=120)
        # Every run asserts its own exit status, rather than leaving it to whichever test happens
        # to assert on the affected line.
        #
        # This was found the hard way while writing the declaration mechanism: the summary line is logged LAST, so a
        # `TypeError` in the final statement still produced the whole report on stdout, and the
        # only test looking at that line caught it by luck. A crash anywhere else would have been
        # invisible — `out` concatenates stderr, so a traceback reads as more report text, and no
        # assertion anywhere looks for one. That is the probe that was never a test: a check that
        # only runs if some other assertion happens to cover the same characters.
        if self.proc.returncode != 0:
            raise AssertionError(
                f"fleet-keepalive.py exited {self.proc.returncode}; the report below is therefore "
                f"truncated at the point it died, and any assertion about text after that point "
                f"would be passing on a run that failed:\n{self.proc.stdout}\n{self.proc.stderr}")
        if "Traceback (most recent call last)" in self.proc.stderr:
            raise AssertionError(f"fleet-keepalive.py raised:\n{self.proc.stderr}")
        return self

    @property
    def out(self):
        return self.proc.stdout + self.proc.stderr

    @property
    def prompts(self):
        p = self.prompt_log
        return p.read_text() if p.exists() else ""


def section(out, heading):
    """The lines under `heading` in a keepalive log, with the timestamp prefix stripped.

    Three declaration-mechanism assertions are about *where a line sits* — "a declared state must be RELOCATED,
    never merged into another verdict" — and that needs a real section reader rather than a
    substring check.

    The first version of this used `out.split(heading, 1)[1].split("\\n\\n", 1)[0]`, and it passed
    for the wrong reason twice: `log()` prefixes **every** line with a timestamp, so the log never
    contains a blank line and the split returned the whole rest of the report. Both relocation
    assertions were then satisfied by a section that came later in the file. A delimiter that
    never matches is a delimiter that cannot fail.

    So: strip the `<timestamp>  ` prefix first — which is what turns the blank separator line
    into an empty string — then split on empty lines. A heading that is absent returns `""`, so a
    test asserting on a section that was never printed fails rather than reading the whole report.
    """
    # `\d`, not `\\d`: an over-escaped pattern compiles and matches nothing, so every line keeps
    # its prefix, the separator line never reads as empty and `section()` returns "". It would
    # then have failed every relocation assertion loudly rather than silently — but only by luck,
    # and for a reason that has nothing to do with the property under test. Probe the helper
    # before trusting it.
    stripped = [re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  ", "", line)
                for line in out.splitlines()]
    body, capturing = [], False
    for line in stripped:
        if capturing:
            if not line.strip():
                break
            body.append(line)
        elif line.startswith(heading):
            capturing = True
    return "\n".join(body)


class ANeverStartedWorkerIsRePrompted(unittest.TestCase):
    """THE property for the never-started rule. The pane that produced nothing is acted on at once."""

    def test_it_is_reprompted_on_the_first_sighting(self):
        """`--stall 8`, and still prompted on run one.

        The stall rule needs eight *unchanged* checks — four hours — and it is built for a worker
        that was doing something. Applied to a pane that never ran a turn it waits for something
        that cannot happen on its own, because nothing has begun.
        """
        r = KeepaliveRun(self, [pane(verdict="NOT-STARTED", progress="NOT-STARTED")],
                         args=["--stall", "8"]).run()
        self.assertIn("wPE:p1", r.prompts,
                      f"a worker that never started must be told to begin: {r.prompts!r}\n{r.out}")
        self.assertIn("you have not run a turn", r.prompts,
                      f"and told what the keepalive observed: {r.prompts!r}")
        print("    --stall 8, never-started -> re-prompted on run 1")

    def test_and_the_nudge_is_not_the_stall_nudge(self):
        """A stalled worker has been working and stopped. This one has not begun.

        The two need different messages: the stall nudge asks "are you blocked?", which is a
        question for a worker that exists; this one says the brief never registered and points at
        where the brief is. Sending the stall nudge to a pane that never started is a message to
        nobody — which is how this state can sit through eight checks and then be called healthy.
        """
        r = KeepaliveRun(self, [pane(verdict="NOT-STARTED", progress="NOT-STARTED")]).run()
        self.assertIn("never registered", r.prompts,
                      f"the prompt must say the brief did not land: {r.prompts!r}")
        self.assertIn("glab issue view 24", r.prompts,
                      f"and route it to the ticket, with the iid read off the branch: {r.prompts!r}")
        self.assertNotIn("name the blocker in one line", r.prompts,
                         f"that is the stall nudge, for a worker that exists: {r.prompts!r}")

    def test_the_ticket_number_comes_from_the_branch(self):
        r = KeepaliveRun(self, [pane(verdict="NOT-STARTED", progress="NOT-STARTED",
                                     branch="issue-24-keepalive-truth")]).run()
        self.assertIn("glab issue view 24", r.prompts, r.prompts)

    def test_the_ticket_step_follows_the_tracker_provider(self):
        """The step is the provider's, not a literal: a GitHub workspace gets `gh issue view`.

        Every other test in this file pins the GitLab flavor because that is the flavor its
        fixtures stage; this one run flips the provider and asserts only the nudge. The CI half
        reports "could not be read" here for the honest reason that the GitHub reader is not
        staged in this fixture — `scripts/test_github_ci.py` covers the reader itself.
        """
        r = KeepaliveRun(self, [pane(verdict="NOT-STARTED", progress="NOT-STARTED")])
        r.env["FACTORY_TRACKER_PROVIDER"] = "github"
        r.run()
        self.assertIn("gh issue view 24", r.prompts, r.prompts)
        self.assertNotIn("glab issue view 24", r.prompts, r.prompts)

    def test_a_branch_with_no_issue_number_does_not_send_a_placeholder(self):
        """An unsubstituted `<iid>` reads as a broken tool, and is ignored — the state this
        whole path exists to escape."""
        r = KeepaliveRun(self, [pane(verdict="NOT-STARTED", progress="NOT-STARTED",
                                     branch="worker/scratch")]).run()
        self.assertNotIn("<iid>", r.prompts,
                         f"an unsubstituted placeholder is a prompt to nobody: {r.prompts!r}")
        self.assertIn("no issue number", r.prompts, r.prompts)

    def test_a_worker_that_did_start_is_not_reprompted(self):
        """The other direction. Same `ahead=0 dirty=0`; only the evidence differs."""
        r = KeepaliveRun(self, [pane(verdict="NOTHING", progress="STARTED")],
                         args=["--stall", "8"]).run()
        self.assertNotIn("never registered", r.prompts,
                         f"a worker that has run turns must not get the never-started prompt: "
                         f"{r.prompts!r}")
        print("    started at ahead=0 -> NOT the never-started prompt")

    def test_a_dirty_worker_is_not_made_urgent_by_this_change(self):
        """The fix must not make every quiet worker urgent.

        A worker mid-edit with a dirty tree is the healthy case this script already handled.
        `NOT-STARTED` is a *separate* path precisely so it can be urgent without dragging the
        ordinary stall rule up to `--stall 1` — a worker that has been working for four hours
        deserves a nudge then, not a re-prompt telling it its brief never landed.

        Below the threshold a dirty tree is silent here, and that is by design rather than an
        oversight: this script counts, `notify-coordinator.py` escalates a *stalled* `IN-FLIGHT`
        row in its own wake-up body. So the assertion is on the distinction that matters — the
        dirty worker must not receive the never-started prompt.
        """
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=4,
                                     progress="STARTED")], args=["--stall", "8"]).run()
        self.assertNotIn("never registered", r.prompts,
                         f"a worker mid-edit must not be told its brief never arrived: "
                         f"{r.prompts!r}")
        self.assertEqual(r.prompts, "",
                         f"and it must still wait for --stall unchanged checks: {r.prompts!r}")

    def test_it_is_reported_under_needs_attention_and_not_as_a_stall(self):
        r = KeepaliveRun(self, [pane(verdict="NOT-STARTED", progress="NOT-STARTED")]).run()
        self.assertIn("NEEDS ATTENTION", r.out, r.out)
        self.assertIn("NEVER STARTED", r.out,
                      f"the report must name the state, since '0 commits' is what it used to "
                      f"say: {r.out}")


class GitLabCIIsReadAndTheTwoRedsAreSeparated(unittest.TestCase):
    """The billing case, and the trap: the family is entirely BLOCKED, and that is not a fault."""

    def test_a_blocked_pipeline_is_not_counted_as_a_fault(self):
        r = KeepaliveRun(self, [pane(verdict="MERGEABLE", ahead=2, progress="STARTED")],
                         ci="blocked").run()
        self.assertIn("CI BLOCKED", r.out,
                      f"a pipeline with no compute minutes must still be reported: {r.out}")
        self.assertIn("NOT a code defect", r.out,
                      f"and said not to be one, or a reader goes looking for code that never "
                      f"ran: {r.out}")
        self.assertIn("0 needing attention", r.out,
                      f"BLOCKED must not land in NEEDS ATTENTION: {r.out}")
        print("    CI blocked -> its own section, 0 needing attention")

    def test_a_red_pipeline_is_a_fault(self):
        r = KeepaliveRun(self, [pane(verdict="MERGEABLE", ahead=2, progress="STARTED")],
                         ci="red").run()
        self.assertIn("NEEDS ATTENTION", r.out, r.out)
        self.assertIn("main pipeline RED", r.out,
                      f"a job that ran and failed is a code defect and belongs with the faults: "
                      f"{r.out}")
        self.assertNotIn("CI BLOCKED", r.out,
                         f"a real failure must not be filed under CI minutes: {r.out}")

    def test_an_unreadable_gitlab_is_not_a_pass(self):
        r = KeepaliveRun(self, [pane(verdict="MERGEABLE", ahead=2, progress="STARTED")],
                         ci="unreadable").run()
        self.assertIn("CI UNVERIFIABLE", r.out, r.out)
        self.assertIn("not a pass", r.out,
                      f"'GitLab could not be read' and 'nothing is wrong' must not print the "
                      f"same way: {r.out}")

    def test_a_repository_with_no_pipeline_is_unverifiable_not_green(self):
        r = KeepaliveRun(self, [pane(verdict="MERGEABLE", ahead=2, progress="STARTED")],
                         ci="none").run()
        self.assertIn("CI UNVERIFIABLE", r.out,
                      f"no pipeline on main means main was not built, which is not a pass: {r.out}")

    def test_the_counts_are_reported_separately(self):
        """The number a reader quotes decides what they do next.

        "8 RED" would send someone to fix code that was never executed. So the two are counted
        separately in the summary line, in words.
        """
        r = KeepaliveRun(self, [pane(verdict="MERGEABLE", ahead=2, progress="STARTED")],
                         ci="blocked").run()
        self.assertIn("0 red / 1 blocked / 0 unverifiable", r.out,
                      f"the summary must separate the two reds: {r.out}")


class AMainSweepRedIsReported(unittest.TestCase):
    """The sweep's recorded run reaches this log, and it vetoes the all-clear.

    Measured on 2026-10-06: `social-studio`'s main was red from 10:00 UTC while the
    sweep gated it seven times. Every row was correct and every row was in
    `/tmp/opencode/main-sweep.cron.log`, which no person read. These drive the real keepalive
    against a synthetic record written in the shape `main_sweep_state.write_run()` produces.
    """

    def test_a_red_row_is_reported_with_repo_commit_and_reason(self):
        """A row a coordinator cannot act on is not a row: repository, commit, first useful line.

        The red log adds the fourth thing that makes it actionable: the full output's path. The summary
        is a judgement about which lines to show, and this is the report that has to survive the
        summary being wrong.
        """
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], sweep="red").run()
        self.assertNotIn("nothing to do", r.out,
                         f"a RED main must never coexist with the reassuring sentence: {r.out}")
        self.assertIn("NEEDS ATTENTION", r.out, r.out)
        self.assertIn("main sweep RED", r.out, f"the fault must be named as the sweep's: {r.out}")
        self.assertIn("meta", r.out, f"the repository: {r.out}")
        self.assertIn("deadbee1234", r.out, f"the commit: {r.out}")
        self.assertIn("guard_test.go:112: synthetic main failure", r.out,
                      f"the sweep's first useful line: {r.out}")
        self.assertIn("main-sweep-red-meta-deadbee1234.log", r.out,
                      f"and the path of the full output beside it: {r.out}")
        print("    a recorded RED main -> NEEDS ATTENTION with repository, commit, reason and log")

    def test_a_missing_record_is_not_a_pass(self):
        """No record at all is the pre-deployment state, and must not read like a green sweep."""
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], sweep=None).run()
        self.assertNotIn("nothing to do", r.out,
                         f"a main nobody has swept is not health: {r.out}")
        self.assertIn("MAIN SWEEP UNVERIFIED", r.out, f"its own heading: {r.out}")
        self.assertIn("no-run-recorded", r.out, f"named so it can be acted on: {r.out}")
        self.assertIn("NOT clear", r.out, r.out)

    def test_a_stale_record_is_not_a_pass(self):
        """A stopped sweep must not leave yesterday's green implying today's health."""
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], sweep="stale").run()
        self.assertNotIn("nothing to do", r.out, f"{r.out}")
        self.assertIn("stale-run", r.out, f"the staleness must be named: {r.out}")

    def test_a_no_gate_row_is_unverified_not_silent(self):
        """A repository the sweep could not gate is the same fact as a pipeline it could not read.

        The sweep itself refuses to call this green; a report that dropped the row would be the
        lie the sweep's `NO-GATE` exists to prevent, arriving through the other door.
        """
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], sweep="nogate").run()
        self.assertNotIn("nothing to do", r.out, f"{r.out}")
        self.assertIn("MAIN SWEEP UNVERIFIED", r.out, r.out)
        self.assertIn("NO-GATE", r.out, f"the row itself must be reported: {r.out}")

    def test_a_fresh_green_record_still_allows_the_all_clear_and_names_it(self):
        """The direction that stops this becoming noise: a green sweep is the normal state."""
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], sweep="green").run()
        self.assertIn("nothing to do", r.out,
                      f"a fresh green record must not make the tool permanently non-clear: {r.out}")
        self.assertIn("latest recorded main sweep", r.out,
                      f"and the all-clear must name the sweep among what it checked: {r.out}")
        print("    fresh green record -> all-clear stands, and names the sweep check")


class ADeclaredStateRelocatesAndNeverClears(unittest.TestCase):
    """The declaration mechanism for CI, and THE TRAP under it.

    ## What the mechanism is for

    `FLEET_EXPECTED_RED` already exists for exactly this shape, and its docstring is the whole
    argument: *a report that is always true is one nobody reads.* Eleven repositories were on
    `ci_quota_exceeded` and two had no pipeline at all, so the report repeated itself at every run
    for as long as the work took.

    ## THE TRAP, and it is why this class is written before the feature

    **A declaration is a silencer unless it is checked.** If declaring every known state produced
    a clear report, then one unchecked edit to a JSON file would be enough to make this monitor go
    quiet — and a monitor that has been silenced by a claim is *worse* than a noisy one, because
    it is trusted. So:

    1. a declared state must never make the report clear, and
    2. a declared state that has become **worse** — `NONE` declared, a real `RED` observed — must
       be **louder** than the same state undeclared, not quieter.

    Both are asserted against the real script driving the real `gitlab_ci.py`, and the first one
    is the reason this class exists rather than a `declared = [...]` filter in the report loop.
    """

    # `NONE` for both: no pipeline on main. `meta` has no `.gitlab-ci.yml` by design and
    # `social-studio` is mid-ticket. This is the exact family state the report has been
    # repeating, and declaring it is what the coordinator would do.
    NONE_DECLARED = {"meta": {"state": "NONE",
                                        "because": "no .gitlab-ci.yml by design; the AGENTS.md "
                                                  "gate block is the gate"},
                     "social-studio": {"state": "NONE", "because": "#32 adds its "
                                                                              "pipeline"}}

    def test_declaring_everything_known_does_not_produce_a_clear_report(self):
        """THE TRAP, and the test that can actually fail.

        **Read the note on the two tests below before believing this one.** Declaring two `NONE`
        repositories does not, by itself, discriminate: `CI UNVERIFIABLE` already withholds the
        all-clear today, so the assertion passes against the unfixed script. A trap test that
        cannot go red is a claim, not evidence, and this file has shipped that before.

        So the discriminating test is
        `test_declaring_every_everything_known_blocked_is_not_a_clear_report`: today a fleet whose
        pipelines are all `BLOCKED` **does** get an all-clear — the never-started rule decided that deliberately,
        so billing cannot make the tool permanently non-clear — and a mechanism that treated
        "declared" as "handled" would keep that all-clear after nine declarations. That is the
        silencer, and it is the one this ticket is about.

        What this test adds is the other half of the claim: a declaration never *becomes* a pass,
        whatever the declared state.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "none"},
            declared=self.NONE_DECLARED,
        ).run()
        self.assertNotIn("nothing to do", r.out,
                         f"declaring every known state must not clear the report — a declaration is "
                         f"a coordinator's claim, not a verification: {r.out}")
        self.assertNotIn("all clear", r.out,
                         f"and the reassuring phrase must not appear even inside a denial: {r.out}")
        self.assertIn("NOT clear", r.out,
                      f"it must say why it is not clear, naming the declared states: {r.out}")
        print("    every NONE state declared -> still NOT clear")

    def test_declaring_every_everything_known_blocked_is_not_a_clear_report(self):
        """THE TRAP, in the only shape where it is falsifiable today — and it is the nine repos.

        Undeclared `BLOCKED` allows the all-clear, on purpose: the never-started rule refused to let a billing
        problem make the monitor permanently non-clear, and that decision stands. So the family
        state this ticket is actually about — nine repositories with no compute minutes — reports
        *clear* right now.

        That is exactly why declaring them must not keep it that way. A coordinator writes nine
        lines into a state file, the sections tidy up, and the monitor reports all clear for a
        family in which **no pipeline has run since the declaration was written**. The declaration
        was believed instead of checked, and a monitor that has been silenced by a claim is worse
        than a noisy one, because it is trusted.

        So the rule is one-directional and worth stating as such: **declaring can only ever make
        this report louder, never quieter.** That asymmetry is the whole safety property, and it is
        why an undeclared `BLOCKED` still clears while a declared one does not — the first is a
        fact this script observed and judged not to be a fault, the second is a claim in a file
        that nothing can check.
        """
        repos = ["meta", "identity", "press"]
        declared = {
            "identity": {"state": "BLOCKED",
                                   "because": "shared runners have no compute minutes (billing)"},
            "press": {"state": "BLOCKED",
                                "because": "shared runners have no compute minutes (billing)"},
        }
        common = dict(repos=repos,
                      per_repo_ci={"meta": "blocked", "identity": "blocked",
                                  "press": "blocked"})
        # The control, so the test cannot pass for the wrong reason: the same nine repositories,
        # undeclared, are clear today. If the assertion below were accidentally satisfied by
        # something else, this control would move and the difference would show.
        control = KeepaliveRun(
            self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")], **common).run()
        self.assertIn("nothing to do", control.out,
                      f"CONTROL: undeclared BLOCKED must still clear — the never-started rule decided that, and if it "
                      f"stops clearing the monitor has been made permanently non-clear: {control.out}")

        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
                         declared=declared, **common).run()
        self.assertNotIn("nothing to do", r.out,
                         f"declaring all nine BLOCKED repositories must NOT clear the report — "
                         f"nothing in them has been verified: {r.out}")
        self.assertNotIn("all clear", r.out,
                         f"and the reassuring phrase must not appear even inside a denial: {r.out}")
        self.assertIn("NOT clear", r.out,
                      f"it must say why it is not clear: {r.out}")
        print("    CONTROL: all BLOCKED undeclared -> clear")
        print("    TRAP:    all BLOCKED declared   -> still NOT clear")

    def test_the_reason_it_is_not_clear_names_the_declaration_as_the_reason(self):
        """An unexplained non-clear is indistinguishable from a stopped coordinator.

        This is the same rule as the reassuring sentence, pointed the other way: the report must
        say *what* withheld the all-clear. "NOT clear" alone teaches nothing, and the reader who
        cannot tell a declared state from a fault stops reading both.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "none"},
            declared=self.NONE_DECLARED,
        ).run()
        self.assertIn("declared", r.out.lower(),
                      f"the withheld all-clear must say that a declaration is what withheld it: "
                      f"{r.out}")
        self.assertIn("NOT a pass", r.out,
                      f"and must say a declaration is not a verification: {r.out}")

    def test_a_declared_state_is_still_reported_in_its_own_section(self):
        """Silence is not the alternative. A report that cannot be wrong is worse.

        The declaration relocates the line out of `CI UNVERIFIABLE` and into a section of its own,
        so a reader skimming sees "known, declared" rather than two more rows of noise. But the row
        is still *there*, still names the repository, and still carries the verdict `gitlab_ci.py`
        actually read. **A declaration must not be able to remove a repository from the report.**
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "none"},
            declared=self.NONE_DECLARED,
        ).run()
        self.assertIn("DECLARED", r.out,
                      f"a declared state must be reported under a heading of its own: {r.out}")
        for repo in self.NONE_DECLARED:
            self.assertIn(repo, r.out,
                          f"{repo} is declared and must still be named in the report: {r.out}")

    def test_a_declared_state_does_not_disappear_from_the_counts(self):
        """The summary is the number a reader quotes.

        Removing a declared repository from the counts is how the declaration becomes a silencer
        without anybody deciding to silence anything: the section looks tidy and the tally says
        zero. So the declared count is counted separately and stated.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "none"},
            declared=self.NONE_DECLARED,
        ).run()
        self.assertRegex(r.out, r"\b2\b[^\n]*\bdeclared\b|declared[^\n]*\b2\b",
                         f"two declared repositories must be counted as two: {r.out}")

    def test_and_an_undeclared_state_is_still_reported_as_it_was(self):
        """The mechanism must not change what an *undeclared* repository does.

        A declaration is per repository. If writing one entry for `meta` also silenced
        `social-studio`, the mechanism would be a blanket switch wearing a data file's
        clothes — and the file would look specific while behaving globally.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "none"},
            declared={"meta": self.NONE_DECLARED["meta"]},
        ).run()
        self.assertIn("CI UNVERIFIABLE", r.out,
                      f"the undeclared repository keeps its own section: {r.out}")
        self.assertIn("social-studio", section(r.out, "CI UNVERIFIABLE"), r.out)
        # …and the declared one has moved out of it.
        self.assertNotIn("meta", section(r.out, "CI UNVERIFIABLE"),
                         f"a declared state must be RELOCATED, never merged into another verdict: "
                         f"{section(r.out, 'CI UNVERIFIABLE')!r}")
        self.assertIn("meta", section(r.out, "CI DECLARED"),
                      f"and it must appear in the section of its own: {r.out}")

    def test_a_declaration_is_data_and_needs_no_code_edit(self):
        """Acceptance 5: a coordinator updates this without touching a script.

        The mirror of `FLEET_EXPECTED_RED` is a state file too, and it is read the same way —
        from a path in the environment. This test does not inspect the implementation; it changes
        the declaration between two runs of the *same* script and shows the report follows, which
        is the property that matters. A mechanism that needed a code edit would pass every other
        test here and still be wrong.
        """
        declared_then = {
            "meta": {"state": "NONE", "because": "no .gitlab-ci.yml by design"},
            "social-studio": {"state": "NONE", "because": "first pass"},
        }
        declared_now = {
            "meta": {"state": "NONE", "because": "no .gitlab-ci.yml by design"},
            "social-studio": {"state": "NONE", "because": "#32 added the pipeline"},
        }
        kwargs = dict(repos=["meta", "social-studio"],
                      per_repo_ci={"meta": "none", "social-studio": "none"})
        first = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                         progress="STARTED")],
                             declared=declared_then, **kwargs).run()
        second = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                          progress="STARTED")],
                              declared=declared_now, **kwargs).run()
        self.assertIn("first pass", first.out, first.out)
        self.assertIn("#32 added the pipeline", second.out,
                      f"changing only the data file must change the report: {second.out}")

    def test_the_counts_never_read_as_a_pass_when_nothing_was_verified(self):
        """The quietest failure in the whole mechanism, and it is in the summary line.

        Relocating eleven repositories into `CI DECLARED` empties `CI BLOCKED` and
        `CI UNVERIFIABLE`, so the summary reads `0 red / 0 blocked / 0 unverifiable` — three
        zeros for a family in which **no pipeline has run at all**. A reader who quotes that
        number, or skims it, has been told everything is fine by a tally that now counts only the
        undeclared.

        So the summary states how much was actually *verified*, which is the number its zeros used
        to imply. This is the "do not make the monitor quieter by making it less honest" rule
        applied to the one line a reader is most likely to quote.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "identity"],
            per_repo_ci={"meta": "none", "identity": "blocked"},
            declared={"meta": {"state": "NONE", "because": "no CI by design"},
                      "identity": {"state": "BLOCKED", "because": "no CI minutes"}},
        ).run()
        self.assertRegex(
            r.out,
            r"verified[^\n]*0 of 2|0 of 2[^\n]*verified",
            f"the summary must say how many pipelines actually verified main — three zeros read "
            f"as a pass for a family where nothing ran: {r.out}")
        print("    2 declared, none verified -> summary says '0 of 2 verified'")

    def test_a_broken_declaration_file_is_reported_not_swallowed(self):
        """Unreadable data is not the same as an empty declaration.

        `except: return set()` — the shape the existing `FLEET_EXPECTED_RED` loader uses — turns a
        typo in the state file into *silence about the typo*: the report goes back to undeclared
        lines and never says why. That is survivable, and it is the wrong direction. It must be
        reported, because a coordinator who believes a declaration is in force when the file does
        not parse has been misled by the mechanism meant to inform them.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "none"},
            declared_raw="{ this is not json",
        ).run()
        self.assertIn("declared", r.out.lower(),
                      f"a declaration file that cannot be read must be reported: {r.out}")
        self.assertIn("NEEDS ATTENTION", r.out,
                      f"and must be a fault, not a silently absent declaration: {r.out}")

    def test_a_declaration_naming_an_unknown_state_is_refused(self):
        """A declared state is a *checked* claim, so an unrecognised one cannot be honoured.

        `gitlab_ci.py` has six verdicts and only two of them are declarable states: `NONE` (this
        repository has no pipeline, by design) and `BLOCKED` (no compute minutes). Declaring
        `GREEN`, `RED`, `RUNNING` or a typo would mean trusting a name this script cannot resolve,
        and the honest answer to an unresolvable name is to say so rather than to treat the
        declaration as satisfied.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "none"},
            declared={"meta": {"state": "NOPE", "because": "a typo"}},
        ).run()
        self.assertIn("NEEDS ATTENTION", r.out,
                      f"a declaration this script cannot resolve must be a fault: {r.out}")
        self.assertIn("NOPE", r.out, f"and must name the state it could not resolve: {r.out}")

    def test_declaring_a_verdict_of_green_is_refused(self):
        """The one declaration that would be a genuine silencer.

        `GREEN` is not a state, it is the absence of a problem. A coordinator who can write
        `{"state": "GREEN"}` into a file and have this script print "all clear" has built a
        one-line switch for the whole monitor, driven by nothing but the assertion that a thing
        is fine. It is refused — and refused *loudly*, because the attempt is itself the finding.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            ci="green",
            repos=["meta"],
            declared={"meta": {"state": "GREEN", "because": "I am fairly sure"}},
        ).run()
        self.assertIn("NEEDS ATTENTION", r.out,
                      f"declaring GREEN must never be honoured, and must be loud: {r.out}")
        self.assertNotIn("nothing to do", r.out,
                         f"and certainly must not clear the report: {r.out}")


class AStaleDeclarationIsWorseThanNoDeclaration(unittest.TestCase):
    """THE TRAP, second half — the direction that loses a red.

    The declaration mechanism's own words: *a mechanism that lets a coordinator hide a red behind a stale declaration is
    a mechanism for losing a red.* `social-studio` is declared `NONE` today and gets a
    pipeline later. The moment it does, three things are possible and only one is right:

    | declared | observed | right answer |
    |---|---|---|
    | `NONE` | `NONE` | declared, still not a pass |
    | `NONE` | `GREEN` | stale declaration, remove it — **and that is good news** |
    | `NONE` | `RED` | **LOUD.** A job ran and failed behind a declaration that said nothing was running. |

    The middle case is the trap's sting: a declaration that went stale in the *favourable*
    direction is easy to treat as noise. It is not — it means the file is lying, and the next
    transition could go the other way.
    """

    def test_a_declared_none_that_becomes_red_is_a_fault(self):
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "red"},
            declared={"meta": {"state": "NONE", "because": "no .gitlab-ci.yml by design"},
                      "social-studio": {"state": "NONE", "because": "#32 adds its "
                                                                          "pipeline"}},
        ).run()
        self.assertIn("NEEDS ATTENTION", r.out,
                      f"a red behind a stale declaration is a code defect and must be a fault: "
                      f"{r.out}")
        self.assertIn("main pipeline RED", r.out,
                      f"and must say a job ran and failed, not that nothing was running: {r.out}")

    def test_and_the_declaration_is_named_as_the_thing_that_is_wrong(self):
        """Loud means it says *both* halves: the pipeline is red AND the file is stale.

        Reporting only "main pipeline RED" leaves the coordinator believing their declaration is
        still doing work, so they fix the pipeline and leave the file — and the next red arrives
        behind a declaration that was never updated. Naming the stale declaration is what stops the
        error compounding.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "red"},
            declared={"social-studio": {"state": "NONE", "because": "#32 adds its "
                                                                          "pipeline"}},
        ).run()
        self.assertIn("social-studio", r.out,
                      f"the repository whose declaration went stale must be named: {r.out}")
        self.assertIn("stale", r.out.lower(),
                      f"and the report must say the DECLARATION is what is wrong: {r.out}")

    def test_a_stale_declaration_is_not_relocated_into_the_declared_section(self):
        """THE RELOCATION RULE: a declared state moves to its own section, or nowhere.

        The trap here is the convenient implementation — "declared ⇒ move the line to the DECLARED
        section" — which is *exactly* what would swallow this red. The declaration only relocates
        while it still matches. Once GitLab says something else, the row belongs in the fault
        bucket and the declaration is the thing being reported.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "red"},
            declared={"social-studio": {"state": "NONE", "because": "#32 adds it"}},
        ).run()
        declared_section = section(r.out, "CI DECLARED")
        self.assertNotIn("social-studio", declared_section,
                         f"a red must not be relocated into the declared section: "
                         f"{declared_section!r}")
        self.assertIn("social-studio", section(r.out, "NEEDS ATTENTION"),
                      f"it must be in the faults instead: {r.out}")

    def test_a_declared_none_that_becomes_green_reports_the_stale_declaration(self):
        """Good news is still news, because the file is now wrong.

        This is the case a coordinator meets the moment a pipeline lands: the pipeline appears and is
        green, the `NONE` line vanishes, and the declaration silently becomes a no-op that still
        looks maintained. The declaration must be reported as stale so it gets removed — and it
        must NOT be a fault, or the mechanism punishes the fix it was written to accommodate.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "green"},
            declared={"social-studio": {"state": "NONE", "because": "#32 adds it"}},
        ).run()
        self.assertIn("stale", r.out.lower(),
                      f"a declaration GitLab has outgrown must be reported: {r.out}")
        self.assertIn("social-studio", r.out, r.out)
        self.assertNotIn("NEEDS ATTENTION", r.out,
                         f"but a declaration that went stale favourably is NOT a fault — the "
                         f"mechanism must not punish the fix: {r.out}")

    def test_a_declared_blocked_that_becomes_red_is_a_fault_too(self):
        """The same trap with the state that has nine repositories behind it today.

        `BLOCKED` means no job ever started. It becomes `RED` the moment compute minutes come back
        and the code is genuinely broken — which is the *happy* end of the billing problem and the
        worst possible moment to be reading a stale declaration, because the report will have been
        saying "billing, not a code defect" in the reader's head for weeks.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "identity"],
            per_repo_ci={"meta": "none", "identity": "red"},
            declared={"identity": {"state": "BLOCKED",
                                             "because": "no compute minutes group-wide"}},
        ).run()
        self.assertIn("NEEDS ATTENTION", r.out,
                      f"a BLOCKED declaration that turned into a real failure must be a fault: "
                      f"{r.out}")
        self.assertIn("main pipeline RED", r.out, r.out)
        self.assertNotIn("CI BLOCKED", r.out,
                         f"and it must leave the section that says 'not a code defect': {r.out}")

    def test_a_declared_none_that_becomes_blocked_is_reported_as_both(self):
        """The middle transition, and the one that is easiest to get wrong quietly.

        `NONE` → `BLOCKED` is not a fault and not good news: the repository gained a pipeline that
        cannot run. It must be reported as a stale declaration *and* as `BLOCKED`, because it is
        still not verified, and a declaration that has stopped matching is the reason to look.
        """
        r = KeepaliveRun(
            self,
            [pane(verdict="IN-FLIGHT", ahead=3, dirty=2, progress="STARTED")],
            repos=["meta", "social-studio"],
            per_repo_ci={"meta": "none", "social-studio": "blocked"},
            declared={"social-studio": {"state": "NONE", "because": "#32 adds it"}},
        ).run()
        self.assertIn("stale", r.out.lower(),
                      f"the declaration no longer matches and must be reported: {r.out}")
        # The real verdict must still be reported *as itself*, in its own section — so a reader
        # is told both halves: the file is stale, and `BLOCKED` is what GitLab says now.
        self.assertIn("social-studio",
                      section(r.out, "CI BLOCKED"),
                      f"and the repository's real state must still be reported as itself, not "
                      f"folded into the stale-declaration section: {r.out}")
        self.assertIn("social-studio",
                      section(r.out, "DECLARED STALE"),
                      f"while the stale declaration is reported too: {r.out}")


class TheReassuringSentenceIsEarned(unittest.TestCase):
    """An all-clear may only be printed when what it implies is true, and must say what it checked."""

    def test_it_is_printed_when_everything_is_genuinely_green(self):
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], ci="green").run()
        self.assertIn("nothing to do", r.out,
                      f"a healthy fleet must still be able to say so, or the fix has silenced the "
                      f"tool: {r.out}")
        self.assertIn("checked:", r.out,
                      f"and it must name what it checked, so an unexplained all-clear is "
                      f"distinguishable from a stopped coordinator: {r.out}")
        print("    healthy + CI green -> all-clear, and it names what it checked")

    def test_it_names_the_pipeline_check_explicitly(self):
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], ci="green").run()
        self.assertIn("pipeline on main", r.out,
                      f"the reader must be able to tell CI was checked and not merely assumed: "
                      f"{r.out}")

    def test_a_never_started_worker_withdraws_it(self):
        """The 2026-10-03 failure, exactly: green CI, one working pane, one worker absent."""
        r = KeepaliveRun(self, [
            pane(name="wPG:p1", verdict="IN-FLIGHT", ahead=6, progress="STARTED",
                 worktree="/w/working"),
            pane(name="wNF:p1", verdict="NOT-STARTED", progress="NOT-STARTED",
                 worktree="/w/never"),
        ], ci="green").run()
        self.assertNotIn("nothing to do", r.out,
                         f"an all-clear while a worker never started is the bug this ticket is "
                         f"about: {r.out}")
        self.assertIn("NEEDS ATTENTION", r.out, r.out)
        self.assertIn("NEVER STARTED", r.out, r.out)

    def test_an_unreadable_gitlab_withdraws_it(self):
        """Even with every worker healthy: a tool that cannot see CI must not claim CI is fine."""
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], ci="unreadable").run()
        self.assertNotIn("nothing to do", r.out,
                         f"'unreachable is not green' — the all-clear implies main was judged: "
                         f"{r.out}")
        self.assertIn("NOT clear", r.out, r.out)
        # Asserted rather than left to inspection: "NOT all clear" *contains* "all clear", so a
        # phrasing like that satisfies any check for a reassuring sentence — including the
        # keepalive's own. The denial must not look like the claim.
        self.assertNotIn("all clear", r.out,
                         f"this log must not contain the reassuring phrase even in a denial: "
                         f"{r.out}")

    def test_but_a_blocked_pipeline_still_allows_it(self):
        """The direction that matters for not manufacturing noise.

        A `BLOCKED` pipeline is not a fault, and refusing the all-clear for it would make the
        tool permanently non-clear while the group has no compute minutes — the failure mode the
        brief warns about. So `BLOCKED` neither vetoes the all-clear nor is hidden: the all-clear
        is printed *and* says what is unverified.
        """
        r = KeepaliveRun(self, [pane(verdict="IN-FLIGHT", ahead=3, dirty=2,
                                     progress="STARTED")], ci="blocked").run()
        self.assertIn("nothing to do", r.out,
                      f"a billing problem must not make the tool permanently non-clear: {r.out}")
        self.assertIn("BLOCKED on CI minutes", r.out,
                      f"but the all-clear must still say what was not verified: {r.out}")
        print("    CI blocked + healthy fleet -> all-clear, and it names the unverified repos")

    def test_a_dry_run_reports_without_sending(self):
        r = KeepaliveRun(self, [pane(verdict="NOT-STARTED", progress="NOT-STARTED")],
                         args=["--dry-run"]).run()
        self.assertEqual(r.prompts, "", f"--dry-run must send nothing: {r.prompts!r}")
        self.assertIn("NEVER STARTED", r.out, f"but it still reports: {r.out}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--explain":
        print(__doc__)
        sys.exit(0)
    unittest.main(verbosity=2)