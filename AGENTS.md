# AGENTS.md — the workspace contract

Instructions for AI agents working in any repository of this workspace.

This file is the **generic default**. A member repository copies it in and specialises the
`<placeholders>`; `templates/AGENTS.repo.md` says exactly what to change. The workspace itself is
`<workspace>` — a container of sibling clones plus **`<meta-repo>`**, the meta repository that
carries the registry, the factory tooling and these documents. The workspace root is **not** itself
a git repository.

## The documents

| File | Role | Read it when |
|---|---|---|
| `README.md` | what this workspace is, quickstart | you are new here |
| `AGENTS.md` | this contract — how work happens | always |
| `FACTORY.md` | the coordinator runbook | you are the coordinator; **end to end before dispatch** |
| `META-PROJECT.md` | the cross-system design of this workspace (skeleton to fill in) | a rule crosses a system boundary |
| `docs/ADOPTION.md` | standing up a new workspace | you are creating one |
| `docs/RUNTIME.md` | herdr, opencode, cron, tracker CLI — exact setup | you are configuring a machine |
| `docs/TRACKER.md` | tracker conventions, the operations the factory needs, GitHub and GitLab flavors | you touch issues |
| `docs/LESSONS.md` | the incidents behind the rules | a rule reads like taste |
| `docs/TOOLING.md` | CodeGraph reference detail | you use the symbol graph |
| `docs/TESTING-MAILPIT.md` | the mail rule in full | a test needs a mail to arrive |
| `docs/PIPELINES-DAGGER.md` | the Dagger v1 pipeline flavor | you write a pipeline |
| `docs/browser-grant.md` | one browser session, one holder | you run a browser walkthrough |
| `templates/` | per-repo contract, project skeleton, issue templates, config example | you create a repository or a workspace |

**Link to a rule; do not restate it.** The copy is the one that gets read, and a rule in two
places is two places to be wrong.

## Scope

- The workspace is `<workspace>`; each subdirectory is one repository, a sibling clone.
- One issue ≈ one session ≈ one mergeable change. Work happens on tickets, never on vibes.
- **Default stack:** Go + chi + GORM/SQLite + OpenAPI + Vue 3/TypeScript, built test-first. The UI
  is the shared component library; shared backend code comes from the shared Go module. A
  workspace may choose a different stack — the method in this file does not change. The full
  default skeleton is `templates/PROJECT_TEMPLATE.md`.
- Every rule in this file has an incident behind it. When one reads like taste, the story is in
  `docs/LESSONS.md`.

## Roles

The workspace is run by a **coordinator of coordinators**. The hierarchy:

- **Coordinator of coordinators** — owns the shared platform (`shared/`), the app registry
  (`apps.json`), the cross-project conventions (stack, UI library, locales, prototype intent) and
  this file.
- **Project coordinators** — one per repository; each runs the factory (workers in Herdr
  workspaces) for its own project, owns its tracker board and plan, and reports up.
- **Workers** — one issue each; never coordinate, never create panes or workspaces, never pick up
  another issue. The brief is in `FACTORY.md` § *Worker brief*.

Rules:

- Shared code lands in `shared/` and is rolled out to all projects by the coordinator of
  coordinators; a project never forks a shared component or library.
- Escalate to the coordinator of coordinators for anything touching more than one repository, any
  shared-package change, or any change to the conventions in this file.
- Merge authority: project coordinators merge into their own repository's default branch; the
  coordinator of coordinators may merge anywhere and owns `shared/`.
- **A new or complex topic is never worked at the coordinator's desk — it is a ticket, and a
  worker works it.** The coordinator captures what it already knows (the measurement that
  justifies the ticket, the constraints that bound it), files the issue, and delegates, without
  being asked to. Coordination itself stays at the desk: merges, fleet sweeps, filings. Anything a
  worker could be briefed to do is a worker's.

## Language — English only, always

**Never write Chinese. Not in code, comments, docs, commit messages, issue titles, issue bodies,
issue comments, test names, test fixtures, or a final answer.** Not a summary, not a status line,
not a suggestion to the user, and not a quoted fragment of someone else's message.

This is not a style preference; it is enforced here, and the rule is absolute:

- **The codebase, the tracker and the git history are the working language of the team that
  maintains them.** Chinese in any of them is unreadable to the people who have to maintain it.
- **User-facing copy language is a project decision** — the default template ships three locales
  and each workspace's locales are listed in `templates/PROJECT_TEMPLATE.md`. A Chinese string in a
  product that has no Chinese locale is not a translation, it is a stray token.
- The rule has been stated by the owner as a hard requirement.

### What to do when a model emits Chinese

If it appears anywhere — a worker's output, a diff, a commit, a note — **treat it as a defect,
not a curiosity**:

1. Do not merge it. Fix it before the merge, the same way a failing gate is fixed.
2. If it came from code, add a check to the ticket so it cannot come back. A comment in a language
   the team does not read is a comment nobody maintains.
3. If a worker emitted it in conversation only, that is still a failure of the brief — say so on
   the issue rather than letting it pass.

A worker brief states this explicitly, because a model will otherwise answer in the language it was
addressed in.

## Shared platform — one system of apps

The repositories of a workspace are one product family: one UI library, one design language, one
shell, one registry.

- **UI library: one component library, never two.** The default is PrimeVue 4 (Vue 3, MIT) with a
  shared theme preset in `shared/<ui>`; project-specific components build on the preset, and
  anything reusable moves into `shared/<ui>`.
- **`shared/<ui>`** — design tokens/preset, `AppShell` (topbar + app switcher + locale switcher),
  common components, i18n catalogs and helpers.
- **`shared/<go>`** — the common backend module: config, structured logging, health endpoint,
  SQLite open/migrate helpers, i18n message keys, the LLM/Jev client wrappers, identity-header
  middleware.
- **App registry: `apps.json`** lives in **`<meta-repo>`** — id, title, description, repo, port,
  URL, status, plus the shared libraries and foundations. Every app's shell reads it so the
  switcher shows the whole system. Adding an app is a registry edit, not a code change.
- **Self-contained:** every app runs with `make dev`; switching between applications is one click
  from any app's topbar. No app depends on a service that is not in this workspace.
- **Common UX:** same layout (topbar + side nav + content), same visual language, same locale
  switcher, same empty/error/loading patterns, same naming.

## Issue tracking — the tracker is the source of truth

All tasks, features, bugs, risks, decisions, progress notes and user feedback live in the
repository's tracker project. They do **not** live in markdown.

- Never add task lists, status sections, milestone tables or TODO markers about project work to
  markdown files. If it is work to do, it is an issue.
- Use the authenticated tracker CLI (or its REST API) from the repository root. SSH git access
  alone is not enough; the CLI needs API access.
- Tracker profiles, tokens and the per-operation mapping (create, search, state, comment, close,
  branch→PR) are in `docs/TRACKER.md`. **Never create a directory named `.git` anywhere it is not
  a repository** — see the incident in `docs/LESSONS.md` § *workspace traps*.
- One-time tracker setup: labels, milestones and the issue templates are created idempotently by a
  setup script — the GitLab flavor is `scripts/setup-gitlab.sh`, the GitHub flavor is
  `scripts/setup-github.sh` (per-flavor notes in `docs/TRACKER.md`).

### This repository's own gate

```bash
python3 scripts/test_fleet_watch.py           # the sweep, and the verdicts it may print
python3 scripts/test_fleet_watch_shared_sources.py  # the fetch-before-verify order, and the reuse
python3 scripts/test_fleet_keepalive.py       # the keepalive's verdicts, and its refusals
python3 scripts/test_notify_coordinator.py    # the coordinator wake-up, and its stall dimension
python3 scripts/test_merge_gate.py            # the merge gate: demo.md is in it, and NO-DEMO is a state
python3 scripts/test_cleanup_workers.py       # the reclaim sweep: repo filter, gone worktree
python3 scripts/test_close_gone_workspaces.py # the gone-workspace sweep, and its refusals
python3 scripts/test_browser_grant.py         # the browser grant: the shape, and the lock
python3 scripts/test_main_sweep.py            # the main sweep, and the verdicts it may print
python3 scripts/test_port_allocation.py       # the allocator's hold: concurrent callers get
                                              #   distinct ports, stale grants are reclaimed
python3 scripts/test_reap_port_holders.py     # the stale-test sweep, and what it refuses to reap
python3 scripts/test_factory_config.py        # the configuration seam: environment > factory.json
                                              #   > shipped defaults, and exit 2 when a config
                                              #   the operator named cannot be read
python3 scripts/test_shell_syntax.py          # every script parses — bash -n for the shell
                                              #   subjects, compile() for the python ones
python3 scripts/test_smoke.py                 # end to end: a scratch workspace with two real
                                              #   repositories — the two verdicts the coordinator
                                              #   trusts, and the partial-run record rule
python3 scripts/test_tracker.py               # the tracker adapter: both flavors' argv, and the
                                              #   refusals (missing CLI, unparsable answer)
python3 scripts/test_github_ci.py             # the GitHub CI reader: the two reds told apart,
                                              #   and the unreachable/none distinction
python3 scripts/test_glab_env.py              # the glab loader finds a credential, and a sourced
                                              #   file does not change the caller's shell
```

`test_meta_gate.py` runs this block **as written**, so a command in it that does not exist fails
instead of being believed, and every `scripts/test_*.py` that exists must be in the block (the
block is what executes it).

**Deliberately not in the block:** the monitors that read *other* repositories
(`fleet-watch.py`, `main_sweep.py`). A gate that is red for work happening in another repository
is a gate people learn to ignore. Their own **tests** are in the block, because a test that
builds its own scratch family is offline and costs nothing.
`test_gitlab_ci.py` is the GitLab flavor's test and one of its cases asserts that the *workspace's*
`.gitlab-cli/` profile resolves — an environment fact, not a repository one; run it in a
configured workspace (see `docs/TRACKER.md`).

### Every session

1. **Orient** — list open issues and pick the one that matches the request. The survey is
   `state=opened` and then read the labels; see `docs/TRACKER.md` for why.
2. **Claim** — move it to the doing label. Never replace the whole label set; add and remove
   single labels (the GitHub and GitLab commands are in `docs/TRACKER.md`).
3. **Work** — comment meaningful progress with evidence: test output, measured numbers,
   screenshots, verify-script logs. Evidence goes into the issue or its commit.
4. **Finish** — comment the outcome with evidence, then close it: put `Closes #<iid>` in the final
   commit on the default branch (the tracker closes it automatically) or close it via the CLI.
5. **Blocked** — set the blocked label and note the blocker and its issue number in a comment.
6. **New feedback from the user** — create the issue first (`Bug` or `Feature`), then act on it.
   Feedback is not a chat message; it is an issue.

### Conventions

- Commits reference issues: `refs #<iid>` while in progress, `Closes #<iid>` on completion. Use
  area-prefixed subjects (`api: …`, `store: …`, `web: …`, `worker: …`).
- One issue ≈ one session or one mergeable change. Split bigger work; link the pieces in the
  parent issue's description. Blocked-by is expressed in a comment.
- Issues are never deleted — close them with a comment.
- **Never put secrets** (tokens, API keys, credentials) in issues, commits or logs.
- Issue descriptions follow `templates/issue_templates/`: `Task`, `Bug`, `Decision`.

### Label taxonomy (scoped — one value per scope)

| Scope | Values | Meaning |
|---|---|---|
| `type::` | `feature` `bug` `task` `docs` `decision` `risk` `spike` | nature of the work; exactly one |
| `area::` | `<api>` `<store>` `<web>` `<cli>` `<worker>` `<docs>` | where it lives; extend per project |
| `status::` | `todo` `doing` `blocked` | workflow; closed = done |
| `sev::` | `high` `medium` `low` | severity, on bugs and risks only |

The factory reads `status::doing` and `status::blocked`; everything else is for humans. A tracker
whose label rules require it gets the same taxonomy spelled with `/` or `-` — the mechanism
(scoped, one value per scope) is what matters.

### Milestones

<Fill in per project, e.g. `M1 — Pilot`, `M2 — Hardening`, `Later / Upgrades`.>
Every issue belongs to exactly one milestone. Completed milestones are archived, not deleted.

## Test-driven design — the gate

**Red → green → refactor. No implementation lands without a test that failed first.**

- Go: store tests against a real temporary SQLite file (`t.TempDir()`, AutoMigrate applied);
  table-driven service tests; `httptest` for handlers. Business rules in services, handlers thin.
- Vue: Vitest + `@vue/test-utils` for components and stores; Playwright for end-to-end flows.
- **Browser validation (agent-browser):** user-facing flows are validated with the `agent-browser`
  CLI against the real running app — open the page, drive the flow, assert the result. A component
  test never substitutes for the browser check.
- **Reproducible proof (Showboat):** every feature closes with a Showboat demo document
  (`demo.md`): run the key workflow with `showboat exec`, then `showboat verify demo.md` must be
  green. That verified document is the evidence for the issue.
- Contract-first: change `api/openapi.yaml` first, then `make api`; `make api-check` must be
  green. Never hand-edit generated files.
- Validation gate before every commit and every hand-off (default stack):

  ```bash
  gofmt -l . && go vet ./... && go test ./...
  pnpm --dir web run test
  pnpm --dir web run typecheck && pnpm --dir web run build && pnpm --dir web run lint
  git diff --check
  ```

- Tests are the evidence: paste the output into the issue. If the CI is not wired, the local gate
  **is** the CI.
- **A repository carries its own contract.** This file is the default, and a repository with no
  `AGENTS.md` gives a landing worker no method at all — which is how two workers were once briefed
  to read a file that did not exist. Copy it in and specialise it (`templates/AGENTS.repo.md`).
- **Schema — the default stack's rule is GORM AutoMigrate:** the GORM models are the single source
  of truth and every app creates/updates its SQLite schema with `db.AutoMigrate(models…)` on
  `serve`. Do not add SQL migration files or a versioned migration runner. The few things
  AutoMigrate cannot express (FTS5 virtual tables, append-only triggers) are one small, explicit
  `db.Exec(…)` step right after AutoMigrate, covered by a test and named as an exception in the
  code.
- SQLite: `PRAGMA foreign_keys = ON` on every connection; WAL; UTC timestamps; RFC3339 at API
  boundaries; never destructive resets (AutoMigrate never drops columns).

## Email in a test — Mailpit, and never a stub

**Whenever a test, a verify script or a golden path needs a mail to *arrive*, it gets a real
mailbox, and the only supported one is [Mailpit](https://github.com/axllent/mailpit).** Not a
hand-written fake SMTP server, not an in-memory mailer, not a fixture that prints the token to
stdout, and not a suite that mocks SMTP because "no application logic depends on it". That last
sentence is the one to be suspicious of: it is usually true and it is why the bug is still there.

- Pinned by **digest**, like every other image; SMTP on `1025`, web UI and REST API on `8025`.
- **Empty the mailbox first, every run** — a long-lived Mailpit accumulates and "the link from the
  last message" then passes against yesterday's mail.
- `{ID}` accepts the literal `latest`; `GET /api/v1/info` is how a check asserts the **relay** was
  used rather than that an API answered.
- Mailpit is a **test fixture, never a relay**: it belongs to the test topology only, carries no
  credential, and is never configured as a real delivery target.

The full rule — the e2e shape, the chaos mechanism for failure paths, and the identity-provider
SMTP fields — is `docs/TESTING-MAILPIT.md`. Inherited by every repository that sends or receives a
mail in a test; a repository with no mail path follows the rule the day a ticket gives it one.

## CodeGraph — the symbol graph, and where it is blind

CodeGraph keeps a SQLite symbol graph per repository in `<repo>/.codegraph/`. **Reach for it before
grep**, and before reading a directory listing, when the question is *where is this*, *who calls
this*, or *what breaks if I change it*.

```sh
codegraph status <repo>            # files, nodes, edges, and the language breakdown
codegraph sync <repo>              # after edits — the index is a cache, not a source of truth
codegraph query <term> -p <repo>   # symbol search; -p is a flag, the path is not an argument
codegraph explore "<question>" -p <repo>
```

Three things to hold on to, and the rest is `docs/TOOLING.md`:

1. **A graph answer is a hypothesis; the source it prints is the evidence.** Symbols and edges go
   stale between `sync`s; the source text is re-read from disk on every call.
2. **Matching is fuzzy and confidently wrong.** Read the file paths in a result before the answer.
3. **A worktree is not indexed, and a prose repository indexes zero files.** Both are expected
   behaviour, not failures.

**Indexing is the user's decision, not the agent's.** A `.codegraph/` in a fresh clone is a
directory nobody asked for; ask before running `codegraph init`.

## Coordinator sessions — the factory

**Only the session the user explicitly designates as coordinator may use this workflow.** A worker
session never creates panes, tabs, workspaces or agents and never coordinates other sessions; it
does its assigned issue and stops. The coordinator's pane is labelled `AI · coordinator` — if
yours is not, this section does not apply to you.

The factory is: coordinator + one worker (subagent session) per issue, each in its own Herdr
workspace and git worktree; the coordinator works the unblock chain and merges results.

Five rules that carry the rest (details, mechanics and the incidents in `FACTORY.md` and
`docs/LESSONS.md`):

1. **Agent status is not a delivery signal; the gate is.** The completion signal is evidence:
   commits ahead, a clean tree, and the gate green — which is what `fleet-watch.py` reports.
2. **Parallelism is the default; prove serial.** Dispatch up to the ceiling; serialise only what
   genuinely conflicts (same file, same worktree, same exclusive resource, real dependency, or a
   shared fixture another ticket asserts on).
3. **Merge first, close second — in that order.** The merge is what makes the issue true; confirm
   it with `git merge-base --is-ancestor` against the branch you actually merged.
4. **Do not believe a guard you have not broken.** A guard is a claim about a failure mode;
   mutation-test it, and never report proof you do not have.
5. **Stop after the number of workers the user asked for.** A merge frees a slot: dispatch in the
   same turn.

**Read `FACTORY.md` end to end before the first dispatch.** It is the runbook — launching workers,
the keepalive and the coordinator wake-up, the all-clear policy, exclusive grants, the merge gate
and the reclaim sweeps.

## Repo conventions

- Direct commits to the default branch (single-developer projects). If the CI is not wired, run
  the checks locally; the coordinator verifies them before merging a worker branch.
- Go: idiomatic, `gofmt`; check every error and wrap with context; document exported symbols; keep
  HTTP handlers thin, business rules in services.
- **LLM calls (default stack):** application LLM work goes through one client library
  (`github.com/tmc/langchaingo` in the default stack) — never a second LLM client. A model wrapper
  is a shared-module concern, not a per-app one.
- **Languages:** code, commits, issues and docs stay in English. User-facing copy lives in locale
  catalogs (no hardcoded strings); API payloads and validation errors are localizable; every
  locale gets tests and an agent-browser pass.
- **UI:** one component library with the shared preset and `AppShell`. No second UI kit and no
  ad-hoc style system.
- Vue: Vue 3 Composition API with `<script setup lang="ts">`; strict TypeScript; typed API calls
  from the generated client; keep components focused and accessible; preserve responsive
  behaviour.
- Never commit OAuth, SMTP, session, CSRF or API keys; hash session tokens; enforce authorization
  on the backend for every protected operation; validate all inputs.

## Documentation boundaries

| File | Role |
|---|---|
| `README.md` | Build/run/usage instructions; status points at the tracker. |
| `PROJECT.md` | The project's design brief. No new task lists. |
| `PLAN.md` / `plan/` | Architecture, interfaces, decisions; phase files for larger efforts. |
| `demo.md` | The Showboat transcript a feature closes with. |
| `AGENTS.md` | The workflow contract. |
| `FACTORY.md` | The coordinator runbook (meta repository). |
| Tracker issues | All tasks, bugs, risks, decisions, progress notes, feedback. |

Inline code TODOs are fine for small local notes; anything actionable beyond that file goes to the
tracker as an issue.

## Definition of done

- Failing test written first, now green; the full validation gate passes.
- Evidence pasted into the issue; `git status` clean; branch merged (worktree workers) or
  `Closes #<iid>` in the final commit on the default branch.
- UI-facing changes: an agent-browser run against the running app.
- Showboat `demo.md` updated and `showboat verify demo.md` green.
- New user-facing copy exists in every locale.
- Docs touched by the change updated; no task lists leaked into markdown.
