#!/usr/bin/env python3
"""close-gone-workspaces.py — close Herdr workspaces whose saved directory is gone.

Herdr records each pane's working directory. When that directory no longer exists the
pane cannot be restored, and Herdr's own message for the state is:

    Saved directory is unavailable. Restore the directory and restart this session.

(see `src/persist/restore.rs` and the deferred agent-resume path in `src/app/mod.rs`.)

A worker workspace reaches that state whenever its git worktree is removed by any path
other than `cleanup-workers.sh` — a manual `git worktree remove`, a crashed sweep, or a
worktree deleted straight from disk. The workspace then stays open for good, holding a
pane and a dead agent in the workspace list forever.

`cleanup-workers.sh` cannot clean this up. It reads
`.worktree.is_linked_worktree // false`, and Herdr drops the entire `worktree` object
once the checkout path is gone — so a **deleted** worktree and a **primary checkout**
produce the same value, land in the same bucket, and are reported as
`primary checkout, never reclaimed`. That is the gone-worktree case.

This script is the safety net, and it needs no git and no repository at all: a directory
that no longer exists cannot be a live worktree, so it is fleet-wide by construction and
cannot be confused by which repository it was run against.

```sh
python3 scripts/close-gone-workspaces.py            # dry-run, prints the table
python3 scripts/close-gone-workspaces.py --yes      # close them
python3 scripts/close-gone-workspaces.py --audit    # classify every workspace
python3 scripts/close-gone-workspaces.py --only wN1 --only wN2   # just these
```

Dry-run by default. Coordinator only — a worker is running inside a directory this
sweep may remove. Sessions are never deleted: the opencode transcript lives in
`opencode.db` independently of the pane, so closing the workspace preserves it, and the
session id is printed so you can still reach it.

## `--only`, and why it exists

`cleanup-workers.sh` runs repository-scoped and hands over the workspaces whose worktree
is gone; this sweep runs fleet-wide. It needs the reverse: **the caller decides
which workspaces are even on the table, and this script still decides whether each one may
be closed.** Without `--only` the two tools disagree about scope, and one of them silently
covers for the other — which is the failure this family keeps hitting.

So `--only` narrows the candidate set and nothing else. Every refusal below still applies
to every named workspace, and a `--only` that names a workspace Herdr does not know is
reported rather than ignored: a filter that silently matches nothing looks exactly like a
sweep that found nothing to do.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
MANIFEST = str(Path(__file__).resolve().parent.parent / ".fleet-state" / "fleet.json")

# Three verdicts, not two. "Nothing to do" and "would not" are different facts, and a
# boolean here collapses them — which is how a report ends up claiming it removed
# something it never considered.
CLOSE = "CLOSE"
SKIP = "SKIP"
REFUSE = "REFUSE"

LIVE = "LIVE"
DEAD = "DEAD"

DELETED_SUFFIX = " (deleted)"


# --------------------------------------------------------------------- predicates


def saved_directory(cwd: str | None) -> str:
    """The directory Herdr recorded for a pane, with its `(deleted)` suffix removed.

    Stripped once: a path literally ending in ` (deleted)` is still a path.
    """
    if not cwd:
        return ""
    if cwd.endswith(DELETED_SUFFIX):
        return cwd[: -len(DELETED_SUFFIX)]
    return cwd


def parse_pane_read(stdout: str) -> str:
    """Did `herdr pane read <pane>` answer, or is the pane gone?

    **It returns raw terminal text, not JSON.** A caller that runs `json.loads` on it
    gets a `JSONDecodeError`, and a caller that reads that exception as "no error
    returned" concludes every live pane is dead — which is one step away from closing
    the coordinator's own workspace. So: only a JSON payload carrying an `error` key
    means the pane is gone. Anything else that produced output means it answered.
    """
    if not stdout.strip():
        return DEAD
    try:
        return DEAD if "error" in json.loads(stdout) else LIVE
    except json.JSONDecodeError:
        return LIVE


def classify(workspace, panes, *, protected=frozenset(), live_panes=frozenset(),
              exists, runtime):
    """Decide one workspace. Returns `(verdict, reason, detail)`.

    Order matters and is part of the contract: a workspace is only ever a candidate
    once its directory is *proven* gone, and every refusal is reported with the reason
    that produced it.
    """
    wid = workspace.get("workspace_id")
    label = workspace.get("label") or ""

    if not panes:
        # No pane, so no recorded directory. Nothing to judge — and no workspace may be
        # invented from an empty list.
        return SKIP, "no pane, so no saved directory", ""

    paths = [saved_directory(p.get("cwd")) for p in panes]
    paths = [p for p in paths if p]
    if not paths:
        return SKIP, "no pane records a saved directory", ""
    survivors = sorted({p for p in paths if exists(p)})
    if survivors:
        return SKIP, "saved directory still exists", "; ".join(survivors)

    gone = sorted(set(paths))
    detail = "; ".join(gone)

    if wid in protected:
        return REFUSE, "protected: coordinator workspace", detail
    if any(p.get("pane_id") in live_panes for p in panes):
        return REFUSE, "protected: pane in the live fleet manifest", detail

    statuses = {p.get("agent_status") for p in panes}
    if "working" in statuses:
        return REFUSE, "agent still working", detail

    runtimes = {runtime(p.get("pane_id")) for p in panes if p.get("pane_id")}
    if LIVE in runtimes:
        # The directory is gone but the TUI is still up: a worker whose worktree was
        # deleted underneath it. Whether to close that is a judgement about its branch,
        # not a fact this sweep can read. Refuse and let the coordinator look.
        return REFUSE, "pane runtime still live", detail

    sessions = sorted({((p.get("agent_session") or {}).get("value") or "-")
                       for p in panes})
    return CLOSE, "saved directory gone, nothing running", f"{detail} | {' '.join(sessions)}"


# ------------------------------------------------------------------------- herdr


def herdr_json(*args, env=None):
    p = subprocess.run(["herdr", *args], capture_output=True, text=True,
                       env=env or {**os.environ, "HERDR_ENV": "1"})
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def pane_runtime(pane_id, env=None):
    p = subprocess.run(["herdr", "pane", "read", pane_id], capture_output=True,
                       text=True, env=env or {**os.environ, "HERDR_ENV": "1"})
    return parse_pane_read(p.stdout)


def coordinator_workspace(env=None):
    """The workspace this process is running in, from the environment.

    Never hardcoded: the pane id changes when this session ends, and a stale value
    either protects nothing or protects the wrong workspace.
    """
    e = env if env is not None else os.environ
    return e.get("HERDR_WORKSPACE_ID") or ""


def live_fleet_panes(manifest=MANIFEST):
    """Pane ids the fleet believes are live, from the manifest.

    The manifest outranks a vanished directory: a worktree can be deleted by a sweep
    running in another session while its worker is still mid-turn.
    """
    try:
        with open(manifest) as fh:
            return set(json.load(fh).values())
    except (OSError, ValueError):
        return set()


# ---------------------------------------------------------------------------- run


def run(*, argv, herdr, protected, live_panes, exists, runtime, out=sys.stdout):
    """The sweep. Every Herdr call arrives as `herdr`, so this is testable offline."""
    apply = "--yes" in argv
    audit = "--audit" in argv
    protect = set(protected)
    for i, a in enumerate(argv):
        if a == "--protect" and i + 1 < len(argv):
            protect.add(argv[i + 1])

    workspaces = herdr("workspace", "list").get("result", {}).get("workspaces", [])

    # `--only` narrows the candidates and nothing else. An empty list means "no filter",
    # so the fleet-wide default is untouched; a non-empty one that matches nothing is a
    # fact the caller needs to see, not a run that silently found no work.
    only: set[str] = set()
    for i, a in enumerate(argv):
        if a == "--only" and i + 1 < len(argv):
            only.update(w for w in argv[i + 1].split(",") if w)
    unknown: set[str] = set()
    if only:
        unknown = only - {w.get("workspace_id") for w in workspaces}
        workspaces = [w for w in workspaces if w.get("workspace_id") in only]

    panes = herdr("pane", "list").get("result", {}).get("panes", [])
    by_ws: dict[str, list] = {}
    for p in panes:
        by_ws.setdefault(p.get("workspace_id"), []).append(p)

    closed, refused, skipped = [], [], []
    for w in sorted(workspaces, key=lambda x: x.get("number") or 0):
        verdict, reason, detail = classify(
            w, by_ws.get(w.get("workspace_id"), []), protected=protect,
            live_panes=live_panes, exists=exists, runtime=runtime)
        row = (w.get("workspace_id"), w.get("label") or "", reason, detail)
        if verdict == CLOSE:
            if apply and not audit:
                herdr("workspace", "close", w.get("workspace_id"))
            closed.append(row)
        elif verdict == REFUSE:
            refused.append(row)
        else:
            skipped.append(row)

    mode = "AUDIT" if audit else ("APPLYING" if apply else "DRY RUN (re-run with --yes)")
    scope = f"in scope: {len(workspaces)} of {len(workspaces) + len(unknown)}" if only \
        else f"workspaces: {len(workspaces)}"
    print(f"close-gone-workspaces.py — {mode}", file=out)
    print(f"{scope}   close: {len(closed)}   "
          f"refused: {len(refused)}   nothing to do: {len(skipped)}", file=out)
    if unknown:
        # A `--only` naming nothing is indistinguishable from a sweep that found nothing
        # to do, which is exactly the "green because it did nothing" failure.
        print(f"--only named {len(unknown)} workspace(s) Herdr does not know: "
              f"{' '.join(sorted(unknown))}", file=out)
    print(f"coordinator workspace {protected or '(not detected)'} is protected; "
          f"live fleet panes {sorted(live_panes) or '(none recorded)'}", file=out)

    if audit:
        buckets = [("would close", closed), ("refused", refused),
                   ("nothing to do", skipped)]
    else:
        buckets = [("closed" if apply else "would close", closed),
                   ("refused", refused)]
    for title, rows in buckets:
        if not rows:
            continue
        print(f"\n{title} ({len(rows)}):", file=out)
        for wid, label, reason, detail in rows:
            print(f"  {wid:6s} {label[:30]:30s} {reason}", file=out)
            if detail and (audit or title != "nothing to do"):
                print(f"         {detail}", file=out)
    if not closed and not refused:
        if unknown:
            print("\nnothing to close, and nothing to look at: every --only name was unknown.",
                  file=out)
        else:
            print("\nnothing to close.", file=out)
    return closed, refused, skipped


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    env = {**os.environ, "HERDR_ENV": "1"}
    if not shutil.which("herdr"):
        print("close-gone-workspaces.py: herdr not found", file=sys.stderr)
        return 2
    run(argv=argv, herdr=lambda *a: herdr_json(*a, env=env),
        protected={coordinator_workspace(env)} | {REPO_ROOT},
        live_panes=live_fleet_panes(), exists=os.path.exists,
        runtime=lambda pid: pane_runtime(pid, env))
    return 0


if __name__ == "__main__":
    sys.exit(main())