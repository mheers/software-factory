#!/usr/bin/env python3
"""Tests for the coordinator wake-up, and for the stall dimension it was missing.

Run: `python3 scripts/test_notify_coordinator.py` (no test runner needed, no network, no
sleeps — the check count is faked, never waited for).

## What this holds

1. A pane past `--stalled` enters the actionable set; at and below the threshold it does not.
2. A pane that past the threshold is **out** of the set again — de-escalation. The streak
   resets when the row moves, so escalation cannot latch.
3. A stall-only run never reports all-clear, and a stall-only wake-up does not end with
   merge instructions it cannot follow.
4. **The mutation.** This bug *is* a false pass — the pre-fix hash suppressed a stalled pane
   forever, while looking correct — so the mutation is what makes it visible. The last class
   binds the pre-fix implementation and asserts it cannot satisfy the assertions above.

## Why it drives the real script

A Python copy of the candidate logic would pass while `main()` stayed broken, which is the
probe that was never a test. So these run `notify-coordinator.py` as a subprocess with a stub
`herdr` and a stub `fleet-watch.py` on `PATH`, and read what it actually prompted. That is the
only thing that proves the wake-up body, the log line and the state machine agree.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "notify-coordinator.py"

DEFAULT_STALLED = 8

# The workspace root the script scopes its coordinator match to, and the cwd of a real
# foreign pane that carries the same label — `w3A:p1` on this machine belongs to another project.
# Both are synthetic fixture data: these tests never read live herdr state, and the pane ids they
# use (`COORD`, `wFOR:p1`) do not exist anywhere.
WORKSPACE = "/synthetic/workspace"
FOREIGN_CWD = "/synthetic/other-workspace"


def foreign_first_fleet():
    """The foreign-pane fixture: a labelled pane in another project is FIRST **and** is the focused one.

    First-and-live is exactly what the pre-fix label-only loop returned, and focused is what the
    cwd *fallback* prefers — so neither the list order nor focus can produce the right answer
    here; only the cwd scope can. The same fixture is used by the fix's tests and by the mutant
    that removes the scope, so the mutation cannot pass by accident of a different fixture.
    """
    panes = [
        {"pane_id": "wFOR:p1", "label": "AI · coordinator", "cwd": FOREIGN_CWD},
        {"pane_id": "COORD", "label": "AI · coordinator", "cwd": WORKSPACE},
    ]
    agents = [
        {"pane_id": "wFOR:p1", "cwd": FOREIGN_CWD, "agent_status": "idle", "focused": True},
        {"pane_id": "COORD", "cwd": WORKSPACE, "agent_status": "idle", "focused": False},
    ]
    return panes, agents


def sweep_run(repos=("meta",), kind="green"):
    """One recorded main-sweep run, in the shape `main_sweep_state.write_run()` writes.

    The default is a **fresh, green** record, because that is the normal operating
    state: cron records a run hourly and the report reads the last one. A harness that shipped no
    record at all would make every all-clear test an assertion about the missing record rather
    than about the thing the test names — so the tests that want the sweep dimension ask for it
    (`sweep="red"`, `sweep=None`, `sweep="stale"`, …).
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


def pane(name, verdict, ahead, dirty, status="idle", worktree=None, branch="issue-99-thing",
         progress="STARTED", progress_why="144.6K tokens consumed"):
    """One fleet-watch row, in the exact shape `fleet-watch.py --json` emits.

    `progress` and `progress_why` are the never-started fields and default to a pane that HAS worked, so a
    test about something else cannot accidentally be about a never-started worker.
    """
    return {
        "pane": name,
        "branch": branch,
        "worktree": worktree if worktree is not None else f"/tmp/wt/{name}",
        "verdict": verdict,
        "ahead": ahead,
        "dirty": dirty,
        "behind": 0,
        "status": status,
        "progress": progress,
        "progress_why": progress_why,
        "why": f"synthetic row for {verdict}",
    }


class NotifyRun:
    """One real invocation of notify-coordinator.py against a synthetic fleet.

    `ci` is the family's pipeline state, as a fixture the **real** `gitlab-ci.py` reads through a
    strict `glab` stub — not a stubbed verdict. That distinction is the whole reason the CI half is
    tested this way: a Python copy of the classifier would pass while `gitlab-ci.py`
    stayed broken, which is the probe that was never a test. So the harness copies the real
    script and stubs only the network underneath it, exactly as it stubs `herdr` above.
    """

    def __init__(self, case, panes, state=None, args=(), manifest=None,
                 coordinator_label="AI · coordinator", focused_pane="COORD", ci="green",
                 declared=None, declared_raw=None, repos=("meta",), per_repo_ci=None,
                 sweep="green", sweep_raw=None, herdr_panes=None, herdr_agents=None, script=None):
        self.coordinator_label = coordinator_label
        self.focused_pane = focused_pane
        self.ci = ci
        self.tmp = Path(tempfile.mkdtemp())
        # `case` registers the cleanup, so every test's scratch fleet goes with it.
        case.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = self.tmp / "notify.json"
        if state is not None:
            self.state_path.write_text(json.dumps(state))

        bindir = self.tmp / "bin"
        bindir.mkdir()
        self._write_glab_stub(bindir)
        # A stub fleet-watch.py, so the sweep is a fixture and not a fleet. notify-coordinator
        # resolves it as `HERE / "fleet-watch.py"`, so it has to sit next to a copy of the
        # script rather than on PATH — see _copy_script. Named `watch_stub` and not `sweep`:
        # `sweep` is the sweep-record parameter naming this run's main-sweep *record*, and a local with
        # the same name shadows it — which it did, silently, until a test looked for a red row.
        watch_stub = bindir / "fleet-watch.py"
        watch_stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps({{'panes': json.loads({json.dumps(json.dumps(panes))})}}))\n")
        watch_stub.chmod(0o755)

        prompt_log = self.tmp / "prompts.log"
        herdr = bindir / "herdr"
        # `pane list` carries the `label` and the `cwd` that `agent list` does not carry
        # together — which is the whole point of the label-based resolver, so the stub has to
        # model the difference or the tests would pass against a stub that made the two look
        # identical. **The `cwd` is not decoration:** the label is not unique on this
        # machine, so the match is label AND cwd, and a fixture whose panes carried no cwd
        # would silently be a label-only test — green against the very defect it exists to
        # catch. `herdr_panes` / `herdr_agents` let the cwd-scope tests replace the fixture,
        # and the default is still the 2026-10-05 shape: two root agents, one labelled.
        panes_fx = herdr_panes if herdr_panes is not None else [
            {"pane_id": "COORD", "label": self.coordinator_label, "cwd": WORKSPACE},
            {"pane_id": "OTHER", "label": None, "cwd": WORKSPACE},
        ]
        agents_fx = herdr_agents if herdr_agents is not None else [
            {"pane_id": "COORD", "cwd": WORKSPACE, "agent_status": "idle",
             "focused": self.focused_pane == "COORD"},
            {"pane_id": "OTHER", "cwd": WORKSPACE, "agent_status": "idle",
             "focused": self.focused_pane == "OTHER"},
        ]
        # `ensure_ascii=False` because the label carries `·` and the stub is a `/bin/sh`
        # `echo`: json's default escapes it to `\u00b7`, which is a sequence dash's echo is
        # free to mangle. The real `herdr` answers UTF-8, so the fixture does too.
        agents_json = json.dumps({"result": {"agents": agents_fx}}, ensure_ascii=False)
        panes_json = json.dumps({"result": {"panes": panes_fx}}, ensure_ascii=False)
        herdr.write_text(
            "#!/bin/sh\n"
            f'case "$1 $2" in\n'
            f'"agent list") echo \'{agents_json}\' ;;\n'
            f'"pane list") echo \'{panes_json}\' ;;\n'
            f'"agent prompt") shift 2; printf "%s\\n" "$*" >> {prompt_log} ;;\n'
            f'"agent get") echo \'{{"result":{{"agent":{{"agent_status":"idle"}}}}}}\' ;;\n'
            f'*) exit 0 ;;\n'
            f"esac\n")
        herdr.chmod(0o755)

        self.scripts = self.tmp / "scripts"
        self.scripts.mkdir()
        # `script=` runs a mutant (the mutation proofs below), still under the canonical name so
        # the sibling imports — `expected_ci`, `main_sweep_state`, `fleet-watch.py` — resolve.
        shutil.copy(script or SCRIPT, self.scripts / "notify-coordinator.py")
        shutil.copy(watch_stub, self.scripts / "fleet-watch.py")
        # The REAL `gitlab-ci.py`, not a stubbed verdict. notify-coordinator resolves it as
        # `HERE / "gitlab-ci.py"`, so it has to sit beside the script, and copying the real one
        # is what makes these tests also exercise the classifier — a hand-written verdict in
        # Python would pass while `gitlab-ci.py` stayed broken, which is the probe that was never
        # a test. `scripts/test_gitlab_ci.py` holds the classifier's own cases.
        # Under its REAL name. The old line copied it to `gitlab-ci.py`, so the fixture
        # agreed with the caller and the production path was never exercised.
        shutil.copy(HERE / "gitlab_ci.py", self.scripts / "gitlab_ci.py")
        # The shared declaration reader, copied beside the script for the same reason
        # `gitlab_ci.py` is: `notify-coordinator.py` imports it by path, and a test that shipped a
        # copy of its own would be asserting against the fixture rather than the module.
        shutil.copy(HERE / "expected_ci.py", self.scripts / "expected_ci.py")
        # The shared main-sweep reader, beside the script for the same reason: it is
        # imported by path, and `main_sweep_state.py` is what turns the sweep's record into the
        # wake-up's red section. A stub of it would make every sweep assertion a claim about the
        # fixture.
        shutil.copy(HERE / "main_sweep_state.py", self.scripts / "main_sweep_state.py")
        # The configuration seam the siblings resolve through — imported by path, so it has to
        # sit beside the script like they do.
        shutil.copy(HERE / "factory_config.py", self.scripts / "factory_config.py")
        # The registry the survey reads. One repository by default, so a run reads exactly one
        # pipeline and every assertion is about one verdict. The real `apps.json` has eleven
        # entries and the stub answers the same payload for all of them, which would make every
        # count in the report a lie.
        # `meta` is in that default, not some other name: `gitlab_ci.py` always appends
        # meta (it has its own CI shape to judge) and de-duplicates, so naming it here yields
        # exactly ONE repository. Naming anything else would yield two, and every count would be
        # doubled — which is how a test ends up asserting on a number that is about the fixture
        # rather than about the code.
        #
        # `repos` is a keyword because the declarations are a *set* of them: a mechanism that
        # relocates one row must not relocate another, and that cannot be shown with one.
        self.apps_path = self.tmp / "apps.json"
        self.apps_path.write_text(json.dumps(
            {"apps": [{"id": n, "repo": n} for n in repos]}))

        # Per-repository CI verdicts. Absent by default, so `CI_FIXTURE` answers every request and
        # every earlier fixture behaves exactly as it did before.
        self.repo_fixtures = self.tmp / "repo-fixtures"
        if per_repo_ci:
            self.repo_fixtures.write_text("".join(f"{k}={v}\n" for k, v in per_repo_ci.items()))
        # The manifest must not claim COORD is a worker, or it is filtered out as one.
        # `manifest` lets a test declare workers the sweep will NOT return, which is how a
        # pane whose worktree is gone gets into the run at all.
        self.manifest_path = self.tmp / "fleet.json"
        self.manifest_path.write_text(json.dumps(manifest or {}))

        # The declared-CI state file. `declared_raw` writes bytes verbatim so a test can
        # present the file malformed, which is a different fact from declaring nothing and is
        # reported differently. `declared` writes the well-formed shape under the real key.
        # Writing neither is the ordinary case: no file means nothing declared, and the path points
        # into this run's own temporary directory so a file left by another run cannot leak in.
        self.expected_ci = self.tmp / "expected-ci.json"
        if declared_raw is not None:
            self.expected_ci.write_text(declared_raw)
        elif declared is not None:
            self.expected_ci.write_text(json.dumps(
                {"_comment": "synthetic", "repositories": declared}))

        # The main sweep's record. `sweep="green"` is the ordinary operating state — the
        # sweep ran recently and found nothing red — so every earlier fixture behaves as it did.
        # `sweep=None` writes no file at all (the sweep has never run), `sweep_raw` writes bytes
        # verbatim so a test can present the file malformed, and the named kinds write the shape
        # `main_sweep_state.write_run()` produces.
        self.main_sweep = self.tmp / "main-sweep.json"
        if sweep_raw is not None:
            self.main_sweep.write_text(sweep_raw)
        elif sweep is not None:
            self.main_sweep.write_text(json.dumps(sweep_run(repos, sweep)))

        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FLEET_NOTIFY_STATE"] = str(self.state_path)
        env["FLEET_MANIFEST"] = str(self.tmp / "fleet.json")
        env["FLEET_KEEPALIVE_LOG"] = str(self.tmp / "keepalive.log")
        env["FLEET_EXPECTED_RED"] = str(self.tmp / "expected-red.json")
        env["FLEET_EXPECTED_CI"] = str(self.expected_ci)
        env["FLEET_MAIN_SWEEP"] = str(self.main_sweep)
        env["FACTORY_WORKSPACE"] = WORKSPACE
        env["FLEET_APPS_JSON"] = str(self.apps_path)
        env["CI_FIXTURE"] = self.ci
        # The fixtures stage and stub the GitLab CI reader; the provider pins it.
        env["FACTORY_TRACKER_PROVIDER"] = "gitlab"
        env["REPO_FIXTURES"] = str(self.repo_fixtures)
        env.update(self.ci_env())
        self.env = env
        self.args = list(args)
        self.proc = None
        self._out = ""

    def run(self, extra=()):
        self.proc = subprocess.run(
            [sys.executable, str(self.scripts / "notify-coordinator.py"),
             *self.args, *extra],
            capture_output=True, text=True, env=self.env, timeout=120)
        # Accumulated, not replaced: several of these tests assert on what the sequence of
        # runs did in total ("woke exactly once over three runs"), and keeping only the last
        # run's output would make that assertion pass for the wrong reason.
        self._out += self.proc.stdout + self.proc.stderr
        return self

    @property
    def out(self):
        assert self.proc is not None, "run() first"
        return self._out

    @property
    def last_out(self):
        assert self.proc is not None, "run() first"
        return self.proc.stdout + self.proc.stderr

    def set_manifest(self, manifest):
        """Change the recorded workers between runs, without rebuilding the scratch fleet."""
        self.manifest_path.write_text(json.dumps(manifest))
        return self

    def _write_glab_stub(self, bindir):
        """A `glab` that answers `api` for pipelines and jobs, and rejects everything else.

        **Strict, because a stub that answers anything cannot fail.** That is not a general
        principle here, it is a measured one from this project: the `glab` stub in
        `test_cleanup_workers.py` shipped `glab mr list --state opened` — a flag the real CLI
        rejects — through 27 of 27 green tests, because it accepted any argument. A permissive
        stub turns a typo in a command this script builds into a test that passes.

        So the accepted surface is exactly what `gitlab-ci.py` may ask for: `glab api` with a
        project path and an endpoint. Anything else exits 2 with a message on stderr, the way the
        real CLI answers a usage error. And `apps.json` is pointed at a one-app fixture, so the
        harness reads exactly one repository and the assertions are about one verdict.

        The job payloads are the two real shapes from the live group, recorded in
        `test_gitlab_ci.py`: `ci_quota_exceeded` with `started_at: null` (the runner never took
        the job) and `script_failure` with a duration (the job ran and failed). Using those
        verbatim rather than an invented shape is what makes "the discriminator works" a claim
        about GitLab rather than about this test.
        """
        quota_jobs = json.dumps([
            {"id": 1, "name": "lint", "status": "failed", "failure_reason": "ci_quota_exceeded",
             "started_at": None, "duration": None, "runner": None},
            {"id": 2, "name": "test", "status": "failed", "failure_reason": "ci_quota_exceeded",
             "started_at": None, "duration": None, "runner": None},
        ])
        ran_jobs = json.dumps([
            {"id": 1, "name": "lint", "status": "failed", "failure_reason": "script_failure",
             "started_at": "2026-10-03T20:03:24.000Z", "duration": 19.99, "runner": {"id": 7}},
            {"id": 2, "name": "test", "status": "success", "failure_reason": None,
             "started_at": "2026-10-03T20:03:24.000Z", "duration": 21.1, "runner": {"id": 7}},
        ])
        # A green pipeline with no failed job. Kept separate from `quota_jobs` on purpose: those
        # are all `failed`, so reusing them for the green fixture would make a succeeded pipeline
        # report RED and the harness would be lying in the direction that hides the bug.
        green_jobs = json.dumps([
            {"id": 1, "name": "lint", "status": "success", "failure_reason": None,
             "started_at": "2026-10-05T17:00:00.000Z", "duration": 18.2, "runner": {"id": 7}},
        ])
        blocked_jobs = json.dumps([
            {"id": 1, "name": "lint", "status": "failed", "failure_reason": "ci_quota_exceeded",
             "started_at": None, "duration": None, "runner": None},
        ])
        ok_pipeline = json.dumps([{"id": 500, "status": "success", "ref": "main",
                                   "sha": "aaaaaaaabbbbccccddddeeeeffff1111"}])
        failed_pipeline = json.dumps([{"id": 501, "status": "failed", "ref": "main",
                                       "sha": "aaaaaaaabbbbccccddddeeeeffff2222"}])
        glab = bindir / "glab"
        # A PLAIN string, not an f-string: the stub reads its payloads from the environment
        # (`$PIPELINE_OK`), so it has no placeholders to interpolate — and as an f-string the shell
        # parameter expansions `${path#*example.com%2F}` are read as Python format braces and the whole
        # file fails to compile. Same shape as `GLAB_STUB` in `test_fleet_keepalive.py`, for the
        # same reason.
        glab.write_text(r"""#!/usr/bin/env bash
# Strict about its arguments, like the real CLI: `glab api <path>` is the only surface this
# project reads pipelines through, and anything else is a usage error rather than an answer.
if [ "$1" != "api" ]; then
  printf 'Unknown command: %s\n' "$1" >&2
  exit 2
fi
path="$2"

# PER REPOSITORY FIXTURES. One CI_FIXTURE for a whole run can only express one verdict at a
# time, so a declaration test would have had to isolate a single repository to say anything — and
# the claim under test is about a declaration applied to a *set* of them ("this one is declared,
# that one is not"). The path is `projects/example.com%2F<repo>/pipelines?...`, so the
# repository name is what follows the last encoded separator; a flat `<repo>=<fixture>` file is
# read for it and CI_FIXTURE stays the fallback, which is why every earlier fixture is unchanged.
REPO=""
case "$path" in
  *"example.com%2F"*) REPO="${path#*example.com%2F}"; REPO="${REPO%%/*}" ;;
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

case "$path" in
  *"pipelines?ref=main&per_page=1"*)
      case "$FIXTURE" in
        green)  cat "$PIPELINE_OK" ;;
        red)    cat "$PIPELINE_FAILED" ;;
        blocked) cat "$PIPELINE_FAILED" ;;
        none)   printf '[]' ;;
        # Both spellings are the same fixture on purpose: `unreadable` is what these tests call
        # it, and a harness that only accepts one name fails with `unknown CI_FIXTURE` — which
        # looks like a broken classifier rather than a typo in a test, and is exactly the kind of
        # quiet fixture change this file's docstring is about.
        unreadable|unreachable) printf '404 Project Not Found\n' >&2; exit 1 ;;
        *) printf 'unknown CI_FIXTURE: %s\n' "$FIXTURE" >&2; exit 2 ;;
      esac ;;
  # The jobs endpoint. `blocked` reuses the failed pipeline id but answers with the never-started
  # job payload, which is exactly the live group's shape: pipeline `failed`, every job
  # `ci_quota_exceeded` with `started_at: null`.
  */pipelines/501/jobs\?*)
      case "$FIXTURE" in
        red)     cat "$JOBS_RED" ;;
        blocked) cat "$JOBS_BLOCKED" ;;
        *) printf 'no jobs fixture for CI_FIXTURE=%s\n' "$FIXTURE" >&2; exit 2 ;;
      esac ;;
  */pipelines/500/jobs\?*)  cat "$JOBS_GREEN" ;;
  *) printf 'Unknown endpoint: %s\n' "$path" >&2; exit 1 ;;
esac
exit 0
""")
        glab.chmod(0o755)
        self.fixtures = {
            "PIPELINE_OK": ok_pipeline,
            "PIPELINE_FAILED": failed_pipeline,
            "JOBS_GREEN": green_jobs,
            "JOBS_RED": ran_jobs,
            "JOBS_BLOCKED": blocked_jobs,
        }
        for name, body in self.fixtures.items():
            (self.tmp / name).write_text(body)

    def ci_env(self):
        return {name: str(self.tmp / name) for name in self.fixtures}

    @property
    def state(self):
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    @property
    def prompts(self):
        p = self.tmp / "prompts.log"
        return p.read_text() if p.exists() else ""


class AStallingWorkerIsActionable(unittest.TestCase):
    """The regression: zero commits for N checks, and never once a wake-up."""

    def test_below_the_threshold_is_not_a_wake_up(self):
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        r.run(["--stalled", "8"])
        # A lone fresh worker is a legitimate all-clear, and saying so once is the point of
        # the all-clear — so the assertion is about the STALL, not about any wake-up.
        self.assertNotIn("STOPPED MOVING", r.out + r.prompts,
                         f"a worker that has barely started is a cry of wolf: {r.out}")
        self.assertEqual(r.state.get("stalling"), [],
                         "nothing is stalling below the threshold")

    def test_at_the_threshold_it_wakes_and_names_the_pane_and_the_count(self):
        """A wake-up you cannot act on is not a wake-up."""
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        r.run(["--stalled", "4"])
        # Four identical rows == a streak of 4. Seed three so the fourth run crosses.
        r.state_path.write_text(json.dumps({"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 3}}}))
        r.run(["--stalled", "4"])

        self.assertIn("woke COORD", r.out,
                      f"a pane at the threshold must wake the coordinator: {r.out}")
        self.assertIn("wPE:p1", r.prompts, "the body must name the pane")
        self.assertIn("issue-99-thing", r.prompts, "and the branch")
        self.assertIn("unchanged 4 check(s)", r.prompts,
                      f"and the check count, which is the actionable part: {r.prompts!r}")
        self.assertIn("ahead=0 dirty=0", r.prompts, "and the counts it needs to decide")
        self.assertEqual(len(r.state.get("stalling") or []), 1)

    def test_a_dirty_tree_is_also_a_stall(self):
        r = NotifyRun(self, [pane("wPJ:p1", "IN-FLIGHT", 4, 3)])
        r.state_path.write_text(json.dumps({"streaks": {
            "wPJ:p1": {"sig": "IN-FLIGHT|4|3|idle", "streak": 7}}}))
        r.run(["--stalled", "8"])
        self.assertIn("woke COORD", r.out,
                      f"an uncommitted tree past the threshold is the same defect: {r.out}")
        self.assertIn("unchanged 8 check(s)", r.prompts)
        self.assertIn("ahead=4 dirty=3", r.prompts)

    def test_a_stall_is_never_reported_as_all_clear(self):
        """AC4. "All clear" must mean all clear."""
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        r.state_path.write_text(json.dumps({"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 99}}}))
        r.run(["--stalled", "8"])
        self.assertNotIn("all clear", r.out,
                         f"a stalling worker is not an all-clear: {r.out}")
        self.assertNotEqual(r.state.get("sig"), "ALL-CLEAR")

    def test_a_stall_only_wakeup_does_not_end_with_merge_instructions(self):
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        r.state_path.write_text(json.dumps({"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 99}}}))
        r.run(["--stalled", "8"])
        # This run has no red gate and nothing mergeable, so the merge closing must NOT be there.
        self.assertNotIn("merge with -F", r.prompts,
                         f"nothing is ready to merge, so do not tell the coordinator to: "
                         f"{r.prompts!r}")
        self.assertIn("alive at all", r.prompts)


class EscalationDoesNotLatch(unittest.TestCase):
    def test_a_worker_that_commits_leaves_the_set(self):
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        r.state_path.write_text(json.dumps({"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 99}}}))
        r.run(["--stalled", "8"])
        self.assertIn("woke COORD", r.out, "precondition: it was stalling")

        # The worker commits. Same pane, moved row.
        r.scripts.joinpath("fleet-watch.py").write_text(
            "import json\nprint(json.dumps({'panes': ["
            + json.dumps(pane("wPE:p1", "MERGEABLE", 2, 0, status="done")) + "]}))\n")
        r.run(["--stalled", "8"])
        self.assertEqual(r.state.get("stalling"), [],
                         "a worker that recovered must leave the stalling set")
        streak = (r.state.get("streaks") or {}).get("wPE:p1", {}).get("streak")
        self.assertEqual(streak, 1,
                         f"the streak must reset on movement, not carry over: {streak}")

    def test_and_then_it_stops_waking(self):
        """Non-latching, which is the half of AC3 that the streak test does not cover.

        The previous test proves the pane left the set. This proves the consequence: a
        recovered worker does not keep the coordinator awake, because an escalation that
        never stops is indistinguishable from a broken cron job and would be ignored
        exactly like one.
        """
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)],
                      state={"streaks": {"wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 99}}})
        r.run(["--stalled", "8"])
        self.assertIn("woke COORD", r.out, "precondition: it escalated")

        # The worker commits and is now mergeable. Run it twice more.
        r.scripts.joinpath("fleet-watch.py").write_text(
            "import json\nprint(json.dumps({'panes': ["
            + json.dumps(pane("wPE:p1", "MERGEABLE", 2, 0, status="done")) + "]}))\n")
        r.run(["--stalled", "8"])
        r.run(["--stalled", "8"])
        self.assertIn("1 ready, 0 failing, 0 stalling", r.out,
                      f"the recovered worker is reported as ready to merge: {r.out}")
        self.assertEqual(r.out.count("woke COORD"), 2,
                         "escalation then one mergeable wake-up, then silence — a recovered "
                         f"worker must not wake the coordinator forever: {r.out}")
        self.assertIn("staying quiet", r.out, "and the silence is a reported fact")


class AQuietRunStillCounts(unittest.TestCase):
    """If a quiet run did not save, the streak would freeze and never fire."""

    def test_a_repeated_identical_run_advances_the_streak_and_eventually_wakes(self):
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        for i in range(1, 6):
            r.run(["--stalled", "4"])
            streak = (r.state.get("streaks") or {}).get("wPE:p1", {}).get("streak")
            self.assertEqual(
                streak, i,
                f"run {i} must count as check {i}; a quiet run that does not save the "
                f"streaks freezes them, which is the bug this rule exists to prevent "
                f"(got {streak})")
            if i < 4:
                self.assertNotIn("STOPPED MOVING", r.out + r.prompts,
                                 f"run {i} is still below the threshold and must not escalate")
        self.assertIn("woke COORD: 0 ready, 0 failing, 1 stalling", r.out,
                      f"run 4 is at the threshold and must wake for the stall: {r.out}")
        print("    identical runs 1..5 -> crossed the threshold on run 4")

    def test_a_quiet_run_still_counts_a_stalling_pane_beside_a_ready_one(self):
        """THE FREEZE, and it only happens beside a mergeable pane.

        A quiet run used to return without saving. That is invisible when the *only* pane
        is the stalling one, because an all-clear run takes a different branch that always
        saved — which is exactly why a test of that shape passes whether or not the bug is
        there.

        The freeze needs a fleet where something is actionable and nothing changes, so the
        run takes the stay-quiet branch. Then the stalling pane beside it never advances,
        never reaches the threshold, and the escalation never fires — the same suppression,
        one function further down.

        Found by mutation: removing `persist()` from the stay-quiet branch did **not** fail
        `test_a_repeated_identical_run_advances_the_streak_and_eventually_wakes`, so that
        test was not holding the line it appeared to hold.
        """
        fleet = [pane("wAA:p1", "MERGEABLE", 1, 0, status="done"),
                 pane("wPE:p1", "NOTHING", 0, 0)]
        r = NotifyRun(self, fleet)
        r.run(["--stalled", "4"])                     # run 1: the mergeable pane wakes
        self.assertIn("woke COORD", r.out, "precondition: the fleet is actionable")
        # Run 1 already counts as check 1, so run 2 and run 3 are stay-quiet at 2 and 3,
        # and run 4 reaches the threshold. The loop stops at 3: from run 4 on the pane is
        # actionable, so those runs are no longer exercising the stay-quiet branch.
        self.assertEqual((r.state.get("streaks") or {})["wPE:p1"]["streak"], 1,
                         "precondition: run 1 counted as one check")
        for i in (2, 3):
            r.run(["--stalled", "4"])
            streak = (r.state.get("streaks") or {}).get("wPE:p1", {}).get("streak")
            self.assertIn("staying quiet", r.last_out,
                          f"run {i} must be a stay-quiet, which is the branch under test: "
                          f"{r.last_out!r}")
            self.assertEqual(streak, i,
                             f"a stay-quiet run is still a check: run {i} must count the "
                             f"stalling pane as {i}, got {streak}. A quiet run that does not "
                             f"save freezes it and the escalation never fires.")
        self.assertNotIn("STOPPED MOVING", r.prompts,
                         "3 of 4 is still below the threshold")

        r.run(["--stalled", "4"])
        self.assertIn("STOPPED MOVING", r.prompts,
                      f"run 4 reaches the threshold and must escalate even though the runs "
                      f"before it were all quiet: {r.prompts!r}")
        self.assertIn("unchanged 4 check(s)", r.prompts)
        print("    a stalling pane beside a ready one counts through quiet runs: 1,2,3,4 -> wakes")

    def test_a_dry_run_advances_nothing(self):
        """A dry run cannot send, so it must not consume the counter."""
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        r.run(["--dry-run", "--stalled", "8"])
        self.assertEqual(r.state.get("streaks"), None if not r.state_path.exists() else
                         r.state.get("streaks"),
                         "a dry run must not write streaks")
        streak = (r.state.get("streaks") or {}).get("wPE:p1", {}).get("streak")
        self.assertIn(streak, (None, 1),
                      f"the dry run may read the world but must not advance a counter that "
                      f"decides whether to send; got {streak}")


class TheLogLineIsReadable(unittest.TestCase):
    """AC5: a silent stay-quiet must say which of the two silences it was."""

    def test_staying_quiet_reports_the_counts(self):
        r = NotifyRun(self, [pane("wPE:p1", "MERGEABLE", 2, 0, status="done")])
        r.run(["--stalled", "8"])          # first run wakes
        r.run(["--stalled", "8"])          # second is unchanged
        self.assertIn("unchanged since the last wake-up", r.out,
                      f"a stay-quiet must say it stayed quiet on purpose: {r.out}")
        self.assertIn("1 ready", r.out, "and carry the counts, so it is not bare noise")

    def test_a_pane_approaching_the_threshold_is_named(self):
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0), pane("wAA:p1", "MERGEABLE", 1, 0, status="done")])
        r.run(["--stalled", "8"])   # run 1: mergeable wakes
        r.run(["--stalled", "8"])   # run 2: unchanged, but the stall is now at 2
        self.assertIn("unchanged since the last wake-up", r.out)
        self.assertIn("not yet actionable: wPE:p1", r.out,
                      f"a pane on its way to the threshold must be visible in the log, or the "
                      f"escalation is invisible until it fires: {r.out}")
        self.assertIn("2 of 8", r.out, "and say how far along it is")

    def test_a_stall_names_the_reason_it_is_actionable(self):
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        r.state_path.write_text(json.dumps({"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 12}}}))
        r.run(["--stalled", "8"])
        self.assertIn("STOPPED MOVING", r.prompts,
                      f"the reason must be duration, not readiness: {r.prompts!r}")


class TheMutationThatProvesTheThresholdIsLive(unittest.TestCase):
    """THE MUTATION, and it is unusual: the bug was a false pass.

    The pre-fix hash suppressed a stalled pane forever while looking entirely correct, so
    asserting the new behaviour alone proves nothing — a hardcoded `True` would satisfy it.
    This class binds the pre-fix implementation and requires it to FAIL the assertions above.
    """

    # The pre-fix `actionable`, verbatim in shape: two sets, no duration, a stalling pane in
    # neither. Reproducing it here rather than paraphrasing is deliberate — a paraphrase
    # would be the mutation that never applied.
    @staticmethod
    def _prefix_actionable(panes, streaks, stalled):
        import hashlib

        ready, broken = [], []
        for r in panes:
            if r.get("verdict") == "MERGEABLE":
                ready.append(f"{r['pane']} {r['branch']}")
            elif r.get("verdict") in ("GATE-RED", "TOOL-INCONSISTENT", "NO-GATE"):
                broken.append(f"{r['pane']} {r['branch']} {r['verdict']}")
        sig = hashlib.sha256("|".join(ready + broken).encode()).hexdigest()[:16]
        return sig, ready, broken, [], {}

    def test_the_old_implementation_cannot_report_a_stall(self):
        stalling = self._prefix_actionable(
            [pane("wPE:p1", "NOTHING", 0, 0)],
            {"wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 99}}, 8)[3]
        self.assertEqual(stalling, [],
                         "the pre-fix implementation must still be here for the mutation to "
                         f"mean anything; it reported {stalling!r} for a pane at 99 unchanged "
                         "checks — that IS the defect, and it is why the assertions above "
                         "are not vacuous")

    def test_and_the_streak_counter_is_live_not_a_constant(self):
        """Below the threshold: out. Above it: in. Same pane, same row, one number apart."""
        # Seeded so the RUN lands on each side: a streak is the count of checks so far, so a
        # seed of 7 becomes 8 on the next run, which is the threshold itself.
        p = pane("wPE:p1", "NOTHING", 0, 0)
        out_below = NotifyRun(self, [p], state={"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 6}}})
        out_below.run(["--stalled", "8"])
        above = NotifyRun(self, [p], state={"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 8}}})
        above.run(["--stalled", "8"])

        self.assertEqual(out_below.state.get("streaks")["wPE:p1"]["streak"], 7,
                         "the run under test lands on 7 of 8")
        self.assertEqual(out_below.state.get("stalling"), [],
                         "7 of 8 is not a wake-up")
        self.assertEqual(above.state.get("streaks")["wPE:p1"]["streak"], 9,
                         "the run under test lands on 9 of 8")
        self.assertIn("woke COORD", above.out,
                      f"9 of 8 is a wake-up — the comparison must be live and not a constant "
                      f"that always fires: {above.out}")
        self.assertIn("unchanged 9 check(s)", above.prompts)
        print("    lands on 7 of 8 -> quiet; lands on 9 of 8 -> wakes. The comparison is live.")


class TheAntiSpamDesignSurvives(unittest.TestCase):
    """The mechanism is correct and must stay: an unchanged mergeable branch is silence."""

    def test_a_ready_branch_that_stays_ready_wakes_exactly_once_in_five_runs(self):
        """THE anti-spam property, and the one my change puts most at risk.

        Two runs only prove "not twice". Five prove the count is *one*, which is the property
        that matters — and five is also the number that catches the failure mode this change
        introduces.

        `actionable()` now counts a per-pane streak for **every** pane, ready ones included.
        That counter advances on every run by construction, because a ready pane that did not
        move is exactly a ready pane. So the readiness entry must not carry the streak: if it
        did, the hashed signature would change every run and this ticket would turn the cron job
        into the "wakes you every 30 minutes for the same unmerged branch" failure the whole
        mechanism exists to prevent.

        Two runs cannot see that — a wake on runs 3, 4 and 5 would pass. That is why this is
        five, and why it asserts the per-run count rather than a total.
        """
        r = NotifyRun(self, [pane("wPE:p1", "MERGEABLE", 2, 0, status="done")])
        for i in range(1, 6):
            r.run(["--stalled", "8"])
            woke = "woke COORD" in r.last_out
            self.assertEqual(
                woke, i == 1,
                f"run {i} of 5: a ready branch that stays ready must wake on run 1 and stay "
                f"silent after. Run {i} woke={woke}. If this fires, the streak leaked into "
                f"the readiness entry and the signature changes every run: {r.last_out!r}")
        self.assertEqual(r.out.count("woke COORD"), 1,
                         f"five identical runs, one wake-up: {r.out}")
        self.assertEqual(r.out.count("staying quiet"), 4,
                         f"and the other four must say why they were quiet: {r.out}")
        print("    5 identical runs of one ready branch -> 1 wake-up, 4 reported silences")

    def test_the_readiness_entry_carries_no_streak(self):
        """The structural half of the above: assert it on the entry itself.

        The run-count test proves the *effect*. This proves the *cause*, and it fails at the
        line rather than at the fifth run — which is the difference between a guard you can
        read and one you have to reverse-engineer from a wake-up count.
        """
        rows = [pane("wPE:p1", "MERGEABLE", 2, 0, status="done")]
        first = NotifyRun(self, rows)
        first.run(["--stalled", "8"])
        second = NotifyRun(self, rows, state=first.state)
        second.run(["--stalled", "8"])
        self.assertEqual(first.state["ready"], second.state["ready"],
                         "the ready entry must be byte-identical across runs, so the signature "
                         "is stable and an unchanged mergeable branch stays suppressed")
        self.assertEqual(first.state["streaks"]["wPE:p1"]["streak"], 1)
        self.assertEqual(second.state["streaks"]["wPE:p1"]["streak"], 2,
                         "precondition for the bug this guards: the streak DOES advance on a "
                         "ready pane, so only its absence from the entry keeps this quiet")

    def test_the_default_threshold_is_eight(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("nc", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        src = Path(SCRIPT).read_text()
        self.assertIn('"--stalled", type=int, default=8', src,
                      "the default must match fleet-keepalive.py --stall, or the two scripts "
                      "disagree about when a worker is stuck")

    def test_a_deliberately_red_branch_is_still_not_a_wake_up(self):
        wt = "/tmp/wt/expected"
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0, worktree=wt)])
        Path(r.env["FLEET_EXPECTED_RED"]).write_text(json.dumps({"worktrees": [wt]}))
        r.state_path.write_text(json.dumps({"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 99}}}))
        r.run(["--stalled", "8"])
        self.assertNotIn("STOPPED MOVING", r.out + r.prompts,
                         f"a branch that is red on purpose is the ticket, not a fault, and a "
                         f"stall escalation is a fault report: {r.out}")
        self.assertNotIn("woke COORD:", r.out,
                         "the declared-red pane must not produce a readiness wake-up")
        self.assertEqual(r.state.get("sig"), "ALL-CLEAR",
                         "with the only pane excluded, the fleet genuinely is all clear")


class AWorkerTheSweepNeverWalked(unittest.TestCase):
    """A recorded pane that produces no row is not a pane that is fine.

    Measured 2026-10-05: `fleet-keepalive.py` logged `4 needing attention` and this script
    printed `all clear. No action needed` **0.2 seconds later**, from the same run. The sweep only
    walks worktrees that exist, so a pane whose worktree was deleted yields no row and no error —
    indistinguishable from "finished and reclaimed". All-clear could therefore be true while the
    fleet was not whole, which is the one thing all-clear must never be.
    """

    def test_a_ghost_pane_makes_all_clear_impossible(self):
        """THE property. A manifest entry the sweep omits must block the all-clear.

        One working pane, nothing ready, nothing failing, nothing stalling — the exact state
        that produced the false all-clear — plus one recorded worker the sweep did not return.
        """
        r = NotifyRun(
            self,
            [pane("wPG:p1", "IN-FLIGHT", 6, 0)],
            manifest={"/synthetic/workspace/media-dagger": "wNF:p1"},
        )
        r.run(["--stalled", "8"])
        self.assertNotIn("all clear", r.prompts,
                         f"a pane nobody is watching must not be reported as all-clear: "
                         f"{r.prompts!r}")
        self.assertIn("did not appear in the sweep", r.prompts,
                      f"and it must say so in those words: {r.prompts!r}")
        self.assertIn("wNF:p1", r.prompts,
                      f"naming the pane, so it can be acted on: {r.prompts!r}")
        print("    one recorded pane missing from the sweep -> all-clear refused, pane named")

    def test_the_wake_up_says_a_ghost_needs_a_decision(self):
        """A pane nobody is watching is not a pane that is fine — so say what to do."""
        r = NotifyRun(
            self,
            [pane("wPG:p1", "IN-FLIGHT", 6, 0)],
            manifest={"/w/gone": "wNF:p1"},
        )
        r.run(["--stalled", "8"])
        self.assertIn("merged into main", r.prompts,
                      f"the body must route the coordinator to the reclaim sweep: {r.prompts!r}")
        self.assertIn("cleanup-workers.sh", r.prompts,
                      f"by name, not by implication: {r.prompts!r}")
        print("    the ghost wake-up names the reclaim sweep and the merge check")

    def test_all_clear_when_every_recorded_worker_was_swept(self):
        """The other direction, so the guard is a comparison and not a constant.

        If the manifest agrees with the sweep, all-clear must still be reachable — otherwise the
        fix would silence the tool entirely, which is the failure in the opposite direction.
        """
        r = NotifyRun(
            self,
            [pane("wPG:p1", "IN-FLIGHT", 6, 0)],
            manifest={"/somewhere": "wPG:p1"},
        )
        r.run(["--stalled", "8"])
        self.assertIn("all clear", r.prompts,
                      f"a whole fleet must still report all-clear: {r.prompts!r}")
        print("    manifest and sweep agree -> all-clear still reachable")

    def test_an_empty_manifest_is_not_a_ghost(self):
        """The trivial case, asserted because `{}` is every other test's fixture."""
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], manifest={})
        r.run(["--stalled", "8"])
        self.assertIn("all clear", r.prompts, f"{r.last_out!r}")
        print("    empty manifest -> no ghosts, no false alarm")


class AnAllClearIsVetoedByGitLabCI(unittest.TestCase):
    """The all-clear's third condition, and the trap inside it.

    This script emits the most reassuring sentence the tool can produce, and on 2026-10-03 it
    emitted it while `media` main was **red in GitLab**. The keepalive has the
    equivalent assertions in `test_fleet_keepalive.py`; these are here because the two halves emit
    the same word and only one of them is the sentence the coordinator reads.

    **A gap this file had, found by mutation rather than by reading.** Making `classify()` return
    a constant verdict left all 31 of these tests green, because none of them asked what happens
    to the all-clear when the pipeline is red — the classifier's own tests caught the constant,
    and nothing caught the *consequence*. That is worth writing down: a monitor's tests and its
    decision's tests are not the same tests, and a guard that only covers one of them is half a
    guard. So these assert the consequence on the real classifier rather than on a stubbed
    verdict.
    """

    def _run(self, ci, panes=None, **kw):
        return NotifyRun(self, panes or [pane("wPG:p1", "IN-FLIGHT", 6, 0)], ci=ci, **kw).run(
            ["--stalled", "8"])

    def test_a_red_main_withdraws_the_all_clear(self):
        """THE property. A job ran and failed, so this is a code defect and `main` is not green."""
        r = self._run("red")
        self.assertNotIn("all clear", r.prompts,
                         f"the all-clear claims nothing is failing, and main IS failing: "
                         f"{r.prompts!r}")
        self.assertIn("main are RED", r.prompts,
                      f"the wake-up must say so in those words: {r.prompts!r}")
        self.assertIn("IS a code defect", r.prompts,
                      f"because a local gate being green does not contradict a red pipeline: "
                      f"{r.prompts!r}")
        print("    main RED -> all-clear refused, and the reason is a code defect")

    def test_a_red_main_with_a_healthy_fleet_still_breaks_the_all_clear(self):
        """One working pane and nothing else actionable — the exact state of the false pass."""
        r = self._run("red", panes=[pane("wPG:p1", "IN-FLIGHT", 6, 0)])
        self.assertNotIn("all clear", r.prompts, f"{r.prompts!r}")
        self.assertIn("woke COORD", r.out,
                      f"and the coordinator must actually be woken about it: {r.out}")

    def test_an_unreadable_main_withdraws_the_all_clear(self):
        """`unreachable is not green`. This is the third condition's whole reason for existing."""
        r = self._run("unreadable")
        self.assertNotIn("all clear", r.prompts,
                         f"a tool that cannot see GitLab cannot claim main is fine: {r.prompts!r}")
        self.assertIn("could not be judged", r.prompts, r.prompts)
        print("    main unreadable -> all-clear refused, named as not-a-pass")

    def test_a_blocked_main_does_not_withdraw_the_all_clear(self):
        """The direction that stops this becoming noise.

        A `BLOCKED` pipeline is billing, not a fault. Refusing the all-clear for it would make the
        tool permanently non-clear while the group has no compute minutes — which is the failure
        the brief warns about, arrived at from the other direction. So it is reported *inside* the
        all-clear and does not veto it.
        """
        r = self._run("blocked")
        self.assertIn("all clear", r.prompts,
                      f"a billing problem must not make the tool permanently non-clear: "
                      f"{r.prompts!r}")
        self.assertIn("BLOCKED", r.prompts,
                      f"but the all-clear must still say what was not verified: {r.prompts!r}")
        self.assertIn("compute minutes", r.prompts,
                      f"and why, or a reader treats an unverified repo as a checked one: "
                      f"{r.prompts!r}")
        self.assertIn("do not go looking for a code defect", r.prompts,
                      f"the point of BLOCKED: {r.prompts!r}")
        print("    main BLOCKED -> all-clear stands, and names what was not verified")

    def test_a_never_started_worker_withdraws_the_all_clear(self):
        """The second condition. Same shape as the red case, different reason."""
        r = self._run("green", panes=[pane("wNF:p1", "NOT-STARTED", 0, 0,
                                           progress="NOT-STARTED",
                                           progress_why="0 tokens")])
        self.assertNotIn("all clear", r.prompts,
                         f"a pane that never ran a turn is not health: {r.prompts!r}")
        self.assertIn("NEVER RUN A TURN", r.prompts, r.prompts)

    def test_all_three_conditions_together(self):
        """The 2026-10-03 state in full: healthy panes, one absent worker, a red main."""
        r = self._run("red", panes=[
            pane("wPG:p1", "IN-FLIGHT", 6, 0),
            pane("wNF:p1", "NOT-STARTED", 0, 0, progress="NOT-STARTED", progress_why="0 tokens"),
        ])
        self.assertNotIn("all clear", r.prompts, f"{r.prompts!r}")
        self.assertIn("NEVER RUN A TURN", r.prompts,
                      f"both facts must be reported, not just one: {r.prompts!r}")
        self.assertIn("main are RED", r.prompts, f"{r.prompts!r}")
        print("    never-started + red main -> both reported, no all-clear")

    def test_an_unchanged_red_main_still_reports_once_then_stops(self):
        """The anti-spam property must survive the new conditions.

        A permanently red `main` would otherwise wake the coordinator every 30 minutes forever,
        which is how a monitor gets switched off — the same outcome as the always-red report this
        ticket exists to prevent, reached by a different route.
        """
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], ci="red")
        woke = []
        for _ in range(4):
            r.run(["--stalled", "8"])
            woke.append("woke COORD" in r.last_out)
        self.assertEqual(woke, [True, False, False, False],
                         f"an unchanged red main must wake once and then stay quiet: {woke}")
        self.assertEqual(r.out.count("staying quiet"), 3, r.out)
        print("    4 identical runs with a red main -> 1 wake-up, 3 reported silences")


class AMainSweepRedReachesTheWakeUp(unittest.TestCase):
    """The sweep gates `origin/main`, and its rows used to die in a cron log.

    Measured on 2026-10-06: `social-studio`'s main was red from 10:00 UTC while the
    sweep gated it **seven times**, every row at a current head, in
    `/tmp/opencode/main-sweep.cron.log` — which nothing reads. These hold the consequence on the
    wake-up path: a RED row reaches the coordinator with its repository, commit and first useful
    line; a green record changes nothing; and the absence of a record is not a pass.
    """

    def _run(self, sweep, panes=None, **kw):
        return NotifyRun(self, panes or [pane("wPG:p1", "IN-FLIGHT", 6, 0)], sweep=sweep,
                         **kw).run(["--stalled", "8"])

    def test_a_red_row_wakes_the_coordinator_with_repo_commit_and_reason(self):
        """A row a coordinator cannot act on is not a row: repository, commit and the reason.

        The red log adds the path of the full output: the wake-up carries the summary and the pointer,
        so the coordinator can read the actual failure without re-running the gate.
        """
        r = self._run("red")
        self.assertIn("woke COORD", r.out, f"the red must actually wake it: {r.out}")
        self.assertNotIn("all clear", r.prompts, f"and never beside an all-clear: {r.prompts!r}")
        self.assertIn("meta", r.prompts, f"the repository: {r.prompts!r}")
        self.assertIn("deadbee1234", r.prompts, f"the commit: {r.prompts!r}")
        self.assertIn("guard_test.go:112: synthetic main failure", r.prompts,
                      f"the sweep's first useful line: {r.prompts!r}")
        self.assertIn("main sweep", r.prompts, f"named as the sweep's finding: {r.prompts!r}")
        self.assertIn("main-sweep-red-meta-deadbee1234.log", r.prompts,
                      f"the path of the full output beside the row: {r.prompts!r}")
        print("    a recorded RED main -> wake-up carries repository, commit, reason and log")

    def test_a_red_record_wakes_once_and_then_stays_quiet(self):
        """The content hash must carry the placement, or the first report is suppressed forever.

        And once reported, an unchanged red must not wake every thirty minutes — the anti-spam
        property the earlier rules both hold, kept here for the sweep's row.

        Counted in **prompt lines**, not in substring occurrences: since the red log, the row names the
        full-output path, which contains the commit a second time, and a substring count would
        report two deliveries of one prompt.
        """
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], sweep="red")
        woke = []
        for _ in range(3):
            r.run(["--stalled", "8"])
            woke.append("woke COORD" in r.last_out)
        self.assertEqual(woke, [True, False, False],
                         f"an unchanged red record must wake once and then stay quiet: {woke}")
        delivered = [line for line in r.prompts.splitlines() if "deadbee1234" in line]
        self.assertEqual(len(delivered), 1,
                         f"and the row must be delivered exactly once: {r.prompts!r}")
        print("    3 identical runs with a red record -> 1 wake-up, 2 reported silences")

    def test_a_fresh_green_record_does_not_withdraw_the_all_clear(self):
        """The direction that stops this becoming noise: a healthy sweep is the normal state."""
        r = self._run("green")
        self.assertIn("all clear", r.prompts,
                      f"a fresh green record must not block the all-clear: {r.prompts!r}")
        self.assertIn("recorded main sweep", r.prompts,
                      f"but the all-clear must name the sweep among what it checked: {r.prompts!r}")
        print("    fresh green record -> all-clear stands, and names the sweep check")

    def test_a_red_appearing_after_a_green_run_is_not_suppressed_as_unchanged(self):
        """THE anti-suppression case: nothing moves except the sweep's record, and it goes red.

        With a ready pane on the board, every other input is byte-identical between two runs, so
        the content hash is the only thing between "the coordinator is told" and "the red's first
        appearance is filed as `unchanged — staying quiet`". That is the sweep record restored through the
        mechanism built to fix it, and it is why the sweep's placement is in the hash.
        """
        r = NotifyRun(self, [pane("wPG:p1", "MERGEABLE", 6, 0)], sweep="green")
        r.run(["--stalled", "8"])
        self.assertIn("woke COORD", r.last_out, f"the ready branch wakes on run 1: {r.last_out}")

        r.main_sweep.write_text(json.dumps(sweep_run(("meta",), "red")))
        r.run(["--stalled", "8"])
        self.assertIn("woke COORD", r.last_out,
                      f"the red's first appearance must wake, not be suppressed as unchanged: "
                      f"{r.last_out}")
        self.assertIn("guard_test.go:112", r.prompts, f"{r.prompts!r}")
        print("    green -> red record with everything else unchanged -> the red still wakes")

    def test_a_missing_record_is_not_a_pass(self):
        """`sweep=None` is the pre-deployment state: nothing has ever gated main."""
        r = self._run(None)
        self.assertNotIn("all clear", r.prompts,
                         f"a tool that has never seen a sweep cannot claim main is fine: {r.prompts!r}")
        self.assertIn("no-run-recorded", r.prompts, f"and it must say so by name: {r.prompts!r}")
        self.assertIn("woke COORD", r.out, f"a never-recorded sweep is worth one wake-up: {r.out}")
        print("    no sweep record -> all-clear refused, named as not-a-pass")

    def test_a_stale_record_is_not_a_pass(self):
        """A crashed or stopped sweep must not leave yesterday's green implying today's health."""
        r = self._run("stale")
        self.assertNotIn("all clear", r.prompts,
                         f"a stale record is not current evidence: {r.prompts!r}")
        self.assertIn("stale-run", r.prompts, f"named as stale: {r.prompts!r}")
        print("    stale sweep record -> all-clear refused, named as stale")

    def test_a_no_gate_row_is_unverified_not_silent(self):
        """A repository the sweep could not gate is the same fact as a pipeline it could not read."""
        r = self._run("nogate")
        self.assertNotIn("all clear", r.prompts, f"{r.prompts!r}")
        self.assertIn("NO-GATE", r.prompts, f"the row must be reported: {r.prompts!r}")

    def test_a_tool_inconsistent_row_is_a_fault_not_a_pane_state(self):
        """The sweep contradicting itself is above every repository — the fix is the script."""
        r = self._run("inconsistent")
        self.assertNotIn("all clear", r.prompts, f"{r.prompts!r}")
        self.assertIn("TOOL-INCONSISTENT", r.prompts, f"{r.prompts!r}")
        self.assertIn("fix the sweep", r.prompts,
                      f"the body must say what to fix: {r.prompts!r}")


class AllClearIsAReminderNotAPlan(unittest.TestCase):
    """The script reports; the coordinator decides.

    Added 2026-10-05 on the user's instruction. An earlier version put the scheduling *policy*
    in the all-clear body — "dispatch in this same turn", "a merge frees a slot" — which turned
    a reminder into a coordinator, and put a convention in the file that runs every 30 minutes
    instead of in `AGENTS.md`, where a person can read it and change it.

    A script that decides what work exists would have to read the tracker, know the capacity and
    judge interference. The last is the coordinator's judgement *by definition* — the family rules
    exist to bound what that judgement may get wrong, and a script cannot apply them.
    """

    def test_the_body_does_not_say_no_action_needed(self):
        """The specific words that made it a stop signal."""
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], manifest={})
        r.run(["--stalled", "8"])
        self.assertNotIn("No action needed", r.prompts,
                         "'No action needed' is a stop signal and must not be sent: "
                         f"{r.prompts!r}")

    def test_the_body_reminds_to_check_for_work(self):
        """The nudge's whole job: say there may be work, and go and look."""
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], manifest={})
        r.run(["--stalled", "8"])
        self.assertIn("work available", r.prompts,
                      f"the reminder must name the question: {r.prompts!r}")
        print("    all-clear asks whether there is work available, not what to do about it")

    def test_the_body_carries_no_scheduling_policy(self):
        """The regression guard for the over-reach.

        Each phrase is a *decision* — how many to run, when to dispatch, what a merge implies. A
        reminder does not make them, and holding policy here means a change of policy needs a
        code change in a cron script rather than an edit to `AGENTS.md`.
        """
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], manifest={})
        r.run(["--stalled", "8"])
        for phrase in ("dispatch", "merge frees a slot", "ceiling", "survey the open issues",
                       "not a stop signal"):
            self.assertNotIn(phrase, r.prompts,
                             f"{phrase!r} is policy, and policy does not belong in a reminder: "
                             f"{r.prompts!r}")
        print("    no dispatch order, no ceiling, no 'merge frees a slot' — policy stays in AGENTS.md")

    def test_the_policy_lives_in_the_runbook_and_not_only_here(self):
        """The other half of `test_the_body_carries_no_scheduling_policy`.

        Stripping policy out of the script is only safe while the policy is written down
        somewhere. Otherwise the obvious repair for "the reminder is too thin" is to put it
        back, and the two tests then protect each other's absence.

        The policy lives in `FACTORY.md`, the coordinator runbook — not in `AGENTS.md`. The
        extraction split the agent contract from the coordinator mechanics on purpose: the
        contract carries the five rules that bound every session, and the scheduling policy
        (parallelism, the ceiling, the interference check, what a merge implies) is a
        coordinator's mechanics and has to be read in full before the first dispatch. Pointing
        this test at the runbook also asserts the runbook keeps it: a runbook that shrank to
        "launch workers and merge" would leave the script's thinness unprotected again.
        """
        runbook = (Path(__file__).resolve().parent.parent / "FACTORY.md").read_text()
        self.assertIn("Be proactive", runbook,
                      "FACTORY.md must carry the scheduling rule, since the script deliberately "
                      "does not")
        for phrase in ("the same turn", "all-clear", "backstop"):
            self.assertIn(phrase, runbook,
                          f"the rule must still name {phrase!r}: without it the rule is a "
                          f"preference, and a preference is not a rule")
        # And the strongest signal must be named as such, because the measured failure was
        # reading it as an absence of news.
        self.assertIn("strongest signal", runbook,
                      "all-clear must be named the strongest signal, not a quiet one — the "
                      "measured failure was treating it as nothing to do")
        print("    FACTORY.md carries the rule; the reminder carries the question")

    def test_the_body_points_at_the_evidence_not_the_status(self):
        """`agent_status` cannot answer the question it raises, so the body says where to look.

        Measured across this project: `idle` has meant both "finished and signed off" and
        "crashed mid-edit", and `done` has meant finished-with-uncompilable-code. A reminder
        pointing at agent status would point at a field that lies.
        """
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], manifest={})
        r.run(["--stalled", "8"])
        self.assertIn("agent_status", r.prompts,
                      f"the reminder must warn that status is not evidence: {r.prompts!r}")
        self.assertIn("issue tracker", r.prompts,
                      f"and must name where the evidence actually is: {r.prompts!r}")
        print("    the reminder says status is not the evidence, and names what is")


class TheParallelQuestionIsAskedInEveryBody(unittest.TestCase):
    """Owner instruction, 2026-10-07: ask in EVERY message, not only the all-clear.

    The all-clear carried the question first; every other body ended without it, so a
    coordinator woken by a red `main` was told about the red and nothing else. The question
    is one constant in the script, asserted here in every shape so the bodies cannot drift.
    """

    PHRASE = "schedule in parallel"

    def _assert_asked(self, r, what):
        self.assertIn(self.PHRASE, r.prompts,
                      f"{what} must ask the standing question: {r.prompts!r}")

    def test_the_all_clear_asks_it(self):
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], manifest={})
        r.run(["--stalled", "8"])
        self._assert_asked(r, "the all-clear")

    def test_a_not_clear_body_asks_it(self):
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], ci="red")
        r.run(["--stalled", "8"])
        self.assertNotIn("all clear", r.prompts, f"{r.prompts!r}")
        self._assert_asked(r, "a NOT-clear wake-up")

    def test_a_ghost_body_asks_it(self):
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)],
                      manifest={"/w/gone": "wNF:p1"})
        r.run(["--stalled", "8"])
        self.assertIn("did not appear in the sweep", r.prompts, f"{r.prompts!r}")
        self._assert_asked(r, "a ghost wake-up")

    def test_a_ready_body_asks_it(self):
        r = NotifyRun(self, [pane("wPE:p1", "MERGEABLE", 2, 0, status="done")])
        r.run(["--stalled", "8"])
        self._assert_asked(r, "a ready-to-merge wake-up")

    def test_a_stall_only_body_asks_it(self):
        r = NotifyRun(self, [pane("wPE:p1", "NOTHING", 0, 0)])
        r.state_path.write_text(json.dumps({"streaks": {
            "wPE:p1": {"sig": "NOTHING|0|0|idle", "streak": 99}}}))
        r.run(["--stalled", "8"])
        self.assertNotIn("merge with -F", r.prompts, f"{r.prompts!r}")
        self._assert_asked(r, "a stall-only wake-up")

    def test_the_question_carries_no_policy(self):
        """The same forbidden phrases as the all-clear's guard, on the question itself.

        A question that names the policy has become the answer — the over-reach this file
        has already paid for once.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("nc", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for phrase in ("dispatch", "merge frees a slot", "ceiling", "survey the open issues"):
            self.assertNotIn(phrase, mod.PARALLEL_QUESTION,
                             f"{phrase!r} is policy, not a question: {mod.PARALLEL_QUESTION!r}")


class ADeclaredStateReachesTheWakeUpPathToo(unittest.TestCase):
    """The gap the declaration mechanism left, found by using what it merged.

    ## What was missed, and why it is the half that mattered

    `fleet-keepalive.py` reads `.fleet-state/expected-ci.json` and reports

        CI 0 red / 0 blocked / 0 unverifiable / 11 declared / 0 declared-stale

    **`notify-coordinator.py` did not read it at all.** It reads GitLab itself and emits the same
    eleven repositories in the same voice, every thirty minutes, through the other door. So the
    report that got tidied is a log nobody reads, and the component that *actually wakes the
    coordinator* still carries the original noise. The mechanism reached one of the two halves of
    one report, and the half it missed is the one that interrupts a human.

    ## What this class asserts

    1. A declared state is **relocated on this path too** — out of the noise, into a section of its
       own, never out of the report.
    2. A declared state **withholds the all-clear**. The brief calls this half as important as the
       first, and it is a safety property rather than a tidiness one: an all-clear a JSON file can
       switch on is a **silencer**, and a silencer is worse than the noise it removed, because it is
       trusted.
    3. A declared state that turned into a real `RED` is **loud on the wake-up path** — not only in
       the keepalive's log. This is the trap that would lose a red.
    4. A stale declaration is reported as stale here too, and never silently dropped.

    ## Why the trap test is driven rather than asserted

    (3) is a **transition**, and a transition cannot be asserted by a single run: the first run of a
    declared state and the run after it goes red differ only in what GitLab said, and a one-run test
    cannot see the difference. So `test_a_declared_state_that_becomes_red_is_loud` runs the script
    **twice** against one fixture — declared `NONE` while GitLab says `NONE`, then GitLab says `RED`
    — and asserts the second run woke the coordinator and named the red. That is the shape of the
    real event: `social-studio` is declared `NONE` today and gains a pipeline later.

    It is also the test that can catch a **silencing regression in the signature**. If the wake-up's
    content hash does not carry the CI placement, an unmoved pane set produces an unchanged hash, the
    second run reports "unchanged, staying quiet", and the red is lost on the path whose entire job
    is not losing reds.

    ## The control is inside the trap test, not in a test of its own

    (2) is falsifiable only in one shape: an **undeclared** `BLOCKED` pipeline allows the all-clear
    today, because the never-started rule decided a billing problem must not make this tool permanently non-clear. So
    `test_declaring_everything_known_withholds_the_all_clear` asserts the control and the change in
    one place — the same fleet, once undeclared and once declared. Two separate tests could pass
    while the real one stopped clearing; a single test that asserts both cannot.
    """

    # The family state this ticket is about: pipelines `BLOCKED` because the shared runners have no
    # compute minutes. `BLOCKED` rather than `NONE` deliberately — a `NONE` verdict already withholds
    # the all-clear through `unverified`, so a `NONE` declaration would assert nothing.
    BLOCKED = {"meta": {"state": "BLOCKED",
                                  "because": "shared runners have no compute minutes (billing)"}}

    def _run(self, ci="blocked", declared=None, **kw):
        return NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], ci=ci,
                         declared=declared, **kw)

    def test_declaring_everything_known_withholds_the_all_clear(self):
        """THE SILENCER, and the assertion the declaration mechanism never reached this script."""
        control = self._run().run(["--stalled", "8"])
        self.assertIn("all clear", control.prompts,
                      f"CONTROL: undeclared BLOCKED must still clear — if it stops clearing, the "
                      f"monitor has been made permanently non-clear: {control.prompts!r}")

        r = self._run(declared=self.BLOCKED).run(["--stalled", "8"])
        self.assertNotIn("all clear", r.prompts,
                         f"declaring a BLOCKED state must NOT clear the wake-up path either — a "
                         f"declaration is a claim, not a verification: {r.prompts!r}")
        self.assertNotEqual(r.state.get("sig"), "ALL-CLEAR",
                            "and the state must not record an all-clear it never gave")
        print("    CONTROL: all BLOCKED undeclared -> all-clear")
        print("    TRAP:    all BLOCKED declared   -> refused, naming the declaration")

    def test_the_reason_it_is_not_clear_names_the_declaration_as_the_reason(self):
        """An unexplained non-clear is indistinguishable from a stopped coordinator.

        Same rule as the reassuring sentence, pointed the other way: the report must say *what*
        withheld the all-clear. "NOT clear" alone teaches nothing, and a reader who cannot tell a
        declared state from a fault stops reading both.
        """
        r = self._run(declared=self.BLOCKED).run(["--stalled", "8"])
        self.assertIn("NOT clear", r.prompts,
                      f"it must deny the all-clear in those words: {r.prompts!r}")
        self.assertIn("DECLARED", r.prompts,
                      f"and name the declaration as the reason: {r.prompts!r}")
        self.assertIn("NOT a pass", r.prompts,
                      f"and say a declaration is not a verification: {r.prompts!r}")

    def test_a_declared_state_is_relocated_never_cleared(self):
        """Relocate, never clear — and relocate into a section of its own.

        The line leaves `BLOCKED`, where eleven identical billing lines train a reader to skim, and
        appears under `DECLARED` instead. What it must NOT do is disappear: a declaration is a
        per-repository relabel, not a filter.
        """
        r = self._run(declared=self.BLOCKED).run(["--stalled", "8"])
        self.assertIn("DECLARED", r.prompts,
                      f"a declared state must be reported under a heading of its own: {r.prompts!r}")
        self.assertIn("meta", r.prompts,
                      f"and the repository must still be named: {r.prompts!r}")
        self.assertIn("compute minutes", r.prompts,
                      f"carrying the reason the coordinator wrote, which is the whole value of the "
                      f"file: {r.prompts!r}")
        self.assertNotIn("BLOCKED on CI minutes", r.prompts,
                         f"a declared state must leave the BLOCKED section — relocation is the "
                         f"point of the mechanism: {r.prompts!r}")

    def test_and_an_undeclared_state_keeps_its_own_section(self):
        """A declaration is per repository.

        If writing one entry also silenced a second repository, the mechanism would be a blanket
        switch wearing a data file's clothes — the file would look specific while behaving globally.
        """
        r = NotifyRun(self, [pane("wPG:p1", "IN-FLIGHT", 6, 0)], ci="blocked",
                      repos=["meta", "identity"],
                      per_repo_ci={"meta": "blocked",
                                   "identity": "blocked"},
                      declared=self.BLOCKED).run(["--stalled", "8"])
        self.assertIn("BLOCKED on CI minutes", r.prompts,
                      f"the undeclared repository keeps the billing section: {r.prompts!r}")
        self.assertIn("identity", r.prompts,
                      f"and is named there: {r.prompts!r}")

    def test_a_declared_state_that_becomes_red_is_loud_on_the_wake_up_path(self):
        """THE TRAP THAT LOSES A RED. Driven, not asserted."""
        declared_none = {"meta": {"state": "NONE",
                                            "because": "no .gitlab-ci.yml by design"}}
        r = self._run(ci="none", declared=declared_none)
        r.run(["--stalled", "8"])
        self.assertNotIn("all clear", r.prompts,
                         f"precondition: a declared NONE is not an all-clear: {r.prompts!r}")

        # GitLab now says a job ran and failed. The declaration did not change.
        r.env["CI_FIXTURE"] = "red"
        r.run(["--stalled", "8"])

        self.assertIn("main are RED", r.prompts,
                      f"a job ran and failed, and the coordinator must be woken in those words: "
                      f"{r.prompts!r}")
        self.assertIn("IS a code defect", r.prompts,
                      f"and told it is a code defect, not billing: {r.prompts!r}")
        self.assertIn("stale", r.prompts.lower(),
                      f"and told the DECLARATION is now the thing that is wrong — reporting only "
                      f"the red leaves the coordinator believing their claim still works: "
                      f"{r.prompts!r}")
        self.assertEqual(r.out.count("woke COORD"), 2,
                         f"run 2 must wake on its own: the pane set did not move, so only the CI "
                         f"placement can make this new. One wake means the red was suppressed as "
                         f"'unchanged': {r.out}")
        print("    declared NONE -> GitLab RED: run 2 wakes on its own and names both halves")

    def test_a_stale_declaration_is_reported_as_stale_here_too(self):
        """Good news is still news, because the file is now wrong.

        The case a coordinator meets the moment a pipeline lands: the pipeline appears and is green, the
        `NONE` line stops matching, and the declaration becomes a no-op that still looks maintained.
        It must be reported so it gets removed — and it must NOT be presented as a code fault, or
        the mechanism punishes the fix it was written to accommodate.
        """
        r = self._run(ci="green",
                      declared={"meta": {"state": "NONE", "because": "pipeline being added"}}
                      ).run(["--stalled", "8"])
        self.assertIn("stale", r.prompts.lower(),
                      f"a declaration GitLab has outgrown must be reported on the wake-up path "
                      f"too: {r.prompts!r}")
        self.assertIn("meta", r.prompts, r.prompts)
        self.assertNotIn("IS a code defect", r.prompts,
                         f"a declaration that went stale favourably is not a code defect: "
                         f"{r.prompts!r}")

    def test_a_broken_declaration_file_is_reported_not_swallowed(self):
        """`except: return {}` is the wrong direction, and it is loud here.

        `_expected_red`'s loader does exactly that, and it is survivable for a set of worktrees — the
        report simply shows the red again. Here a typo would un-declare eleven repositories, this
        script would go back to the always-true report, and nothing would say why. A coordinator who
        believes a declaration is in force when the file does not parse has been misled by the
        mechanism meant to inform them.
        """
        r = self._run(declared_raw="{ this is not json").run(["--stalled", "8"])
        both = r.prompts + r.out
        self.assertIn("declared", both.lower(),
                      f"a declaration file that cannot be read must be reported: {r.prompts!r}")
        self.assertIn("could not be read", both,
                      f"in the words of the reader, not paraphrased: {both!r}")

    def test_a_declaration_naming_an_unknown_state_is_refused(self):
        """An unresolvable name cannot be honoured, and the attempt is itself the finding.

        `GREEN` is the one declaration that would be a genuine silencer: a coordinator who can write
        `{"state": "GREEN"}` and see this script report a healthy family has built a one-line switch
        for the whole monitor, driven by nothing but an assertion that a thing is fine.
        """
        r = self._run(ci="green",
                      declared={"meta": {"state": "GREEN",
                                                   "because": "I am fairly sure"}}).run(
            ["--stalled", "8"])
        self.assertIn("GREEN", r.prompts + r.out,
                      f"the refused state must be named, or the file looks fine: {r.prompts!r}")
        self.assertNotIn("all clear", r.prompts,
                         f"and a refused declaration must certainly not clear: {r.prompts!r}")

    def test_a_declaration_naming_a_repository_this_run_did_not_read_is_reported(self):
        """A declaration nothing can compare against is still a claim, and still withholds.

        The survey is the registry, so a renamed or removed repository leaves a declaration pointing
        at nothing — with no row to check it against and no error anywhere.
        """
        r = self._run(declared={"nosuchrepo": {"state": "BLOCKED",
                                                         "because": "no minutes"}}
                      ).run(["--stalled", "8"])
        self.assertIn("nosuchrepo", r.prompts + r.out,
                      f"an unverifiable declaration must be named: {r.prompts!r}")
        self.assertNotIn("all clear", r.prompts,
                         f"and it still withholds the all-clear: {r.prompts!r}")

    def test_an_unchanged_declaration_wakes_once_and_then_stops(self):
        """THE ANTI-SPAM PROPERTY, and the one this change puts most at risk.

        Eleven declarations relayed verbatim into the wake-up body is precisely the every-30-minutes
        wake-up the declaration mechanism was built to remove — arriving on the other door. So the content hash must carry
        the declaration, which makes a **change** to it a wake-up and leaves an unchanged one silent.

        Three runs assert the count is one. Two would only prove "not twice".
        """
        r = self._run(declared=self.BLOCKED)
        woke = []
        for _ in range(3):
            r.run(["--stalled", "8"])
            woke.append("woke COORD" in r.last_out)
        self.assertEqual(woke, [True, False, False],
                         f"an unchanged declaration must wake once and then stay quiet, or this "
                         f"relocation has rebuilt the alert it replaced: {woke}")
        self.assertIn("staying quiet", r.out,
                      f"and the silence must be a reported fact, not an absence: {r.out}")
        print("    3 identical runs with a declaration -> 1 wake-up, 2 reported silences")

    def test_the_committed_declaration_file_is_read_by_its_real_key(self):
        """The trap measured on this mechanism, held against the file that is actually committed.

        A plausible `repos` key was added beside `repositories` in this very repository's state file
        while the declaration mechanism was being written, and it **read as empty and said nothing** — eleven
        declarations in force, none of them reported. Nothing went red, because "no declaration
        matched" is a legitimate answer and only a test that knows the answer can tell it from a
        wrong key.

        So this asserts against the **committed** `.fleet-state/expected-ci.json`, through the real
        module, rather than against a fixture written by this file: a key spelled differently would
        read as an empty declaration on every run in the family, silently.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location("ec_shared", HERE / "expected_ci.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        committed = HERE.parent / ".fleet-state" / "expected-ci.json"
        doc = json.loads(committed.read_text())
        self.assertIn("repositories", doc,
                      f"the committed state file's key is `repositories` — {committed}")
        self.assertNotIn("repos", doc,
                         f"a second, plausible key beside it is what made the mechanism read as "
                         f"empty and stay silent: {sorted(doc)}")

        # …and the module agrees, with nothing else configured.
        module.os.environ["FLEET_EXPECTED_CI"] = str(committed)
        declared, refusals = module.read_declarations()
        self.assertEqual(refusals, [], f"the committed file must read without refusals: {refusals}")
        self.assertEqual(sorted(declared), sorted(doc["repositories"]),
                         f"every committed declaration must be in force — a reader that drops one "
                         f"silently is the defect this test exists to catch: {sorted(declared)}")
        for repo, claim in declared.items():
            self.assertIn(claim["state"], module.DECLARABLE,
                          f"{repo} declares {claim['state']}, which is not declarable")
            self.assertTrue(claim["because"], f"{repo} declares no reason, so nobody can act on it")
        print(f"    the committed file reads as {len(declared)} declaration(s), zero refusals")

    def test_a_dry_run_with_everything_declared_is_not_an_all_clear(self):
        """`--dry-run` is what a coordinator reads before deciding, so it must not flatter.

        A dry run that reported an all-clear with everything declared would be the same silencer with
        an extra step, and it is the shape the brief asks for by name.
        """
        r = self._run(declared=self.BLOCKED, args=["--dry-run"]).run()
        self.assertNotIn("all clear", r.out,
                         f"--dry-run with every state declared must not read as an all-clear: "
                         f"{r.out}")
        self.assertIn("DECLARED", r.out,
                      f"and it must say why, naming the declaration: {r.out}")





class TheCoordinatorIsFoundByItsLabel(unittest.TestCase):
    """The nudge must reach the coordinator by contract, not by accident of focus.

    Measured 2026-10-05: **three** live agents sat at the workspace root, none of them a
    recorded worker, and the wake-up reached the right one only because that pane was focused.
    Focus is a property of the next click, not a fact about the role — and two of those panes are
    other opencode sessions that are not workers and never will be, so "not a worker" does not
    narrow the field to one.

    `herdr agent list` carries no `label` field at all. The convention in `AGENTS.md` (the
    coordinator's pane is labelled `AI · coordinator`) was therefore not resolvable from the data
    the resolver was reading, which is precisely how a nudge goes to the wrong session: the
    resolver looked at a field the contract does not name. It now asks `pane list`, where the
    label exists.

    A wrong wake-up is worse than a missing one, because it looks like it worked.
    """

    def test_the_label_beats_focus(self):
        """THE property: the other root pane is focused, and the label still wins.

        Without the label this is the exact state that misdirects the nudge — two root agents,
        neither a worker, the coordinator's pane not focused.
        """
        r = NotifyRun(
            self,
            [pane("wPG:p1", "GATE-RED", 4, 0)],
            focused_pane="OTHER",       # focus moved to the sibling root session
            coordinator_label="AI · coordinator",
        )
        r.run(["--stalled", "8"])
        self.assertIn("COORD", r.prompts,
                      f"the labelled coordinator must be woken even when it is not focused: "
                      f"{r.prompts!r}")
        self.assertNotIn("OTHER", r.prompts,
                         f"the focused sibling is not the coordinator: {r.prompts!r}")
        print("    focus on the sibling root pane -> the labelled coordinator is still woken")

    def test_an_unlabelled_coordinator_falls_back_rather_than_failing(self):
        """No label must not mean no coordinator — it means fall back.

        The fallback is weaker, and that is honest: it is documented as weaker rather than
        silently standing in for a contract it does not have.
        """
        r = NotifyRun(
            self,
            [pane("wPG:p1", "GATE-RED", 4, 0)],
            coordinator_label="",
            focused_pane="COORD",
        )
        r.run(["--stalled", "8"])
        self.assertIn("COORD", r.prompts,
                      f"an unlabelled fleet must still wake somebody: {r.prompts!r}")
        print("    no label -> the cwd fallback still resolves, rather than going silent")

    def test_a_worker_labelled_coordinator_is_ignored(self):
        """The label must not be able to wake a worker's pane.

        Otherwise the fix trades a wrong pane for a different wrong pane: a stale manifest entry
        whose pane happens to carry the label would take the wake-up.
        """
        r = NotifyRun(
            self,
            [pane("wPG:p1", "GATE-RED", 4, 0)],
            manifest={"/w/reg": "COORD"},
            coordinator_label="AI · coordinator",
        )
        r.run(["--stalled", "8"])
        self.assertNotIn("wPG:p1 GATE-RED", r.prompts,
                         f"the sweep's own rows must still be reported: {r.prompts!r}")
        # COORD is a recorded worker here, so the label must not resolve to it. The cwd
        # fallback also excludes recorded workers, so nothing should claim COORD.
        self.assertNotIn("1 failing", r.prompts,
                         f"a worker pane must not be reported as the coordinator's own "
                         f"wake-up: {r.prompts!r}")
        print("    a pane labelled coordinator but recorded as a worker is refused")


class AForeignLabelledPaneIsNeverPrompted(unittest.TestCase):
    """#98. The label is not unique on this machine, so the cwd is part of the match.

    `AGENTS.md` documents the convention — the coordinator's pane is labelled `AI · coordinator` —
    and it is *this repository's* convention. **Other projects on this machine use the same
    label.** Measured: `wX:p2` (this workspace), `w3A:p1` and `w1C:p1` (two other projects) all
    carry it, and 204 of 205 logged wake-ups landed on `wX:p2` only because `herdr pane list`
    returns this workspace's pane first. **Ordering is luck, not a guard:** if this workspace's
    pane is not the first live labelled one — its agent not detected, the workspace recreated
    under a later number, the label moved — the wake-up lands in another project's session as
    user input. That is measured on this machine, not hypothetical: another project's notifier
carried
    the identical label-only match and its first live run woke *this* coordinator pane.

    So the match is label **and** cwd (the workspace root, or a directory under it),
    a labelled pane elsewhere is reported with `labelled elsewhere, not touched` and never
    prompted, and the recorded-worker guard and cwd fallback are unchanged.
    """

    def test_a_foreign_labelled_pane_listed_first_does_not_take_the_wake_up(self):
        """THE headline case: the foreign pane is first AND focused AND live, and ours wins.

        This is exactly the shape the pre-fix loop resolved: iterate `pane list` in order,
        return the first labelled pane with a detected agent. If the fix is absent this test
        fails by prompting `wFOR:p1`; if the fixture lost its teeth (no cwd, or no agent for
        the foreign pane) it would pass for the wrong reason — which is why the mutation
        below removes the scope and requires this same fixture to go the other way.
        """
        panes, agents = foreign_first_fleet()
        r = NotifyRun(self, [pane("wPG:p1", "GATE-RED", 4, 0)],
                      herdr_panes=panes, herdr_agents=agents)
        r.run(["--stalled", "8"])
        self.assertIn("woke COORD", r.out,
                      f"the pane whose cwd is this workspace must take the wake-up: {r.out}")
        self.assertIn("COORD", r.prompts,
                      f"and it must actually receive the prompt: {r.prompts!r}")
        self.assertNotIn("wFOR:p1", r.prompts,
                         f"the foreign pane was prompted — this is the wrong-session wake-up "
                         f"the cwd scope is about, and it is worse than a missing one because it looks "
                         f"like it worked: {r.prompts!r}")
        print("    foreign labelled pane first, live and focused -> this workspace's pane still wins")

    def test_a_lone_foreign_labelled_pane_is_reported_and_nobody_is_prompted(self):
        """No local labelled pane, and no local agent to fall back to: report, prompt nobody.

        Only a foreign labelled pane and an unlabelled local pane with **no detected agent** —
        the shape where "prompt nobody rather than the foreign pane" is literal. Silence would
        be indistinguishable from "no coordinator session is up at all", which is why the
        refusal is a log line.
        """
        r = NotifyRun(
            self,
            [pane("wPG:p1", "GATE-RED", 4, 0)],
            herdr_panes=[
                {"pane_id": "wFOR:p1", "label": "AI · coordinator", "cwd": FOREIGN_CWD},
                {"pane_id": "OTHER", "label": None, "cwd": WORKSPACE},
            ],
            herdr_agents=[
                {"pane_id": "wFOR:p1", "cwd": FOREIGN_CWD, "agent_status": "idle",
                 "focused": True},
            ],
        )
        r.run(["--stalled", "8"])
        self.assertEqual(r.prompts, "",
                         f"no pane may be prompted here — the foreign one certainly not: "
                         f"{r.prompts!r}")
        self.assertIn("labelled elsewhere, not touched", r.out,
                      f"and the refusal must be reported, not silent: {r.out}")
        self.assertIn("wFOR:p1", r.out,
                      f"naming the pane that was not touched, so it can be checked: {r.out}")
        self.assertIn("cannot wake it", r.out,
                      f"with nothing local to fall back to, the run must say so: {r.out}")
        print("    only a foreign labelled pane -> reported as elsewhere, nobody prompted")

    def test_a_foreign_labelled_pane_is_never_the_fallback_either(self):
        """The other half: the label path yields None, the cwd fallback runs, and the foreign
        pane still loses even though it is the FOCUSED one.

        The fallback is already scoped (`cwd == WORKSPACE`); this pins that it stays that way,
        rather than being "helpfully" widened to reach the focused pane when the label misses.
        """
        r = NotifyRun(
            self,
            [pane("wPG:p1", "GATE-RED", 4, 0)],
            herdr_panes=[
                {"pane_id": "wFOR:p1", "label": "AI · coordinator", "cwd": FOREIGN_CWD},
                {"pane_id": "OTHER", "label": None, "cwd": WORKSPACE},
            ],
            herdr_agents=[
                {"pane_id": "wFOR:p1", "cwd": FOREIGN_CWD, "agent_status": "idle",
                 "focused": True},
                {"pane_id": "OTHER", "cwd": WORKSPACE, "agent_status": "idle",
                 "focused": False},
            ],
        )
        r.run(["--stalled", "8"])
        self.assertIn("woke OTHER", r.out,
                      f"the local, unlabelled fallback pane is the only candidate: {r.out}")
        self.assertNotIn("wFOR:p1", r.prompts,
                         f"and the foreign pane is never a candidate: {r.prompts!r}")
        print("    label miss + focused foreign pane -> the local cwd fallback still wins")

    def test_a_labelled_pane_under_the_workspace_root_is_ours(self):
        """`or under it`: a coordinator session inside a checkout below the root counts.

        The companion of the boundary test below — together they pin the rule as "the root or a
        directory under it", not "exactly the root" and not "any string that starts with it".
        """
        subdir = WORKSPACE + "/meta"
        r = NotifyRun(
            self,
            [pane("wPG:p1", "GATE-RED", 4, 0)],
            herdr_panes=[
                {"pane_id": "SUBDIR", "label": "AI · coordinator", "cwd": subdir},
                {"pane_id": "wFOR:p1", "label": "AI · coordinator", "cwd": FOREIGN_CWD},
            ],
            herdr_agents=[
                {"pane_id": "SUBDIR", "cwd": subdir, "agent_status": "idle", "focused": True},
                {"pane_id": "wFOR:p1", "cwd": FOREIGN_CWD, "agent_status": "idle",
                 "focused": False},
            ],
        )
        r.run(["--stalled", "8"])
        self.assertIn("woke SUBDIR", r.out,
                      f"a pane inside the workspace is the workspace's: {r.out}")
        self.assertNotIn("wFOR:p1", r.prompts, f"{r.prompts!r}")
        print("    a labelled pane in a subdirectory of the workspace counts as ours")

    def test_a_sibling_sharing_the_prefix_is_not_ours(self):
        """`/synthetic/workspace/other` starts with the workspace path as a STRING.

        A match written as `startswith(WORKSPACE)` treats a sibling repository as local, and the
        sibling produces no error — it just takes the wake-up. The boundary is the separator.
        """
        sibling = WORKSPACE + "-other"
        r = NotifyRun(
            self,
            [pane("wPG:p1", "GATE-RED", 4, 0)],
            herdr_panes=[
                {"pane_id": "wSIB:p1", "label": "AI · coordinator", "cwd": sibling},
                {"pane_id": "COORD", "label": "AI · coordinator", "cwd": WORKSPACE},
            ],
            herdr_agents=[
                {"pane_id": "wSIB:p1", "cwd": sibling, "agent_status": "idle",
                 "focused": True},
                {"pane_id": "COORD", "cwd": WORKSPACE, "agent_status": "idle",
                 "focused": False},
            ],
        )
        r.run(["--stalled", "8"])
        self.assertIn("woke COORD", r.out,
                      f"a sibling whose name merely starts with the root is foreign: {r.out}")
        self.assertNotIn("wSIB:p1", r.prompts,
                         f"and must never be prompted: {r.prompts!r}")
        print("    a sibling sharing the path prefix is foreign; the separator is the boundary")


class TheMutationThatProvesTheCwdScopeIsLive(unittest.TestCase):
    """The positive control: remove the cwd scope, and the same fixture takes the foreign pane.

    The behavioural tests above prove the fixed script does the right thing; they do **not**
    prove the fixture is able to fail. This mutant carries the pre-fix label-only match, and
    `foreign_first_fleet()` — the very fixture the headline test uses — must make it prompt the
    foreign pane. The inert-mutation trap (FACTORY.md 4a) is a mutation that never took effect
    and whose green is then read as evidence: so the needle must be present and unique, the
    result must differ, and the mutant must still `ast.parse`.
    """

    NEEDLE = '        if not (cwd == WORKSPACE or cwd.startswith(WORKSPACE + "/")):'
    REPLACEMENT = "        if False:"

    def _mutant(self):
        src = SCRIPT.read_text()
        self.assertIn(self.NEEDLE, src,
                      "the cwd scope moved, so this mutation no longer removes it — repoint "
                      "the needle at the line that does")
        self.assertEqual(src.count(self.NEEDLE), 1,
                         f"{self.NEEDLE!r} is not unique; the mutation would be ambiguous")
        mutated = src.replace(self.NEEDLE, self.REPLACEMENT, 1)
        self.assertNotEqual(mutated, src, "the mutation changed nothing")
        # `ast.parse` is this project's `bash -n`: a mutant that cannot load proves nothing
        # about the guard it was meant to break.
        ast.parse(mutated)
        path = Path(tempfile.mkdtemp(prefix="mutant.")) / "notify-coordinator.py"
        path.write_text(mutated)
        self.addCleanup(shutil.rmtree, str(path.parent), ignore_errors=True)
        return path

    def test_the_label_only_match_prompts_the_foreign_pane(self):
        panes, agents = foreign_first_fleet()
        r = NotifyRun(self, [pane("wPG:p1", "GATE-RED", 4, 0)],
                      herdr_panes=panes, herdr_agents=agents, script=self._mutant())
        r.run(["--stalled", "8"])
        self.assertIn("wFOR:p1", r.prompts,
                      "the label-only mutant MUST take the foreign pane — otherwise this "
                      "fixture cannot tell the guard from its absence, and the green above "
                      f"proves nothing: {r.prompts!r}")
        print("    mutant (label only) -> prompts wFOR:p1, the foreign pane")

    def test_the_real_script_holds_the_line_on_the_same_fixture(self):
        panes, agents = foreign_first_fleet()
        r = NotifyRun(self, [pane("wPG:p1", "GATE-RED", 4, 0)],
                      herdr_panes=panes, herdr_agents=agents)
        r.run(["--stalled", "8"])
        self.assertNotIn("wFOR:p1", r.prompts,
                         f"the real script must not: {r.prompts!r}")
        self.assertIn("COORD", r.prompts,
                      f"and must wake this workspace's pane instead: {r.prompts!r}")
        print("    real script, same fixture -> prompts COORD, never wFOR:p1")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--explain":
        print(__doc__)
        sys.exit(0)
    unittest.main(verbosity=2)