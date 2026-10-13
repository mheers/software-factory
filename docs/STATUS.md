# STATUS — what is genericized, and what is not yet

This repository was extracted **docs-first** from a working factory implementation. The contract
and the reference documents are generic; the scripts are the working implementation, **sanitized
of the reference workspace's names, absolute paths and tracker identifiers**, with their layout
resolved at runtime through `factory_config.py` (environment → `factory.json` → shipped defaults)
and their tracker and CI reads through provider-selected flavors — `glab` by default, the GitHub
flavor kept. What remains is listed below.

Hosted at `github.com/mheers/software-factory` (MIT — see `LICENSE`). Measured state:
**2026-10-13**.

## Done in this pass

| Area | What happened |
|---|---|
| Contract | `AGENTS.md` rewritten as the generic workspace contract (compressed from ~2000 lines to the rules; the factory mechanics split out). |
| Runbook | `FACTORY.md` created — the coordinator mechanics, moved out of the agent contract. |
| Design | `META-PROJECT.md` replaced with a generic cross-system design skeleton (placeholders, decisions register, tenancy rule). |
| Reference docs | `docs/LESSONS.md` (the incidents behind the rules), `docs/TRACKER.md`, `docs/RUNTIME.md`, `docs/ADOPTION.md`, `docs/TOOLING.md`, `docs/TESTING-MAILPIT.md`, `docs/PIPELINES-DAGGER.md`, `docs/browser-grant.md`. |
| Templates | `templates/` — the per-repo contract guide, the project skeleton, the issue templates, `factory.json.example`. |
| Pruned | Client product documents (legal annexes, hosting shortlist, demonstration estate, boundary design), the client-specific guards and their tests, the landscape `Makefile`, and the generated-state tests. All of it still lives in the reference workspace. |
| Scripts | Sanitized: every reference-workspace name, absolute path and tracker identifier in `scripts/` and its tests replaced with neutral placeholders. |
| Configuration | `scripts/factory_config.py` — the one seam: environment → `factory.json` → shipped defaults, **exit 2** on a config the operator named but cannot be read; the shell CLI included. Every coupled script's layout resolves through it. |
| Tracker | `scripts/tracker.py` — `open_issues` / `file_issue` / `issue_state` / `mr_count`, `glab` (default) and `gh` flavors selected by configuration; `main_sweep`, `cleanup-workers.sh` and the keepalive's not-started nudge read through it. |
| CI reader | `scripts/github_ci.py` — `gh run list` / `gh run view --json jobs` classified with the GitLab flavor's vocabulary (imported, so the words cannot drift); the two reds are told apart by whether a job ever started. |
| GitHub setup | `scripts/setup-github.sh` — one repository at a time: the scoped label taxonomy, the three milestones, and the issue forms copied into `.github/ISSUE_TEMPLATE/` when missing. |
| CI | `.github/workflows/test.yml` runs the gate block (through `test_meta_gate.py`) on push and pull request. |
| Smoke | `scripts/test_smoke.py` — a scratch workspace with two synthetic repositories, real git and real `make`: `fleet-watch` reports `MERGEABLE` and `GATE-RED`; `main_sweep --dry-run` records a complete run and a partial run leaves the record byte-identical. It found `repos_from_apps` resolving repositories against the script's own parent instead of the configured workspace. |
| Added | `.fleet-state/expected-ci.json` (an empty declarations file, deliberately committed), `scripts/test_meta_gate.py` (runs the documented gate block as written), `scripts/README.md` (per-script status). |

## Not yet done — the remaining extraction

Ordered so each step keeps the suite green.

1. **Per-repo contract guard** — the `check-agents-md.sh` idea, generalized: every repository in
   the registry carries a contract that states the shared invariants.
2. **Per-repo contract phase 2** — shorten member contracts once the meta contract is reliably
   reachable (a link plus the repository-specific section, not a full copy).

## Decisions (the extraction plan's open questions)

Recorded 2026-10-13.

| # | Decision | Why |
|---|---|---|
| E1 maintenance direction | **New projects only** for cut 1 — this repository is the template for new workspaces; the reference workspace keeps its own copy. Revisit after the first external use. | A drift guard is only worth building once something consumes this as upstream. |
| E2 license & visibility | MIT (`LICENSE`); **public**. | The owner's call; contact in the README. |
| E3 `herdr` distribution | **Single-machine tool for now** — `herdr` is not packaged for other machines yet (`docs/RUNTIME.md`). | The factory is useless without it; stating the limit beats a broken adoption. |
| E4 dual tracker | **`glab` is the shipped default; the GitHub flavor is kept** (`github_ci.py`; `tracker.py` with `provider: github`; `glab-env.sh` is the default flavor's loader). | One mechanism, two CLIs. |
| E5 stack template | The default stack stays the **template default**; stack-neutrality is a later generalization. | `templates/PROJECT_TEMPLATE.md` is a skeleton, not a requirement. |

Deliberate deviations from the plan as written, recorded so they are not re-litigated:

- **No committed root `factory.json`.** Resolution is environment → `factory.json` → shipped
  defaults; a missing file is not an error (the plan's "exit 2 when missing" would break the
  defaults it asked for in the same breath). Exit 2 is kept for a configuration the operator
  **named** that cannot be read or parse.
- **`tracker-env.sh` was not needed**: `gh` owns its credential store, so the GitHub flavor needs
  no profile loader; `glab-env.sh` remains the GitLab flavor's.
- **`issue_comment` / `issue_close` are not implemented** — no shipped script calls them; workers
  comment and close through the tracker CLI in their brief.
- **No GitHub Project board** (the plan marked it optional), and **no issues filed in this
  repository** (the B9 dogfooding point) — the owner chose to keep the work list in chat for now.

## Measured test status

Run with `python3 scripts/test_<name>.py` in this repository, 2026-10-13:

- **Green:** `test_factory_config`, `test_tracker`, `test_github_ci`, `test_fleet_watch`,
  `test_fleet_watch_shared_sources`, `test_fleet_keepalive`, `test_notify_coordinator`,
  `test_merge_gate`, `test_cleanup_workers`, `test_close_gone_workspaces`, `test_browser_grant`,
  `test_main_sweep`, `test_port_allocation`, `test_reap_port_holders`, `test_glab_env`.
- **Environment-dependent:** `test_gitlab_ci` has one case that asserts the *workspace's*
  `.gitlab-cli/` profile resolves under cron's `PATH`; run it in a configured GitLab workspace.
- The gate block in `AGENTS.md` names the green set; `test_meta_gate.py` runs it as written and
  asserts no `scripts/test_*.py` is missing from it.

## The reference implementation

The workspace this was extracted from keeps the client content, the full product documents and the
client-specific guards. Nothing was deleted from the world by this cleanup — it was left behind.
If a document from that workspace is needed here, extract the *mechanism* (as was done for
`expected_ci.py`), not the product decisions.
