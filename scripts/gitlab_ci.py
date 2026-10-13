#!/usr/bin/env python3
"""gitlab-ci — what GitLab says about `main`, and whether that is a code defect.

WHY THIS EXISTS
---------------
The keepalive runs **local** gates, and reported `GREEN` for repositories whose GitLab
pipeline was red. The failure this family spent a day on was only ever visible in GitLab:
`make check` was green on the workstation, because the workstation has the sibling checkout the
pipeline could not obtain. So a tool that answers "is the local gate green?" and reports the
answer as "is the code good?" is not being conservative — it is answering a different question.

The third instance of one shape, after the merge gate's omission of `showboat` and the
reading of `t.Logf` as a failure: **the monitor checks something adjacent to the thing and reports the
result as the thing.**

THE TRAP, AND WHY THIS FILE IS MOSTLY ABOUT IT
----------------------------------------------
**Every pipeline in this family is currently red, and none of it is a code defect.** The shared
runners have no compute minutes, so every job is rejected before it starts. Measured across the
whole group on 2026-10-05 — seven repositories with a pipeline on `main`, every job identical:

    status=failed  failure_reason=ci_quota_exceeded  started_at=null  duration=null  runner=null

Against that, the same repositories three days earlier, failing for real:

    status=failed  failure_reason=script_failure     started_at=...    duration=19.99  runner=<id>

So a keepalive that reads pipeline state naively reports **every repository red, forever** — and a
monitor that is always red is a monitor nobody reads, which is this project's own recurring failure
in the opposite direction from an always-green one.

`VERDICTS` therefore has five values and **two of them are not a pass**:

| verdict | meaning | a code defect? |
|---|---|---|
| `GREEN` | the pipeline on `main` succeeded | no |
| `RED` | it failed, and a job **ran** | yes |
| `BLOCKED` | it failed, and **no job ever started** — CI minutes exhausted | **no** |
| `RUNNING` | a pipeline is in flight | unknown yet |
| `NONE` | no pipeline on `main` at all | nothing to say |
| `UNKNOWN` | GitLab could not be read, or the fields do not separate the two reds | **not a pass** |

`BLOCKED` is the whole point. It is a distinct state rather than a `RED` with a softer word
because collapsing it into `RED` is the exact defect this file exists to prevent, and collapsing it
into `GREEN` would be a lie in the other direction — nothing was verified.

THE DISCRIMINATOR, AND WHY IT IS NOT A HEURISTIC
------------------------------------------------
`failure_reason` is GitLab's own field and it separates the two cases exactly. Measured, not
assumed, and both halves are required so that a single surprising field cannot decide:

- **`BLOCKED`** — the job failed with `failure_reason == "ci_quota_exceeded"`, **or** every failed
  job has `started_at is None` **and** `duration is None`. Both halves are the same fact seen
  twice: the job was never picked up by a runner.
- **`RED`** — at least one failed job has a `failure_reason` that is *not* a never-started
  signature, or a non-null `duration`. A job that ran for 20 seconds and exited 1 ran.

WHICH IS ALSO WHY THE VERDICT IS NEVER GUESSED
-----------------------------------------------
If the jobs cannot be read, or the fields do not agree, the answer is `UNKNOWN` and **not** a
`RED`. `UNKNOWN` is not a pass: `GREEN` is the only pass. A tool that must never report a
confident wrong answer has exactly one honest move when it cannot see, and that is to say so —
`NO-GATE`/`NONE` already exists in this codebase as a name rather than a lie, and this is the
same convention applied to a network call.

    gitlab-ci.py --once        # human table, exit 0
    gitlab-ci.py --json        # one JSON object on stdout, for programs
    gitlab-ci.py --repo NAME   # a single repository

`--json` prints **one** object on stdout and nothing else, because a wide table invites reading a
number out of the wrong column — which has already produced one confident claim about a bug that
did not exist in this project.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factory_config  # noqa: E402

# The only pass. Named first so every other name in this file reads as "not this".
GREEN = "GREEN"
# A code defect: a job ran and failed.
RED = "RED"
# Not a code defect and not a pass: the runners had no compute minutes and no job ever started.
# This state exists so the family is not permanently red, and so nobody is sent to fix code that
# was never executed.
BLOCKED = "BLOCKED"
# A pipeline is in flight, so there is no verdict yet.
RUNNING = "RUNNING"
# No pipeline on this ref. Not a failure, and not a verification.
NONE = "NONE"
# GitLab could not be read, or the fields do not separate the two reds. NOT a pass.
UNKNOWN = "UNKNOWN"

VERDICTS = (GREEN, RED, BLOCKED, RUNNING, NONE, UNKNOWN)

# GitLab's own field for "the runner could not take this job because the namespace is out of
# compute minutes". Measured, not remembered: every job in every pipeline in this group carries
# it, alongside `started_at: null` and `duration: null`.
QUOTA_REASON = "ci_quota_exceeded"

# `failure_reason` values that mean the job was never picked up by a runner, so the code inside
# it never ran. Deliberately a set and not a single comparison: a future runner type could report
# a different never-started reason, and a check that only knows one spelling turns that into a
# confident `RED`.
NEVER_STARTED_REASONS = frozenset({QUOTA_REASON, "runner_system_failure", "runner_unsupported"})

# A pipeline status that is still in flight.
IN_FLIGHT = frozenset({"running", "pending", "created", "preparing", "waiting_for_resource",
                       "scheduled"})

# The branch the CI reader reads. The tracker namespace comes from `factory_config`
# (`FLEET_GITLAB_GROUP` still overrides — the GitLab flavor shipped first and cron sets it).
DEFAULT_BRANCH = "main"


def group() -> str:
    return factory_config.tracker_owner()


def branch() -> str:
    return os.environ.get("FLEET_GITLAB_BRANCH", DEFAULT_BRANCH)


def api_timeout() -> int:
    try:
        return int(os.environ.get("FLEET_GITLAB_TIMEOUT", "60"))
    except ValueError:
        return 60


def _glab() -> str | None:
    """The `glab` binary, or None — which is `UNKNOWN`, not an error to raise.

    Absent tooling is not a red pipeline and it is not a green one. It is the absence of an
    answer, and the caller has to be able to say so.
    """
    found = shutil.which("glab")
    if found:
        return found
    # Cron's PATH is `/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin` and `glab` is
    # installed by `go install` into `~/go/bin`, which is not on it. So under cron every
    # repository reads `UNKNOWN — glab is not on PATH`, forever, and that is a fact about the
    # process's environment rather than about GitLab — the same confusion as the missing token,
    # one layer further out.
    #
    # Look where it is actually installed before concluding it is absent. `UNKNOWN` is right when
    # the tool genuinely is not there, and it is a lie when we simply did not look in the right
    # place: "cannot tell" and "did not look properly" are different facts, and only one of them
    # is the tool's fault.
    for candidate in (Path.home() / "go/bin/glab", Path.home() / ".local/bin/glab",
                      Path("/usr/local/bin/glab"), Path("/usr/bin/glab")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _glab_env() -> dict:
    """The environment `glab` needs, loading the workspace profile when it is missing.

    Cron runs this script with a bare environment: `crontab` sets `PATH` and nothing else, so
    `GLAB_CONFIG_DIR` and `GITLAB_TOKEN` are absent and every `glab api` answers **403**. The
    symptom is indistinguishable from a token-scope problem — `cannot tell` for all eleven
    repositories, indefinitely — while the same script in a shell that had sourced `glab-env.sh`
    reads every one of them and reports 8 `BLOCKED` and 1 `GREEN`.

    That ran for hours before anyone read it as an environment problem rather than a verdict, and
    the reason it was believable is the point: **`UNKNOWN` is doing its job**, so the absence of
    data looked like a fact about GitLab instead of a fact about how the process was started.

    So a tool that needs a credential obtains it. Making the caller remember an env script is a
    convention, and a convention that only a shell satisfies is not one.
    """
    if os.environ.get("GITLAB_TOKEN"):
        return dict(os.environ)
    env_file = Path(__file__).resolve().parent / "glab-env.sh"
    if not env_file.is_file():
        return dict(os.environ)
    # `env -0` separates with NUL, so this is bytes and NOT text=True — `text=True` hands back a
    # str and `.split("\0")` raises TypeError. A credential loader that crashes is worse than one
    # that does not load, because it takes the whole survey down instead of reporting UNKNOWN.
    run = subprocess.run(["bash", "-c", f"source '{env_file}' >/dev/null 2>&1 && exec env -0"],
                         capture_output=True, env=dict(os.environ), check=False)
    if run.returncode != 0 or not run.stdout:
        return dict(os.environ)
    loaded = {}
    for entry in run.stdout.decode("utf-8", "replace").split("\0"):
        if "=" in entry:
            key, _, value = entry.partition("=")
            loaded[key] = value
    return {**os.environ, **loaded}


def api(path: str) -> tuple[object | None, str]:
    """One `glab api` call. Returns `(parsed, "")` or `(None, reason)`.

    `glab api` is the right surface and not `glab pipeline list`: the latter has no per-job view,
    so it cannot see a job that never started — which is the entire discriminator. Real `glab`
    exits non-zero on an HTTP error and writes the message to **stderr**, and its output can be a
    bare error document rather than the payload, so the exit status is read and not inferred from
    an absence of output.
    """
    binary = _glab()
    if binary is None:
        return None, "`glab` is not on PATH, so GitLab was not read at all"
    try:
        run = subprocess.run([binary, "api", path], capture_output=True, text=True,
                             timeout=api_timeout(), env=_glab_env())
    except subprocess.TimeoutExpired:
        return None, f"`glab api` did not finish within {api_timeout()}s"
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip().splitlines()
        return None, detail[0][:200] if detail else f"`glab api` exited {run.returncode}"
    try:
        return json.loads(run.stdout), ""
    except json.JSONDecodeError as exc:
        return None, f"`glab api` returned something that is not JSON ({exc})"


def latest_pipeline(repo: str) -> tuple[dict | None, str]:
    """The most recent pipeline on the family's branch, or `(None, reason)`.

    `per_page=1` with the API's default ordering: it returns pipelines newest-first, which is what
    "the latest pipeline on main" means. `sha` is returned so a caller can say *which* commit was
    judged, because a verdict about "the pipeline" is worthless without it — a mergeable branch
    and a red `main` are different facts about different commits.
    """
    enc = quote(f"{group()}/{repo}", safe="")
    payload, why = api(f"projects/{enc}/pipelines?ref={branch()}&per_page=1")
    if payload is None:
        return None, why
    if not isinstance(payload, list):
        return None, f"expected a list of pipelines, got {type(payload).__name__}"
    if not payload:
        return None, f"no pipeline on {branch()} — this repository has never been built there"
    if not isinstance(payload[0], dict):
        return None, "a pipeline row was not an object"
    return payload[0], ""


def pipeline_jobs(repo: str, pipeline_id: int) -> tuple[list[dict] | None, str]:
    enc = quote(f"{group()}/{repo}", safe="")
    payload, why = api(f"projects/{enc}/pipelines/{pipeline_id}/jobs?per_page=100")
    if payload is None:
        return None, why
    if not isinstance(payload, list):
        return None, f"expected a list of jobs, got {type(payload).__name__}"
    return [j for j in payload if isinstance(j, dict)], ""


def classify(pipeline: dict, jobs: list[dict]) -> tuple[str, str]:
    """`(verdict, why)` for one pipeline and its jobs. The two reds, separated.

    PURE, and that is deliberate: this is the decision every other thing in the file exists to
    feed, so it takes data rather than doing I/O and can be tested against recorded payloads from
    the live group without a network. The tests assert it against **both** real shapes — a
    `script_failure` pipeline from 2026-10-03 and a `ci_quota_exceeded` one from 2026-10-05 —
    because a classifier tested only on the case that is currently breaking cannot tell a
    discriminator from a constant.
    """
    status = pipeline.get("status")
    if status in IN_FLIGHT:
        return RUNNING, f"pipeline #{pipeline.get('id')} is {status}"

    failed = [j for j in jobs if j.get("status") == "failed"]
    if not failed:
        if status == "success":
            return GREEN, f"pipeline #{pipeline.get('id')} succeeded"
        if status in ("canceled", "skipped", "manual"):
            # A pipeline nobody ran is not a pass and not a fault. `main` was not verified.
            return NONE, (f"pipeline #{pipeline.get('id')} is {status} — nothing was built, so "
                          f"{branch()} is NOT verified")
        if status == "failed":
            # Reached when the jobs list is readable but holds nothing marked `failed`. A failed
            # pipeline with no failed job is a contradiction, and the honest reading is that this
            # reader cannot see the job that failed — not that there was none. Saying "no job
            # failed" here is the exact move this file exists to refuse.
            return UNKNOWN, (f"pipeline #{pipeline.get('id')} is failed but none of its "
                             f"{len(jobs)} job(s) is marked failed, so the job that failed is not "
                             f"visible from here. This cannot be called a code defect or a pass — "
                             f"it is a gap in what GitLab returned")
        return UNKNOWN, (f"pipeline #{pipeline.get('id')} has status {status!r}, which this "
                         f"reader does not classify")

    # Both halves of the never-started signature, and they must AGREE rather than either one
    # sufficing. That is stricter than it first looks, and the stricter version is the correct
    # one: a `failure_reason` this reader does not recognise is not evidence that the job never
    # ran, and treating it as though it were turns an unfamiliar failure into a confident
    # `BLOCKED` — which is the one verdict a reader is least likely to look at again.
    #
    # So a job is "never started" only when it has **no recognised never-started reason** and
    # **no start and no duration**. A cancelled job with `job_execution_timeout` fails the
    # second half, because an explicit reason is itself evidence that the runner acted, and it
    # fails the first because the reason is not one of the never-started spellings. It is `RED`,
    # because "something started and then failed" is the fact, and the reason is attached for a
    # human to read.
    #
    # The consequence is deliberate and worth stating: an unrecognised never-started reason with
    # no timing at all reports `BLOCKED` with a reason that does NOT claim billing. It is
    # unverifiable-from-here rather than green, and a reader who adds the spelling to
    # `NEVER_STARTED_REASONS` gets the sharper wording. Guessing either way would be the defect.
    never_started, ran = [], []
    for job in failed:
        reason = job.get("failure_reason")
        known_never_started = reason in NEVER_STARTED_REASONS
        no_timing = job.get("started_at") is None and job.get("duration") is None
        if known_never_started or (reason is None and no_timing):
            never_started.append(job)
        else:
            ran.append(job)

    if never_started and not ran:
        reasons = sorted({str(j.get("failure_reason")) for j in never_started})
        # Only claim billing when EVERY never-started job carries the quota reason. One
        # unrecognised reason in the set is enough to make "the runners have no minutes" a claim
        # about a job this reader cannot identify, and that is the over-confident direction.
        all_known = all(j.get("failure_reason") in NEVER_STARTED_REASONS
                        for j in never_started)
        minutes = all_known and any(j.get("failure_reason") == QUOTA_REASON
                                    for j in never_started)
        if minutes:
            because = ("the shared runners have no compute minutes, so every job was rejected "
                       "before it ran. This is billing, not a code defect")
        else:
            because = ("no job started, and this reader does not recognise the reason, so whether "
                       "this is a runner problem or something else cannot be told from here")
        return BLOCKED, (f"{len(never_started)} job(s) failed without ever starting "
                         f"({', '.join(reasons)}) — {because}. Nothing in this repository ran, so "
                         f"it is not verified, and this is not evidence of a code defect")

    if ran and not never_started:
        names = ", ".join(str(j.get("name")) for j in ran[:6])
        return RED, (f"{len(ran)} job(s) ran and failed ({names}) — this IS a code defect")

    # Mixed. Named rather than resolved: half the pipeline proved the code is wrong and the other
    # half proved nothing at all, and picking either half would be the guess this file refuses to
    # make. It is still actionable — there is real work in there — so it is not `UNKNOWN`.
    names = ", ".join(str(j.get("name")) for j in ran[:6])
    return RED, (f"{len(ran)} job(s) ran and failed ({names}) AND {len(never_started)} never "
                 f"started. The code defect is real; the rest is unverified, so this pipeline "
                 f"does not report on every job in it")


def repo_verdict(repo: str) -> dict:
    """One repository's verdict, as a dict. Never raises, never guesses."""
    pipeline, why = latest_pipeline(repo)
    if pipeline is None:
        # A repository with no pipeline and a repository whose pipeline could not be read are
        # different facts, and the second one is about this tool rather than about the code.
        unreachable = "not on PATH" in why or "`glab" in why or "JSON" in why
        state = UNKNOWN if unreachable else NONE
        return {"repo": repo, "verdict": state, "why": why,
                "pipeline": None, "sha": None, "branch": branch()}

    jobs, jobs_why = pipeline_jobs(repo, pipeline.get("id"))
    if jobs is None:
        # The pipeline failed or succeeded, and we cannot see a single job — so we cannot tell
        # the two reds apart. `UNKNOWN`, never `RED` and never `GREEN`.
        return {"repo": repo, "verdict": UNKNOWN,
                "why": (f"pipeline #{pipeline.get('id')} is {pipeline.get('status')} but its jobs "
                        f"could not be read ({jobs_why}), so a failure that ran and a failure "
                        f"that never started cannot be told apart"),
                "pipeline": pipeline.get("id"), "sha": pipeline.get("sha"), "branch": branch()}

    state, reason = classify(pipeline, jobs)
    return {"repo": repo, "verdict": state, "why": reason,
            "pipeline": pipeline.get("id"), "sha": (pipeline.get("sha") or "")[:8],
            "branch": pipeline.get("ref")}


def repos() -> list[str]:
    """The repositories to read, from `apps.json` rather than a list typed in here.

    The registry is the family's own record of what exists, it is already the thing
    `web/vite.config.ts` reads at build time, and a hardcoded list would be a second one that
    disagrees. `meta` is included because it has a `.gitlab-ci.yml` shape to judge even
    where its own CI is not configured yet.
    """
    path = factory_config.registry_path()
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return []
    found = [a["repo"] for a in doc.get("apps", []) if isinstance(a, dict) and a.get("repo")]
    for lib in doc.get("libraries", []):
        if isinstance(lib, dict) and lib.get("repo"):
            found.append(lib["repo"])
    found.append(factory_config.meta_repo())
    seen, out = set(), []
    for name in found:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def survey(names: list[str] | None = None) -> list[dict]:
    return [repo_verdict(name) for name in (names if names is not None else repos())]


def print_table(rows: list[dict]) -> None:
    width = max((len(r["repo"]) for r in rows), default=10)
    for r in rows:
        print(f"  {r['repo']:<{width}}  {r['verdict']:<8}  {(r['sha'] or '-'):<9}"
              f"{r['pipeline'] or '-':<12} {r['why']}")
    blocked = [r for r in rows if r["verdict"] == BLOCKED]
    red = [r for r in rows if r["verdict"] == RED]
    unknown = [r for r in rows if r["verdict"] == UNKNOWN]
    print()
    # The counts are separated on purpose. A monitor that collapses "seven repositories are red
    # because the runners have no minutes" into one red number is the defect this file exists to
    # prevent, and the number a reader quotes is the number that decides what they do next.
    print(f"  {len(red)} RED (a job ran and failed — code), "
          f"{len(blocked)} BLOCKED (no job ever ran — CI minutes), "
          f"{len(unknown)} UNKNOWN (cannot tell)")
    if blocked:
        print("  BLOCKED is not a pass: nothing in those repositories was verified.")


def main() -> int:
    ap = argparse.ArgumentParser(description="what GitLab says about main, and whether it is a "
                                             "code defect")
    ap.add_argument("--once", action="store_true", help="print the table (the default)")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object on stdout, for programs. Never parse the table.")
    ap.add_argument("--repo", action="append", default=None,
                    help="read one repository; repeatable. Defaults to the family registry.")
    args = ap.parse_args()

    rows = survey(args.repo)
    if args.json:
        # `BLOCKED`/`UNKNOWN` are reported as their own values so a consumer cannot mistake the
        # absence of a code defect for the presence of a verification.
        print(json.dumps({"branch": branch(), "repos": rows}, indent=1, sort_keys=True))
        return 0
    if not rows:
        print("  no repositories to read — apps.json was unreadable or empty", file=sys.stderr)
        return 2
    print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())