# Runtime — what the factory runs on

Exact setup for a machine that hosts a factory workspace. Versions below were measured on the
reference machine in October 2026; treat them as floors, not pins, and record any deviation in the
workspace's own notes.

| Component | Reference version | Job |
|---|---|---|
| `herdr` | 0.9.3 | terminal workspace manager — one workspace per worker, one pane per agent |
| `opencode` / `opencode2` | 2.0.27 | the agent TUI: the coordinator session and every worker session |
| `python3` | 3.12 | the factory scripts (stdlib only, no test runner, no network) |
| `git` | 2.43 | worktrees, branches, the merge discipline |
| `gh` (GitHub) / `glab` (GitLab) | 2.100 / current | the tracker CLI |
| `agent-browser` | 0.9.1 | real-browser walkthroughs against the running app |
| `showboat` | 0.4.0 | executable demo documents (`demo.md`), the reproducible proof |
| `codegraph` | 1.5.0 (1.6.2 available) | the per-repository symbol graph |
| Docker | 29.x | container images a gate needs (mailpit, the identity provider, engines) |
| Node/pnpm | 22 / 10 | the frontend half of the default stack |
| cron | — | the keepalive, the coordinator wake-up, the main sweep |

One requirement is not yet distributable: **`herdr` is not packaged for other machines yet** —
until it is, treat the factory as a single-machine tool (`docs/STATUS.md` § *Decisions*, E3).

## The sessions

- **The coordinator** is an opencode session the user starts by hand and labels
  `AI · coordinator`. It runs in the meta repository (or the project coordinator's repository) and
  uses `HERDR_ENV=1`; read the Herdr skill with `herdr --skill` once per session if you have not
  used it before.
- **Workers** are `opencode2 --auto` sessions started by the coordinator inside a per-issue Herdr
  workspace (see `FACTORY.md` § *Launch one worker*). Workers never launch sessions.

### The model pin

A worker's model comes from **configuration**, not from a command flag: `opencode2 --auto` carries
none. Pin it for the whole workspace in the `opencode.json` **one directory above the repositories**
so every worktree inherits it, and name the fallback order in the workspace's `AGENTS.md`:

```jsonc
// <workspace>/opencode.json — the file every worktree inherits
{
  "$schema": "https://opencode.ai/config.json",
  "model": "<provider>/<coordinator-model>",
  "agents": {
    "build": { "model": "<provider>/<worker-model>#max" }
  }
}
```

**The pin is verified, not decorative.** After launching a worker, read its status line and confirm
it names the intended model; a worker on another model is mislaunched, and the status line is the
cheapest place that says which model a worker is actually on. When a provider limit bites, the
fallback order in `AGENTS.md` applies per worker, and a worker on the fallback moves back up when
the limit lifts — the pin itself never changes for any of this.

## cron — the two jobs that keep the fleet honest

```cron
# every 30 minutes: report readiness, nudge stalled workers, restart a dead agent;
# then wake the coordinator when the set of ready work CHANGES.
*/30 * * * * cd <workspace>/<meta-repo> && \
  python3 scripts/fleet-keepalive.py --stall 2 --cooldown 45 >> /tmp/opencode/fleet-keepalive.cron.log 2>&1; \
  python3 scripts/notify-coordinator.py >> /tmp/opencode/fleet-keepalive.cron.log 2>&1

# hourly: gate every repository's origin/main and record the result (dry means: file nothing).
17 * * * * cd <workspace>/<meta-repo> && \
  python3 scripts/main_sweep.py --dry-run >> /tmp/opencode/main-sweep.cron.log 2>&1
```

Notes that matter under cron:

- **`PATH` is minimal** under cron (`/usr/local/bin:/usr/bin:/bin` plus what you add). Put the
  extra directories in the crontab itself — the reference machine adds `~/.local/bin` and
  `~/go/bin` — because a track cli installed by `go install` is not otherwise found, and the
  failure looks like an authentication error.
- **The keepalive never merges, closes or dispatches**; it nudges and reports. The wake-up prompts
  the coordinator's pane — a notification would only tell a human, a prompt is what resumes an idle
  session. Both are rate-limited by *content*: unchanged work stays quiet.
- **Logs**: `/tmp/opencode/fleet-keepalive.log`, `.cron.log`, `main-sweep.cron.log`. Nothing is
  mailed.
- Worker reclaim (merged worktrees, branches, workspaces) is run **on demand by the coordinator**,
  not by cron — `cleanup-workers.sh` is dry-run by default. A cron reclaim is a second
  implementation waiting to disagree with the first; if a workspace wants one, make it an explicit
  ticket and assert its refusals.

## The tracker CLI

- **GitLab (the default)**: a workspace-level profile directory (`GLAB_CONFIG_DIR`) holding a
  token; load it via `scripts/glab-env.sh`, which walks up from its own location and refuses
  rather than exporting an empty credential. Never point `GLAB_CONFIG_DIR` at a directory named
  `.git` — it breaks the build of every Go module in the workspace (`docs/LESSONS.md` §
  *workspace traps*).
- **GitHub (the other flavor)**: `gh auth status` must be green with issue and PR scopes. `gh`
  needs no config directory beyond its own auth.
- Conventions and the operation set: `docs/TRACKER.md`.

## The browser

`agent-browser` keeps one daemon per **session name**
(`/run/user/<uid>/agent-browser/<session>.sock`), so the unit of exclusion is the session name, not
the machine or the repository. Two runs sharing one name corrupt each other silently. Every
walkthrough therefore takes the browser grant and gets its own session name —
`scripts/browser-grant.py` holds the lock and classifies the failure; `docs/browser-grant.md` has
the mechanism and the measured cross-talk.

## Reproducible proof

`showboat` (0.4.0) records a demo document by running commands and capturing their output;
`showboat verify <file>` re-runs it. Two facts to know: it exits **0 in silence** when it verifies,
and it exits **0 on a document with zero recorded commands** — which is why the merge gate asks
`showboat extract` what the document records, and calls zero entries `NOT-VERIFIED` rather than
green.

## The symbol graph

`codegraph` keeps a SQLite index per repository in `<repo>/.codegraph/`. Indexing is the user's
decision, not the agent's; the usage rules and the traps are `docs/TOOLING.md`. The CLI and the MCP
server are separate installations and can differ in version — upgrade both or state which one is
old.

## State and logs

| Path | Holds |
|---|---|
| `<repo>/.fleet-state/fleet.json` | the manifest of live workers (pane ↔ worktree), git-ignored |
| `<repo>/.fleet-state/expected-ci.json` | **committed** CI declarations — a decision, not machine state |
| `/tmp/opencode/fleet-expected-red.json` | declared-red worktrees, machine state |
| `/tmp/opencode/*.log` | keepalive, notify and sweep logs |

A cleared `/tmp` loses the declared-red file and the logs; it does not lose the manifest, which is
why the manifest lives in the meta repository.
