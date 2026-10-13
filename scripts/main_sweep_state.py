#!/usr/bin/env python3
"""main_sweep_state — the main sweep's last completed run, read and classified ONCE.

`scripts/main_sweep.py` gates every repository's `origin/main` — correctly, with a
dedupe key and honest `NONE`/`NO-GATE` states — and its output died in
`/tmp/opencode/main-sweep.cron.log`, which **nothing reads**. Measured on 2026-10-06:
`social-studio`'s main was red from 10:00 UTC and the sweep gated it **seven times**;
every row said RED at a current head, in a log no person opened. The red was found by a worker
tripping over it three hours later. The log is where reds go to hide.

So the sweep now **records** each completed run beside the rest of the fleet state, and both
halves of the owner-facing report — `fleet-keepalive.py` and `notify-coordinator.py` — read that
record through this module. The alternative the ticket also allows (run the sweep undry from cron
so each red files a deduplicated issue into the repository it belongs to) was rejected for a
reason that is about this mechanism rather than about filing: an issue is a row in a tracker the
coordinator may not be looking at, and the ticket's acceptance is that the red arrives **in the
coordinator report**. The sweep's own dedupe key and dry-run mode are untouched; this module only
makes the last run legible to the report that already interrupts a human.

## What is read, and why it is not the log

The record is JSON, written by the sweep at the end of a **complete** run:

    {"run_at": "2026-10-06T17:17:03Z", "dry_run": true,
     "rows": [{"repo": "portal", "verdict": "RED", "commit": "8134392",
               "key": "portal@8134392", "detail": "…first useful line…",
               "log": "/…/.fleet-state/main-sweep-red-portal-8134392.log"}, …]}

Not the cron log, and the reason is the one this repository keeps re-learning: a log is a
presentation, and a reader that parses one is a second implementation of the writer's format,
free to disagree with it. `expected_ci.py` next door exists for exactly that argument. The sweep
owns this file; the report scripts read it.

## The full output, beside the row

`detail` is the sweep's `first_useful_line` — enough to *see* the failure, deliberately not
enough to *prove* it, and the difference cost real time: `identity`'s panic was sighted
three times without ever yielding its message (a dedicated worker and a load-driven reproduction
were needed to name it), and portal's `errno` was unprovable from the record — its fix eventually
recovered the text from a session's own logs. So a RED row also gets the **full output of the
failing gate step**, written beside the record as

    <record dir>/main-sweep-red-<repo>-<commit>.log

and the row's `log` field names it. Reading the record is then enough to read the failure without
re-running the gate — which is the acceptance for the row, and the reason the file is written by
**both** modes: cron runs the sweep `--dry-run`, so a log written only when issues are filed would
never exist in production.

| policy | value |
|---|---|
| one file per red | `main-sweep-red-<repo>-<commit[:12]>.log`, with `/` in a repo name (`shared/go`) encoded as `__` because a `-` is a character a repository name may itself contain; a re-run at the same commit overwrites rather than accumulates |
| bounded size | each file is capped at `MAX_LOG_BYTES`; over it, the first and last halves are kept with a marker saying how many bytes were dropped |
| rotation | every completed run removes the red logs it does not reference, so storage never exceeds one log per currently-red repository and a green run only ever shrinks it |

**A partial run is never recorded.** `main_sweep.py --repo X` writes nothing, because a record
holding only the repositories it read would be indistinguishable from a full run in which every
other repository was green — a silencer, which is worse than the noise it removes. The writer's
side of that rule lives in `main_sweep.py`; this module only refuses to invent completeness.

## The rule, and it is the same rule as CI

**Absence of a record is not a pass.** A missing record, an unreadable one, one with no parseable
date, and one older than `MAX_AGE_SECONDS` are all `unverified`, and `unverified` withholds the
all-clear rather than granting it — exactly as `expected_ci.place()` withholds it for a declared
CI state. `main` is the ref every future branch is created from, so "not swept" and "swept and
green" must not read the same in the report.

`NO-GATE` and `NO-REMOTE` rows are `unverified` for the same reason, and a verdict this reader
does not recognise is `unverified` too: an unknown word is not evidence of health, and dropping
it would be silence. A **RED** row is a fault, and it is the one thing here that must never be
folded into a count — the report carries the repository, the commit and the sweep's own
`first_useful_line` of the failure, because a row the coordinator cannot act on is not a row.

`TOOL-INCONSISTENT` is a fault in its own right: the sweep contradicted itself, and the fix is
above the repository.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factory_config  # noqa: E402

# The record sits in the main checkout's state directory beside every other fleet state file,
# resolved through `factory_config` so the two halves of the sweep and the report chain agree no
# matter which directory any of them is invoked from. The instance both cron jobs run from is the
# same checkout, which is what makes this path right in production.
DEFAULT_PATH = factory_config.path("main_sweep_record")

# Two sweep intervals. The sweep runs hourly (:17); a record older than this means at least one
# full run was missed, and that is the moment the report must stop implying main is watched. It
# is deliberately not an hour — a run that starts at :17 and takes minutes must not read as
# stale at :30 because the clock rounded.
MAX_AGE_SECONDS = 2 * 60 * 60

# Where a red row's full gate output goes, beside the record, and how big each file may get.
# The prefix is what the rotation globs on, so it is one constant rather than a string typed in
# the writer and again in the pruner — two spellings of it would quietly stop pruning.
RED_LOG_PREFIX = "main-sweep-red-"
MAX_LOG_BYTES = 256 * 1024

# Where a row goes, named after the sections both report scripts print.
RED = "red"
INCONSISTENT = "inconsistent"
UNVERIFIED = "unverified"


def path() -> Path:
    """Where the last completed run is recorded, from the environment, or the default."""
    return Path(os.environ.get("FLEET_MAIN_SWEEP", str(DEFAULT_PATH)))


def _now_iso() -> str:
    """UTC, because a record with a local-time stamp is ambiguous across a timezone change."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_run_at(raw: object) -> float | None:
    """The record's timestamp as epoch seconds, or None when it is missing or unparseable."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def _slug(text: str, limit: int | None = None) -> str:
    """One filesystem-safe token for a log name.

    `/` (only ever the `shared/…` repository names) becomes `__`, not `-`: `-` is a character a
    repository name may itself contain, so `shared/go` and a hypothetical
    `shared-go` would share one file and one repository's log would silently be the
    other's. Everything else outside `[A-Za-z0-9._-]` goes to `-` for the same reason — this name
    is computed from a repository and a commit, and it has to be nameable on every machine.
    """
    out = "".join("__" if c == "/" else (c if (c.isalnum() or c in "._-") else "-") for c in text)
    return out[:limit] if limit else out


def red_log_path(repo: str, commit: str, where: Path | None = None) -> Path:
    """Where one red row's full gate output is kept: beside the record it belongs to.

    `where` is the record path, so the log lands in the same directory by construction — a log
    written to `/tmp` while the record sits in `.fleet-state/` would be one more file the report
    cannot point at, which is the defect this exists to fix.
    """
    p = Path(where) if where is not None else path()
    return p.parent / f"{RED_LOG_PREFIX}{_slug(repo)}-{_slug(commit, 12) or 'unknown'}.log"


def _bounded(text: str, limit: int = MAX_LOG_BYTES) -> str:
    """The full text, capped at `limit` bytes with the head AND the tail kept.

    Dropping only the tail is the tempting version and it is wrong for this payload: a Go test
    run ends with `FAIL` and the package summary, and a `make check` ends with the failing step,
    so the interesting part is as likely to be last as first. The marker states the byte count
    it dropped, because a truncated file whose reader cannot tell it was truncated is a file
    that reads as complete.
    """
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text if text.endswith("\n") else text + "\n"
    half = limit // 2
    head = raw[:half].decode("utf-8", "ignore")
    tail = raw[-half:].decode("utf-8", "ignore")
    dropped = len(raw) - 2 * half
    return (f"{head}\n"
            f"… [truncated {dropped} of {len(raw)} bytes — the first and last {half} kept] …\n"
            f"{tail}\n")


def write_red_log(repo: str, commit: str, output: str, where: Path | None = None) -> Path | None:
    """Write one red row's full gate output beside the record; `None` when there is none.

    `None` rather than an empty file: a red row whose gate produced no output is a row whose
    summary ("gate failed with no output") is the whole story, and a zero-byte log pointed at by
    the record would be a directory entry that says nothing while implying it holds something.
    """
    text = output or ""
    if not text.strip():
        return None
    p = red_log_path(repo, commit, where=where)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_bounded(text))
    return p


def _prune_red_logs(keep: set[str], where: Path) -> None:
    """Remove the red logs the just-recorded run does not reference. Rotation, not housekeeping.

    Bounded by construction: a completed run names at most one log per RED repository, so the
    directory holds at most that many files, and a repository that goes green has its stale log
    removed on the next run — which is what makes *a green run never grows storage* true rather
    than merely intended. The record is replaced **before** this runs, so what a reader of the
    new record can resolve is preserved; a log kept by a reader of the old record is the one
    file this may take away, and the window is one sweep interval wide.

    A failure to unlink is swallowed on purpose: stale storage is not worth failing a record that
    has already landed, and the only caller (`write_run`) must not lose the record over it.
    """
    kept = {str(Path(k).resolve()) for k in keep if k}
    for old in sorted(where.parent.glob(f"{RED_LOG_PREFIX}*.log")):
        if str(old.resolve()) in kept:
            continue
        try:
            old.unlink()
        except OSError:
            pass


def write_run(rows: list[dict], dry_run: bool, where: Path | None = None) -> Path:
    """Record one completed run. Returns the path it wrote.

    Called by `main_sweep.py` for **every** complete run, dry or applying. `--dry-run` means
    "file no issues" — the record is what lets the report chain read the run at all, so a dry run
    that wrote nothing would leave the mechanism this module exists for with nothing to read.
    For the same reason the red rows' full output is written here in both modes: cron runs the
    sweep dry, and a log that only exists when the sweep files an issue is a log that never
    exists.

    Written via `os.replace`, so a reader that arrives mid-write sees the previous record rather
    than half of this one. A truncated record would read as unreadable — a withholding state, not
    a false pass — but there is no reason to manufacture it.
    """
    p = Path(where) if where is not None else path()
    recorded: list[dict] = []
    for row in rows:
        entry = {
            "repo": row.get("repo", ""),
            "verdict": row.get("verdict", ""),
            "commit": row.get("commit", ""),
            "key": row.get("key", ""),
            "detail": row.get("detail", ""),
            "log": "",
        }
        # The gate's full output rides in the row as `output` — in memory only, because a record
        # holding it would be the log the record exists to point at. It lands on disk as a file
        # beside the record for a RED row, and the row names it. GREEN and NONE write nothing.
        if entry["verdict"] == "RED":
            written = write_red_log(entry["repo"], entry["commit"],
                                    str(row.get("output") or ""), where=p)
            entry["log"] = str(written) if written else ""
        recorded.append(entry)
    doc = {
        "run_at": _now_iso(),
        "dry_run": bool(dry_run),
        "rows": recorded,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)
    _prune_red_logs({r["log"] for r in recorded}, p)
    return p


def read_run(where: Path | None = None) -> tuple[dict | None, list[str]]:
    """`(record, refusals)` — the last completed run, and every problem reading it.

    The two are returned separately for the reason `expected_ci.read_declarations()` documents:
    "the file is not there yet" and "the file is there and cannot be read" must not print the
    same way. Both are reported, and neither is a pass.
    """
    p = Path(where) if where is not None else path()
    try:
        raw = p.read_text()
    except FileNotFoundError:
        # Ordinary before the first run of the deployed sweep — and still not a pass, which is
        # decided in `classify()` where the reader can say why.
        return None, []
    except OSError as exc:
        return None, [f"the main sweep's record {p} could not be read ({exc})"]
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"the main sweep's record {p} is not valid JSON ({exc})"]
    if not isinstance(doc, dict):
        return None, [f"{p} is not a JSON object, so no run can be read from it"]
    return doc, []


def _row_line(row: dict) -> str:
    """One row as a report line: repository, verdict, commit, first useful line, and its log.

    The commit is in the line on purpose — the dedupe key is `repo@commit`, and a red that
    reappears at a new commit is a new problem. An empty detail falls back to the sweep's own
    `why` so the line is never merely "RED" with nothing behind it.

    A row with a `log` field carries the path of the full gate output beside it, appended
    rather than replacing the summary: the summary is what a reader scans, the file is what they
    open when the summary is not enough — which is the state the two incidents in the ticket
    were in. The path is last so the repository stays the line's first token, which is what
    `placement_key()` hashes.
    """
    detail = str(row.get("detail") or row.get("why") or "").strip()
    line = (f"{row.get('repo') or '?'} {row.get('verdict') or '?'} "
            f"{row.get('commit') or '-'} — {detail or 'no detail recorded'}")
    log = str(row.get("log") or "").strip()
    if log:
        line += f" [full output: {log}]"
    return line


def classify(doc: dict | None, errors: list[str] | None = None, *,
             now: float | None = None, where: Path | None = None) -> dict:
    """Where every recorded row goes, and whether the record itself can be trusted.

    Pure enough to test without a filesystem: `doc` is what `read_run()` returned, `errors` is its
    refusals, `now` is injectable so a staleness case does not need a clock. Both report scripts
    call this, so they cannot file one run into different states — the failure `expected_ci.py`
    exists to prevent, and the reason the placement is not written twice.

    Buckets:

    | bucket | what | who acts |
    |---|---|---|
    | `red` | a row the sweep marked `RED` | a worker fixes origin/main on a branch |
    | `inconsistent` | the sweep contradicted itself | fix the sweep, not the row |
    | `unverified` | no record / stale / unreadable / `NO-GATE` / `NO-REMOTE` / unknown | nothing is verified; not a pass |
    """
    out: dict = {"red": [], "inconsistent": [], "unverified": [],
                 "verified_green": 0, "swept": 0}
    if doc is None:
        # No record at all, or a record that could not be read. Both are the absence of an
        # answer, and this repository's rule is that the absence of an answer is not a pass.
        if errors:
            out["unverified"] += [f"unreadable-run — {e}" for e in errors]
        else:
            out["unverified"].append(
                f"no-run-recorded — the main sweep has recorded no run in "
                f"{where or path()}, so no repository's main has been swept by it")
        return out

    rows = doc.get("rows")
    if not isinstance(rows, list):
        out["unverified"].append(
            f"unreadable-run — the main sweep's record has no `rows` list, so nothing in it "
            f"can be read as a verdict. The writer and this reader have drifted apart.")
        return out
    if not rows:
        # A complete run always has at least the repositories in apps.json. An empty record is a
        # record of nothing, and a report that read it as "no row is red" would be exactly the
        # silence the sweep's own NO-GATE branch exists to prevent.
        out["unverified"].append(
            "empty-run — the main sweep's record holds no rows, so no repository was swept and "
            "its silence is not a pass")
        return out

    # The date is checked before the rows, because a record whose age is unknown cannot be
    # treated as current — a stop-error here would let a crashed sweep report yesterday's green.
    ts = _parse_run_at(doc.get("run_at"))
    if ts is None:
        out["unverified"].append(
            "undated-run — the main sweep's record carries no parseable `run_at`, so how old "
            "it is cannot be established and its rows cannot be treated as current")
    else:
        age = (now if now is not None else time.time()) - ts
        if age > MAX_AGE_SECONDS:
            out["unverified"].append(
                f"stale-run — the last recorded sweep ran {int(age // 60)} minutes ago "
                f"(limit {MAX_AGE_SECONDS // 60}); main is not currently watched")

    for row in rows:
        if not isinstance(row, dict):
            out["unverified"].append(
                "unreadable-run — a row in the main sweep's record is not an object, so it "
                "cannot be read as a verdict")
            continue
        verdict = row.get("verdict")
        line = _row_line(row)
        if verdict == "RED":
            out["red"].append(line)
        elif verdict == "TOOL-INCONSISTENT":
            out["inconsistent"].append(line)
        elif verdict == "GREEN":
            out["verified_green"] += 1
        else:
            # `NO-GATE`, `NO-REMOTE` — and any verdict this reader does not recognise, which is
            # also not evidence of health. Dropping it would be silence, and silence reads as a
            # pass in a report whose whole job is to say when main was not verified.
            out["unverified"].append(line)
    out["swept"] = len(rows)
    return out


def classify_run(now: float | None = None) -> dict:
    """Read the record and classify it — what both report scripts call."""
    doc, errors = read_run()
    return classify(doc, errors, now=now)


def placement_key(classified: dict) -> str:
    """A stable summary of how the run was placed, for the wake-up's content hash.

    The wake-up is rate-limited by content, not by the clock, and the sweep's placement has to be
    in that hash for the same reason `ci_placement_key()` put the CI placement there: without it,
    a run whose only change is a RED appearing hashes identically to the run before and its first
    appearance is reported as "unchanged since the last wake-up, staying quiet". The red would
    then be in the keepalive log every half hour and interrupt nobody — the sweep record, restored.

    Deliberately the repository (and the run-state token), **not** the commit or the detail: a
    new commit on a red main is the same situation continuing, and hashing it would turn this
    into a wake-up per push — the alert the declaration mechanism was built to remove. A red that is fixed and then
    regresses leaves the set and re-enters it, which is a change, and wakes.
    """
    parts: list[str] = []
    for bucket in (RED, INCONSISTENT, UNVERIFIED):
        for line in classified.get(bucket) or []:
            # A row's first token is its repository; a run-state line's first token is the state
            # (`no-run-recorded`, `stale-run`, …). Both are the right granularity: repo-level for
            # rows, state for the record, so missing → stale → green each read as news once.
            parts.append(f"{bucket}:{line.split(' ', 1)[0]}")
    return "|".join(sorted(parts))
