#!/usr/bin/env python3
"""The GitHub flavor of the CI reader: what GitHub says about `main`, and whether it is a defect.

Same verdict vocabulary as the GitLab flavor — imported from it, so the words cannot drift —
and the same discipline about the two reds:

- a job that **ran and failed** is a code defect (`RED`);
- a job that **never started** proves nothing (`BLOCKED`): the GitHub analogue of GitLab's
  `failure_reason` + no-timing signature is a failed job with **no `startedAt`** — no runner
  ever picked the work up. Whether the cause is billing, runner capacity or configuration
  cannot be told from here, and the message does not claim it.

Read from `gh run list` / `gh run view --json jobs` (never the printed table: a wide table
invites reading a number out of the wrong column, which has already produced a confident claim
about a bug that did not exist). `--json` prints `{"branch": …, "repos": [...]}` for programs.

Exit codes: 0 when the survey ran (whatever the verdicts); 2 when there was nothing to read.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import factory_config  # noqa: E402

# The verdict vocabulary is one contract. The GitLab flavor owns the definitions and this reader
# imports them, so a verdict cannot exist in one flavor and not the other.
from gitlab_ci import (  # noqa: E402
    BLOCKED,
    GREEN,
    NONE,
    RED,
    RUNNING,
    UNKNOWN,
    VERDICTS,
)

DEFAULT_BRANCH = "main"

# `gh run list` statuses that mean the run is in flight.
IN_FLIGHT = frozenset({"queued", "in_progress", "requested", "waiting", "pending"})

# Conclusions that mean nobody ran the code: the run was cancelled, skipped, or superseded.
NOTHING_RAN = frozenset({"cancelled", "skipped", "neutral", "stale"})

# Conclusions that mean something went wrong.
FAILED = frozenset({"failure", "timed_out", "action_required"})


def branch() -> str:
    """The ref this reader judges. `main` unless the workspace says otherwise."""
    return os.environ.get("FLEET_GITHUB_BRANCH", DEFAULT_BRANCH)


def api_timeout() -> int:
    try:
        return int(os.environ.get("FLEET_GITHUB_TIMEOUT", "60"))
    except ValueError:
        return 60


def _gh() -> str | None:
    """The `gh` binary, or None — which is `UNKNOWN`, not an error to raise.

    Cron's `PATH` is `/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin`; a `gh` installed into
    `~/go/bin` or `/snap/bin` is "not on PATH" without being absent. Look where it actually is
    before concluding it is gone: "cannot tell" and "did not look properly" are different facts.
    """
    found = shutil.which("gh")
    if found:
        return found
    for candidate in (Path.home() / "go/bin/gh", Path.home() / ".local/bin/gh",
                      Path("/usr/local/bin/gh"), Path("/usr/bin/gh"), Path("/snap/bin/gh")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _gh_json(args: list[str]) -> tuple[object | None, str]:
    """One `gh` call that must print JSON. Returns `(parsed, "")` or `(None, reason)`.

    `args` is everything after the binary; the binary itself comes from `_gh()`.
    """
    binary = _gh()
    if binary is None:
        return None, "`gh` is not on PATH, so GitHub was not read at all"
    try:
        run = subprocess.run([binary, *args], capture_output=True, text=True,
                             timeout=api_timeout())
    except subprocess.TimeoutExpired:
        return None, f"`gh` did not finish within {api_timeout()}s"
    except OSError as exc:
        return None, f"`gh` could not be run ({exc})"
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip().splitlines()
        first = detail[0][:200] if detail else f"exited {run.returncode} with no output"
        return None, f"`gh` exited {run.returncode}: {first}"
    try:
        return json.loads(run.stdout), ""
    except json.JSONDecodeError as exc:
        return None, f"`gh` returned something that is not JSON ({exc})"


def latest_run(repo: str) -> tuple[dict | None, str]:
    """The most recent workflow run on the family's branch, or `(None, reason)`.

    `--limit 1` with the API's default ordering: newest first, which is what "the latest run on
    main" means. `headSha` is returned so a verdict can name the commit it judged.
    """
    payload, why = _gh_json(["run", "list", "--repo", factory_config.repo_full_name(repo),
                             "--branch", branch(), "--limit", "1",
                             "--json", "databaseId,status,conclusion,headSha"])
    if payload is None:
        return None, why
    if not isinstance(payload, list):
        return None, f"expected a list of runs, got {type(payload).__name__}"
    if not payload:
        return None, f"no workflow run on {branch()} — this repository has never been built there"
    if not isinstance(payload[0], dict):
        return None, "a run row was not an object"
    return payload[0], ""


def run_jobs(repo: str, run_id: int) -> tuple[list[dict] | None, str]:
    payload, why = _gh_json(["run", "view", str(run_id),
                             "--repo", factory_config.repo_full_name(repo), "--json", "jobs"])
    if payload is None:
        return None, why
    if not isinstance(payload, dict):
        return None, f"expected an object with jobs, got {type(payload).__name__}"
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return None, "the run view carried no jobs list"
    return [j for j in jobs if isinstance(j, dict)], ""


def classify(run: dict, jobs: list[dict]) -> tuple[str, str]:
    """`(verdict, why)` for one workflow run and its jobs. PURE — the same contract as the
    GitLab classifier, and tested against both live shapes: a code failure and a
    never-started one."""
    status = run.get("status")
    if status in IN_FLIGHT:
        return RUNNING, f"run #{run.get('databaseId')} is {status}"

    conclusion = run.get("conclusion")
    if conclusion == "success":
        return GREEN, f"run #{run.get('databaseId')} succeeded"
    if conclusion in NOTHING_RAN:
        # A run nobody finished is not a pass and not a fault. `main` was not verified.
        return NONE, (f"run #{run.get('databaseId')} is {conclusion} — nothing was built, so "
                      f"{branch()} is NOT verified")

    if conclusion in FAILED:
        failed = [j for j in jobs if j.get("conclusion") in FAILED]
        if not failed:
            # A failed run with no failed job visible is a contradiction; the honest reading is
            # that this reader cannot see the job that failed — not that there was none.
            return UNKNOWN, (f"run #{run.get('databaseId')} is {conclusion} but none of its "
                             f"{len(jobs)} job(s) is marked failed, so the job that failed is not "
                             f"visible from here. This cannot be called a code defect or a pass — "
                             f"it is a gap in what GitHub returned")

        # Never-started: a failed job with no `startedAt` (and no completion), i.e. no runner
        # ever picked it up. A job with a `startedAt` ran, whatever else is true about it.
        never_started = [j for j in failed if not j.get("startedAt")]
        ran = [j for j in failed if j.get("startedAt")]

        if never_started and not ran:
            return BLOCKED, (f"{len(never_started)} job(s) failed without ever starting — no "
                             f"runner ever picked the work up, so nothing in this repository "
                             f"ran. It is not verified, and this is not evidence of a code "
                             f"defect")

        names = ", ".join(str(j.get("name")) for j in ran[:6])
        if ran and not never_started:
            return RED, f"{len(ran)} job(s) ran and failed ({names}) — this IS a code defect"
        # Mixed. Named rather than resolved: half the run proved the code is wrong, the other
        # half proved nothing. It is still actionable, so it is not `UNKNOWN`.
        return RED, (f"{len(ran)} job(s) ran and failed ({names}) AND {len(never_started)} never "
                     f"started. The code defect is real; the rest is unverified, so this run "
                     f"does not report on every job in it")

    return UNKNOWN, (f"run #{run.get('databaseId')} has status {status!r} and conclusion "
                     f"{conclusion!r}, which this reader does not classify")


def repo_verdict(repo: str) -> dict:
    """One repository's verdict, as a dict. Never raises, never guesses."""
    run, why = latest_run(repo)
    if run is None:
        # A repository with no run and a repository whose run could not be read are different
        # facts, and the second is about this tool rather than about the code.
        unreachable = "not on PATH" in why or "`gh" in why or "JSON" in why
        state = UNKNOWN if unreachable else NONE
        return {"repo": repo, "verdict": state, "why": why,
                "pipeline": None, "sha": None, "branch": branch()}

    run_id = run.get("databaseId")
    jobs, jobs_why = run_jobs(repo, run_id)
    if jobs is None:
        # The run failed or succeeded and we cannot see a single job — so the two reds cannot be
        # told apart. `UNKNOWN`, never `RED` and never `GREEN`.
        return {"repo": repo, "verdict": UNKNOWN,
                "why": (f"run #{run_id} is {run.get('status')}/{run.get('conclusion')} but its "
                        f"jobs could not be read ({jobs_why}), so a failure that ran and a "
                        f"failure that never started cannot be told apart"),
                "pipeline": run_id, "sha": run.get("headSha"), "branch": branch()}

    state, reason = classify(run, jobs)
    return {"repo": repo, "verdict": state, "why": reason,
            "pipeline": run_id, "sha": (run.get("headSha") or "")[:8], "branch": branch()}


def repos() -> list[str]:
    """The repositories to read, from the registry rather than a list typed in here.

    The registry is the workspace's own record of what exists, and a hardcoded list would be a
    second one that disagrees. The meta repository is included because it has a workflow shape
    to judge even where its own CI is not configured yet.
    """
    path = factory_config.registry_path()
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
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
    # The counts are separated on purpose: collapsing "seven repositories are blocked because a
    # runner never started" into one red number is the defect this reader exists to prevent.
    print(f"  {len(red)} RED (a job ran and failed — code), "
          f"{len(blocked)} BLOCKED (no job ever ran — runner or billing), "
          f"{len(unknown)} UNKNOWN (cannot tell)")
    if blocked:
        print("  BLOCKED is not a pass: nothing in those repositories was verified.")


def main() -> int:
    ap = argparse.ArgumentParser(description="what GitHub says about main, and whether it is a "
                                             "code defect")
    ap.add_argument("--once", action="store_true", help="print the table (the default)")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object on stdout, for programs. Never parse the table.")
    ap.add_argument("--repo", action="append", default=None,
                    help="read one repository; repeatable. Defaults to the workspace registry.")
    args = ap.parse_args()

    rows = survey(args.repo)
    if args.json:
        # `BLOCKED`/`UNKNOWN` are their own values so a consumer cannot mistake the absence of a
        # code defect for the presence of a verification.
        print(json.dumps({"branch": branch(), "repos": rows}, indent=1, sort_keys=True))
        return 0
    if not rows:
        print("  no repositories to read — the registry was unreadable or empty", file=sys.stderr)
        return 2
    print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
