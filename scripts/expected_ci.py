#!/usr/bin/env python3
"""expected_ci — the declared-CI-state mechanism, read and classified ONCE for both scripts.

The declaration mechanism, and the gap found by using what it merged.

## Why this file exists, and why it is a module and not a copy

`fleet-keepalive.py` grew the declaration mechanism: a state file, `FLEET_EXPECTED_CI`, listing
repositories whose CI state is *the answer rather than a gap*. `notify-coordinator.py` **never
learned about it** — it read GitLab itself and emitted the same eleven repositories in the same
voice every thirty minutes, through the other door. The keepalive log went clean and the wake-up
body did not, which is the failure the declaration mechanism was built for, still arriving.

The obvious repair inside `notify-coordinator.py` is to write a second reader. That is the specific
failure this repository keeps hitting: **two readers of one state file that disagree**, each
correct about its own fixture and neither able to see the other. So the reader lives here and both
scripts import it. There is one place where `repositories` is spelled, one place where a
declarable state is decided, and one place where a declaration is placed.

## The rule this module exists to keep

**Declaring can only ever make a report louder, never quieter.** A declaration is a claim, and
nothing in either script can check a claim, so a declared state *withholds* the all-clear instead
of granting it. The comfortable implementation — `if repo in declared: skip` — is a silencer, and
a silencer is worse than noise because it is trusted.

So `place()` has exactly one job: decide where a repository's line goes **and what is wrong with
the declaration at the same time**.

| declared | observed | placement | stale? |
|---|---|---|---|
| matching | same state | `declared` — reported in its own section, never cleared | no |
| any | `RED` | `red` — **loud**, plus `stale` and an `unusable` line | yes |
| any | `BLOCKED` | `blocked` — still reported, still not verified | yes |
| any | `GREEN` / `RUNNING` | `silent` for the row, `stale` for the claim | yes |
| any | `UNKNOWN` / `NONE` / unrecognised | `unverified` — the absence of an answer is still not a pass | yes |

The `RED` row is the one that matters. `social-studio` is declared `NONE` and gains a
pipeline later; the declaration goes stale, and a red lands behind it. A mechanism that lets a
coordinator hide a red behind a stale declaration loses that red for good, so a red behind a
declaration is reported in **both** places — as the fault it is, and as the lying claim that hid it.

## Why the refusals are returned as text and never raised

The existing `FLEET_EXPECTED_RED` loader does `except Exception: return set()`. That is survivable
for a set of worktrees — the report simply shows the GATE-RED again — and it is **not** survivable
here: a typo in the file would silently un-declare eleven repositories, both reports would go back
to being always-true, and nothing would say why. Measured on this mechanism: adding a plausible
`repos` key beside `repositories` made it read as empty and **say nothing at all** — a state file
with two plausible keys is a mechanism that fails silently. So every problem is returned as text and
both scripts print it.

## Why it is imported by path rather than assumed on `sys.path`

Both scripts run from cron **by absolute path** with a bare `PATH` and no `PYTHONPATH`, so neither
`sys.path[0]`-style sibling import nor a package import can be assumed. Each caller puts this
file's directory on `sys.path` explicitly and imports it by name; a missing module is then an
`ImportError` in the cron log, which is loud, rather than a script that quietly reports nothing
declared.

## The shape, read from the committed file rather than assumed

    {"repositories": {"meta": {"state": "NONE", "because": "no .gitlab-ci.yml"}}}

**The key is `repositories`.** Not `repos`, not `entries` — see the note above about the key that
made this read as empty. `scripts/test_notify_coordinator.py` and `scripts/test_fleet_keepalive.py`
both hold the reader: break it, and both go red with their own assertions.
"""

from __future__ import annotations

import sys

import json
import os
from pathlib import Path

# `scripts/expected-ci.json` is not a thing. The state file lives in the repository's
# `.fleet-state/`, beside `expected-red.json`, and the override is the same environment variable
# both callers already honoured — so a coordinator has one place to point them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import factory_config  # noqa: E402

DEFAULT_PATH = factory_config.path("expected_ci")

# The states a declaration may carry, and only these two. They are the ones that are *known and
# durable*: no pipeline on `main` at all, and no job ever started. `GREEN` is deliberately absent —
# it is the only pass, so honouring it would let one line of JSON switch the whole monitor off.
DECLARABLE = frozenset({"NONE", "BLOCKED"})

# Why each verdict is not declarable, so a refusal explains itself rather than merely failing.
REFUSED = {
    "GREEN": "it is the only pass — a declaration cannot create one, and honouring it would let a "
             "single line of this file switch the whole monitor off",
    "RED": "a red main is a code defect, and a declaration that excuses one is a declaration that "
           "loses it",
    "UNKNOWN": "it means GitLab could not be read — declaring that not looking is the answer is "
               "not a declaration, it is a gap",
    "RUNNING": "a pipeline in flight is not a known state, it is the absence of one",
}

# Where a repository's line goes. Named after the sections both scripts print, so a caller's
# rendering reads as a mapping rather than as an interpretation.
DECLARED = "declared"
RED = "red"
BLOCKED = "blocked"
UNVERIFIED = "unverified"
SILENT = "silent"


def path() -> Path:
    """Where the declaration file is, from the environment, or the default."""
    return Path(os.environ.get("FLEET_EXPECTED_CI", str(DEFAULT_PATH)))


def read_declarations() -> tuple[dict[str, dict], list[str]]:
    """`(declarations, refusals)` — the states a coordinator has declared, and every problem.

    The file is a **state file rather than a code edit**, so this is updated without touching a
    script:

        {"repositories": {"meta": {"state": "NONE", "because": "no .gitlab-ci.yml"}}}

    Both halves are returned because they answer different questions, and a caller that collapses
    them is back to one silent value: `declarations` is what is in force, `refusals` is what could
    not be read and therefore what must be reported rather than assumed absent.
    """
    p = path()
    try:
        doc = json.loads(p.read_text())
    except FileNotFoundError:
        # Nothing declared is the ordinary case, not a fault. A file that exists but cannot be
        # read is a different fact and is reported as one below.
        return {}, []
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"the declaration file {p} could not be read ({exc}), so nothing is declared "
                    f"and every repository is reported as if it never were"]
    if not isinstance(doc, dict):
        return {}, [f"{p} is not a JSON object, so no declaration is in force"]
    declared = doc.get("repositories", {})
    if not isinstance(declared, dict):
        # The `repositories` key is named here and nowhere else, deliberately. `doc.get("repos")`
        # would return `{}` for a file that declares eleven repositories, and the mechanism would
        # report itself as having nothing declared — which is how a state file with two plausible
        # keys becomes a mechanism that fails silently.
        return {}, [f"{p} has a `repositories` key that is not an object, so no declaration is in "
                    f"force. The key is `repositories` — a differently spelled one reads as EMPTY, "
                    f"not as an error, so check the spelling before assuming nothing is declared"]

    good: dict[str, dict] = {}
    errors: list[str] = []
    for repo, entry in declared.items():
        if not isinstance(repo, str) or not repo.strip():
            errors.append(f"the declaration file names a repository that is not a name: {repo!r}")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{repo}: the declaration is {type(entry).__name__}, not an object "
                          f"holding `state` and `because`")
            continue
        state = entry.get("state")
        if state not in DECLARABLE:
            why = REFUSED.get(state, "it is not a state this script recognises")
            errors.append(f"{repo}: declared state {state!r} is not declarable, because {why}. "
                          f"Declarable states are: {', '.join(sorted(DECLARABLE))}")
            continue
        good[repo] = {"state": state,
                      "because": str(entry.get("because") or "no reason recorded").strip()}
    return good, errors


def place(repo: str, verdict: str, claim: dict | None) -> dict:
    """Where this repository's line goes, and what is wrong with its declaration.

    Pure: it takes the verdict GitLab gave and the claim a coordinator wrote, and returns the
    placement. No I/O, no environment, no globals — so both scripts decide identically and a
    disagreement between them is impossible rather than merely unlikely.

    `claim` is `None` for an undeclared repository, and the placement is then exactly what the never-started rule
    decided: `RED` is a fault, `BLOCKED` is billing, `UNKNOWN` and `NONE` mean `main` was not
    verified, and `GREEN`/`RUNNING` have nothing outstanding.

    The declaration is **checked, never trusted**, and the order is the whole point: it relocates a
    row only while it still matches. The convenient implementation is `if claim: relocate; continue`,
    and that is precisely what would swallow the red behind a stale declaration.
    """
    stale = None
    unusable = None
    if claim and verdict != claim["state"]:
        stale = (f"declared {claim['state']} ({claim['because']}), and GitLab now says {verdict}")
        if verdict == RED.upper() or verdict == "RED":
            # Worse. A job ran and failed behind a declaration that said otherwise, so it is a
            # fault AND the file is wrong. Both halves are reported, because reporting only the red
            # leaves the coordinator believing their declaration still works — so they fix the code,
            # leave the file, and the next red arrives behind an unchanged claim.
            unusable = (f"{repo}: declared {claim['state']} and is now RED — the declaration is "
                        f"stale AND a job ran and failed. This is worse than no declaration, "
                        f"because it hides the red behind a claim: {claim['because']}")

    if claim and verdict == claim["state"]:
        bucket = DECLARED
    elif verdict == "RED":
        bucket = RED
    elif verdict == "BLOCKED":
        bucket = BLOCKED
    elif verdict in ("GREEN", "RUNNING"):
        # Nothing outstanding for the row itself. The stale declaration is still reported on top,
        # so the row going quiet never makes the file's wrongness quiet with it.
        bucket = SILENT
    else:
        # `UNKNOWN` and `NONE` both mean `main` was not verified. A verdict this reader does not
        # recognise is still a fact about the repository, and dropping it would be silence.
        bucket = UNVERIFIED

    return {"bucket": bucket, "stale": stale, "unusable": unusable,
            "claim": claim["state"] if claim else None}


def unseen(declared: dict[str, dict], seen: set[str]) -> list[str]:
    """Declarations naming a repository this run did not read.

    Also an unchecked claim, and the one nothing else can catch: the survey is the registry, so a
    renamed or removed repository leaves a declaration pointing at nothing and no row to compare it
    against. It still withholds the all-clear — a declaration nothing can check is a claim.
    """
    return [
        f"{repo}: declared {claim['state']} ({claim['because']}), but this run read no such "
        f"repository — a declaration nothing can check is a claim, and it still withholds the "
        f"all-clear"
        for repo, claim in sorted(declared.items()) if repo not in seen
    ]
