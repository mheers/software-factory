# scripts/ — the factory tooling

Python 3 stdlib, no test runner, no network. Every script prints a verdict and exits non-zero when
the verdict is not a pass; **exit code 2 means "cannot tell" and is never a pass.**

## Status legend

- **Generic** — no workspace-specific behaviour; usable as-is.
- **Coupled** — assumes the factory's layout (a meta repo beside member repositories, the
  `shared/` convention, a tracker). Layout constants resolve through `factory_config` —
  environment, then `factory.json`, then the shipped defaults — and the tracker goes through
  `scripts/tracker.py`; whatever coupling remains is named per row.
- **Flavor** — one supported tracker/CI implementation of a mechanism that has more than one.
  Both flavors ship — `glab` is the default, `gh` is kept — selected by
  `factory_config.tracker_provider()` (`docs/TRACKER.md`).

## Runtime tooling

| Script | What it does | Status |
|---|---|---|
| `factory_config.py` | the one configuration seam — environment → `factory.json` → shipped defaults; exit 2 on a config the operator named but cannot be read; also the shell CLI (`branch-prefix`, `tracker-provider`, `path <key>`) | Generic |
| `tracker.py` | the tracker adapter — `open_issues` / `file_issue` / `issue_state` / `mr_count`, GitLab (`glab`, the default) and GitHub (`gh`) flavors selected by `factory_config`; the CLI the shell side reads (`state`, `mr-count`) | Generic |
| `github_ci.py` | the **GitHub flavor** of the CI reader: run/job classification with the same verdict vocabulary as `gitlab_ci.py` (imported from it, so the words cannot drift); a failed job with no `startedAt` is `BLOCKED`, not `RED` | Flavor |
| `fleet-watch.py` | sweeps live worker branches and reports `MERGEABLE` / `IN-FLIGHT` / `GATE-RED` / `NO-GATE` / `NOTHING`; the completion signal the coordinator trusts; `--watch` is the callback | Coupled (workspace walk, `shared/` fetch convention, gate order) |
| `fleet-keepalive.py` | from cron: reports readiness, nudges stalled workers, restarts a dead agent; never merges, closes or dispatches. CI reader: the provider-selected flavor | Generic |
| `notify-coordinator.py` | wakes the coordinator's pane when the set of ready work changes; reports facts, carries no policy. Layout via `factory_config`; tracker and CI through the provider-selected flavors | Generic |
| `merge-gate.py` | the merge gate as a program: shared sources → the repository's own gate → `showboat verify demo.md`; four states, not two | Generic |
| `cleanup-workers.sh` | reclaims merged worker worktrees, branches and Herdr workspaces; dry-run by default; tracker checks through `tracker.py` (`--no-tracker` skips them) | Coupled (worktree layout, GitLab profile flavor) |
| `close-gone-workspaces.py` | closes Herdr workspaces whose saved directory is gone; no git and no repository needed | Generic |
| `browser-grant.py` | one `agent-browser` session, one holder; refuses with exit 75 rather than queueing; classifies the failure | Generic |
| `main_sweep.py` + `main_sweep_state.py` + `first_useful_line.py` | gates every repository's `origin/main`, records every completed run, files a deduplicated issue (through `tracker.py`) for a red one, and writes the failing step's full output beside the record. Layout via `factory_config` | Coupled (registry) |
| `expected_ci.py` | the declared-CI-state mechanism: a declaration makes the report louder, never quieter; shared by the keepalive and the wake-up | Generic (state file resolved via `factory_config`) |
| `gitlab_ci.py` | the **GitLab flavor** of the CI reader — the default reader: pipeline `RED` vs `BLOCKED` vs `UNKNOWN`, kept apart. The flavors are selected by `factory_config.tracker_provider()` | Flavor |
| `glab-env.sh` | loads the workspace-local GitLab profile; refuses rather than exporting an empty credential; the token export does not depend on the binary being on `PATH` | Flavor |
| `port-allocation.py` | one allocation of golden-path ports for the whole family, with reclaim of stale grants | Coupled (registry) |
| `reap-port-holders.py` | reports (and with `--yes` kills) test processes holding a TCP port; finds holders by process, not by scanning a range | Generic |
| `setup-gitlab.sh` | idempotent tracker setup: labels, milestones, issue templates, optionally an initial backlog | Flavor (group and id are configuration) |
| `setup-github.sh` | idempotent tracker setup for one GitHub repository: the scoped label taxonomy, the three milestones, and the issue forms copied into `.github/ISSUE_TEMPLATE/` when missing | Flavor |

## Tests

Every script above has a test beside it (`test_<name>.py` / `test_<name_with_underscores>.py`),
except `first_useful_line.py` (covered through `test_main_sweep.py`) and the two setup scripts
(`setup-gitlab.sh` / `setup-github.sh`, no test yet — see `docs/STATUS.md`). The tests build scratch families and stub the external binaries
(`herdr`, `git`, `glab`, `gh`) through `PATH`; they are the reason the coupled scripts can be extracted
later without guessing their behaviour.

`test_meta_gate.py` runs the gate block in `AGENTS.md` **as written** and asserts every
`scripts/test_*.py` that exists is named in that block — the block is what executes the suite.
`test_smoke.py` drives the real `fleet-watch.py` / `main_sweep.py` pair end to end against a
scratch workspace with real git and real `make`; `test_shell_syntax.py` asks every script to
parse, because a file nothing runs can sit broken until somebody runs it.

**Known environment-dependent cases** (workspace state, not repository defects):

- `test_notify_coordinator.py` reads the committed `.fleet-state/expected-ci.json`; a fresh
  workspace must ship one (the template does).
- `test_gitlab_ci.py` has one case that asserts the workspace's own `.gitlab-cli/` profile resolves
  under cron's `PATH`; it needs a configured GitLab workspace.

## Not here yet

The remaining extraction (see `docs/STATUS.md`): the generalized per-repository contract guard,
and the phase-2 shortening of member contracts once the meta contract is reliably reachable.
