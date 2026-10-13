---
project: <project-slug>
title: <Project Title>
status: draft
created: <YYYY-MM-DD>
origin: <who asked, why>
audience: the next AI agent or engineer picking this up
model_under_test: <TypeSafe / Jev model, if the project uses one>
data_classes: <the data classes this app owns — declared in META-PROJECT.md and apps.json>
priority: <P0 | P1 | P2>
---

# <Project Title> — Project Template

Copy this file to `PROJECT.md` in the new repository, keep the front matter and fill every
`<placeholder>`. The prose around the placeholders is the default this stack assumes.

**One line:** <what this system does, for whom, in one sentence.>

**Status:** <draft | pilot | production>. Treat every number that is not marked *measured* as a
plan, not a result.

## Where the rules live

| Rule set | File |
|---|---|
| Cross-system design — identity, the authorization model, the agent surface, hosting and data protection, the decisions | `<meta-repo>/META-PROJECT.md` |
| Workflow, labels, milestones, the validation gate, the definition of done | `<meta-repo>/AGENTS.md` (copy it into the repo root and specialise it) |
| The factory — worktrees, sessions, ports, the coordinator roles | `<meta-repo>/FACTORY.md` |
| The app registry — ids, ports, status, data classes | `<meta-repo>/apps.json` |
| This skeleton | `<meta-repo>/templates/PROJECT_TEMPLATE.md` |

**Link to a cross-system rule; do not restate it.** Every copy of a rule is another place to be
wrong, and the copy is the one that gets read. If a rule here contradicts `META-PROJECT.md` or
`AGENTS.md`, those two win and the fix is an issue.

## Project intent — customer-facing golden prototype

This is a customer-facing golden prototype: a working, clickable end-to-end flow with realistic
**synthetic** data wherever the real data is missing. More than a PoC, deliberately less than
the full product — **depth on the golden path beats breadth**, and no dead ends or placeholder
screens on it. `demo.md` (Showboat) plus an agent-browser walkthrough are what the customer
sees, and both must work from a clean checkout.

**This template makes identity P0, not deferred.** `<identity-service>` (an identity provider,
a policy engine, extauthz and oauth2proxy) gates every app, so there is no "no auth in M1"
phase. That is the default this stack assumes: a data class that needs authorization cannot be
demoed on a trusted network, and skipping it is not an acceptable prototype shortcut.
**Synthetic data is acceptable — skipped authorization is not.** A project that departs from
this default records the decision instead of dropping the gate silently.

**This template carries placeholders, and so does a new repo built from it.** A project's
names, entities, customers, sponsors, colours, legal entity and locale set are filled in from
that project's own configuration and seed fixtures — never written into this template, into
`<shared-ui>`, or into another project's fixtures. A project may have a design partner; the
code is not about that partner.

## Stack

- **Go 1.25+** — `chi` v5 router, `Cobra` CLI, `GORM` + **SQLite**
- **OpenAPI 3** contract with generated Go server stubs and TypeScript client types
- **Vue 3 + TypeScript** — Vite, Pinia, Vue Router, strict `vue-tsc`
- **Vitest** (+ `@vue/test-utils`) for frontend units, **Playwright** for e2e
- **One Docker container** serving the API and the embedded frontend (`go:embed` of `web/dist`)
- **Jev / TypeSafe judgments** — `github.com/mheers/typesafeai-systemone-jev-go` via the
  `jev` package in `<shared-go>` (the only Jev client)
- **LLM calls** — `github.com/tmc/langchaingo` via the `llm` package in `<shared-go>`,
  reaching a local Ollama through `llms/ollama` (the only LLM client)
- **Identity** — `<identity-service>` (an identity provider + a policy engine + extauthz).
  Access is decided by the policy engine at the gateway; **the app asserts facts, it does not
  decide access** (`META-PROJECT.md`)
- **PrimeVue 4** — the shared UI kit, themed by `<shared-ui>` (never a second library)
- **Shared platform** — `<shared-ui>` (frontend) and `<shared-go>` (backend), each its own
  repository in this workspace
- **Browser validation** — `agent-browser` CLI against the real running app
- **Reproducible proof** — Showboat executable demo documents (`demo.md`, `showboat verify`)

Rules of thumb:

- Jev is always called through the Go library; no Python sidecar, no hand-rolled HTTP client, no
  direct REST call to TypeSafe from application code.
- LLM work that is not a Jev judgment (extraction, summarising, translation) uses langchaingo —
  as an LLM client, not as a framework: extract, chunk and validate ourselves.
- Every user-facing surface ships in every locale the project declares: `<default-locale>`
  (the default), `<second-locale>` and `<third-locale>`. Copy lives in i18n catalogs, never inline in components
  or handlers, and every locale gets a test and an agent-browser pass. **The locale set is a
  project decision, not a default** — record it and do not change it without a recorded decision.
- SQLite is the only database. One file, WAL mode, `PRAGMA foreign_keys = ON` on **every**
  connection.
- **The SQLite driver is pure Go** — `github.com/ncruces/go-sqlite3` or `modernc.org/sqlite`,
  with `sqlite-vec` through the `/ncruces` bindings — so every app is one static binary with no C
  toolchain in CI. The driver and the FTS5/`sqlite-vec` bootstrap live in `<shared-go>/db`;
  **an app does not import a SQLite driver itself**, and `gorm.io/driver/sqlite`'s CGO build is
  not acceptable here.
- No second language runtime on the server. If it can be Go, it is Go.
- The frontend is a static SPA embedded in the binary; there is no separate frontend deployment.
- Everything runs in one hosting jurisdiction, and the regulated data classes this project
  declares never leave it. Cross-border handling is a cross-system decision, not an app-level
  one (`META-PROJECT.md`).

## Shared platform

This project is one app in a family of sibling apps. The authoritative app list, with id,
title, port, status and data classes, is `<meta-repo>/apps.json` — **adding an app is a registry
edit**, so read the registry instead of copying a list that will rot.

- `<shared-ui>` — the shared frontend package: the PrimeVue preset, the app shell with the app
  switcher and locale switcher, common components, the locale catalogs and the typed API client.
  Consume it as a local package; never copy a component out of it.
- `<shared-go>` — the common Go module (config, logging, health, db, i18n, jev, llm, authz,
  api), consumed through a committed `replace` while developing and as a tagged version
  everywhere else.
- `<upstream-adapter>` — a fork of an upstream data client, where a project needs one:
  read-only and one-way, reached only through the fork, and nothing here writes to the upstream
  source.

**A project never forks a shared component.** Changes to `<shared-ui>` or `<shared-go>` land
there and are rolled out by the coordinator of coordinators; escalate rather than editing around
a shared interface.

## Repository layout

The workspace is a **container of sibling clones**, not a repository itself:

```text
<workspace>/
├── <meta-repo>/                   # the meta repository: AGENTS.md, FACTORY.md,
│                                  # META-PROJECT.md, apps.json, templates/, scripts/
├── <project>/                     # one repository per app, one sibling clone each
├── <project>-issue-<iid>/         # a git worktree, always a sibling — see below
└── shared/
    ├── <shared-go>/               # its own repository
    └── <shared-ui>/               # its own repository
```

Inside one app:

```text
<repo>/
├── api/
│   └── openapi.yaml          # single source of truth for the HTTP API
├── cmd/<project>/            # Cobra entrypoints (serve, seed, admin, import, …)
├── internal/
│   ├── api/                  # generated stubs + thin HTTP handlers
│   ├── <domain>/             # one package per domain area (business rules live here)
│   ├── store/                # GORM models, AutoMigrate, queries
│   └── web/                  # go:embed of web/dist
├── web/
│   ├── src/                  # Vue 3 SPA (components, views, stores, api client)
│   ├── e2e/                  # Playwright specs
│   └── package.json          # "<shared-ui>": "file:../../shared/<shared-ui>"
├── scripts/                  # verify-*.sh, seed helpers (the tracker setup script does not
│                              # live here — see Starting a new project)
├── plan/                     # phase files for larger efforts (optional)
├── Makefile                  # every workflow is a target
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── AGENTS.md                 # agent workflow (copy of <meta-repo>/AGENTS.md, specialised)
├── PLAN.md                   # architecture, interfaces, decisions
├── PROJECT.md                # the design brief (this template is its skeleton)
└── README.md                 # build / run / usage
```

**The relative shared paths are the reason worktrees are siblings.** They are what make the
workspace a container of clones:

```go
// go.mod — the module path is the repository path, so a tagged version needs no replace
replace <shared-go-module> => ../shared/<shared-go>
```

A worktree under `~/.herdr/worktrees/<repo>/<branch>` breaks `../shared/<shared-go>` and
`file:../../shared/<shared-ui>`. Create it as a sibling instead, and never add a `go.work`
pointing outside the worktree (`FACTORY.md`).

## Contract-first API

`api/openapi.yaml` is the only API source of truth.

1. Change OpenAPI first.
2. `make api-go` then `make api-ts` (or `make api`).
3. `make api-check` must be green — it diffs the generated artifacts against the contract.
4. Never hand-edit generated files (`internal/api/gen/`, `web/src/api/schema.ts`).
5. Keep the compile-time assertion in the concrete handler package:

   ```go
   var _ gen.ServerInterface = (*Handler)(nil)
   ```

6. Do not add a route that is absent from OpenAPI.
7. Every operation declares its **data class** (`x-data-class`, matching the front matter and
   `<meta-repo>/apps.json`); an operation that cannot be placed in a class does not belong in
   the API yet.

## Data layer rules

- **GORM AutoMigrate is the schema mechanism:** the models are the single source of truth
  and the binary calls `db.AutoMigrate(models…)` on `serve` (and in store tests) — no
  hand-written SQL migration files, no versioned migration runner. The few things
  AutoMigrate cannot express (FTS5 virtual tables, `sqlite-vec` virtual tables,
  append-only triggers) are one small, explicit `db.Exec(…)` step right after AutoMigrate,
  covered by a test and named as an exception in the code.
- `PRAGMA foreign_keys = ON` for every SQLite connection.
- UTC timestamps everywhere; RFC3339 at API boundaries.
- Never use destructive database reset commands. To prove a fix against real data, copy the
  database and run a temporary, env-guarded test against the copy.
- Soft deletes are explicit (`deleted_at IS NULL` filters), so "empty list" never means "row
  does not exist" — check the raw row before believing a validation error.

## Test-driven design

The loop for every change: **red → green → refactor**. No implementation lands without a test
that failed first.

- **Go store layer** — tests run against a real temporary SQLite file (`t.TempDir()`), with
  AutoMigrate applied. No mocked database.
- **Go services** — table-driven tests; business rules in services, HTTP handlers stay thin.
- **Go handlers** — `httptest` against the generated router; assert status codes and payloads.
- **Vue components and stores** — Vitest + `@vue/test-utils`; test behaviour, not internals.
- **End-to-end flows** — Playwright against a real server + seeded database.
- **Browser validation** — drive user-facing flows with the `agent-browser` CLI against the
  real running app; a component test never substitutes for the browser check.
- **Reproducible proof** — a Showboat `demo.md`: `showboat exec` runs the key workflow,
  `showboat verify demo.md` proves it still reproduces.
- **Authorized flows** — a user-facing flow is validated *through* `<identity-service>`, not
  around it. A test that bypasses the gateway proves nothing about the thing that must not be
  bypassed.

Tests are the evidence. A task is not done until the test output is pasted into its issue.

## Commands

```bash
cp .env.example .env
make dev                      # seed + backend (<port from <meta-repo>/apps.json>) +
                              # Vite (:5173, proxies /api)
make test                     # go test ./... + frontend unit tests
make lint                     # gofmt check, go vet, eslint, prettier check
make build                    # frontend build + Go binary (embedded SPA)
make api                      # regenerate Go + TS from api/openapi.yaml
```

One app, one command: `make dev` starts this app only. There is no script that starts the
family — the apps are independent containers, and the shell's app switcher is what ties them
together.

## Validation gate

Run before every commit and before handing work back:

```bash
gofmt -l .                    # must print nothing
go vet ./...
go test ./...
pnpm --dir web run test
pnpm --dir web run typecheck && pnpm --dir web run build && pnpm --dir web run lint
git diff --check
```

For UI-facing work, add the agent-browser flow against the running app, and finish with
`showboat verify demo.md` — the verified demo is the hand-off evidence.

If CI is not wired, **the local gate is the CI** — run it yourself and paste the
evidence into the issue.

## Deployment

- One container, `/app/data` writable and mounted persistently (SQLite database, media, caches).
- **Never publish an app port.** Requests arrive through the gateway (oauth2proxy + extauthz),
  which authenticates and sets the identity headers the app reads through `<shared-go>/authz`;
  a direct backend port bypasses that authorization entirely. Local `make dev` binds loopback
  only.
- The app trusts **only** the gateway's identity headers, resolved through the one shared
  precedence function — a client-supplied identity header must never decide access.
- TLS and reverse proxy in front of the container; secrets (OAuth, SMTP, session, CSRF, API
  keys, identity-service credentials) live outside the repository.
- The runtime validates production configuration before serving traffic.
- Backups: use SQLite's backup API or stop the container before copying the database, and copy
  the data directory in the same backup.

## Starting a new project

1. Create the repository in the workspace's tracker and clone it into `<workspace>/<repo>`.
   Tracker access and token limits are covered in `<meta-repo>/docs/TRACKER.md`.
2. Copy this template to `PROJECT.md` in the repo root (keep the front matter, fill the
   placeholders) and copy `<meta-repo>/AGENTS.md` to the repo root and specialise it.
   `README.md` stays build/run/usage; the design brief is `PROJECT.md`.
3. Register the app in `<meta-repo>/apps.json` — id, title, description, repo, port, status,
   data classes. That edit is what makes it appear in the app switcher. The tracker setup
   (labels, milestones, issue templates) is a one-off, run once by the coordinator of
   coordinators (`scripts/setup-gitlab.sh` / `scripts/setup-github.sh`), not per app.
4. Write the `go.mod` `replace` for `../shared/<shared-go>` and the `<shared-ui>` local package
   dependency before the first `go build`, and check that `make dev` works before any feature
   work starts.
5. Create `api/openapi.yaml` with `GET /api/health` as the first contract; wire `make api`.
6. Create `cmd/<project>` with `serve` and `seed` commands.
7. Write the first failing test (health endpoint) and make it pass — the TDD loop starts on day
   one.
8. Put the app behind `<identity-service>` and add a test that an unauthenticated request is
   rejected — authorization belongs in the first milestone, not a later one.
9. Add `Dockerfile` + `docker-compose.yml` and prove `make build` produces one working container.

## Documentation boundaries

| File | Role |
|---|---|
| `README.md` | Build / run / usage. Status points at the tracker. |
| `PROJECT.md` | The design brief: problem, evidence, scope, libraries with versions and licences, known pitfalls, decisions to settle. |
| `PLAN.md` | Architecture, interfaces, decisions. No new task lists. |
| `plan/` | Phase files for a larger effort, executed in order. |
| `AGENTS.md` | Agent and coordinator workflow. |
| `OPERATIONS.md` | Deployment, backup/restore, provider checklists (when needed). |
| `demo.md` | Showboat executable demo — reproducible proof of the current feature. |
| `pitch/` | A customer-facing pitch — **only once there is a real customer conversation to support it.** Never invent a customer, a number or a benefit case. |
| Tracker issues | All tasks, bugs, risks, decisions, progress notes, feedback. |

Never add task lists, status sections, milestone tables or TODO markers about project work to
markdown. If it is work to do, it is an issue.

## Definition of done

- The failing test exists and now passes; the full validation gate is green.
- The change is committed with `Closes #<iid>` (or the issue is closed with an evidence
  comment), and `git status` is clean.
- The issue carries the evidence: test output, measured numbers, screenshots.
- UI-facing changes carry an agent-browser run; `demo.md` is updated and
  `showboat verify demo.md` is green.
- The golden path works from a clean checkout with seeded **synthetic** data **and** an
  authenticated session — synthetic data is fine, missing authorization is not.
- Every endpoint declares its data class.
- The app uses `<shared-ui>` (PrimeVue preset + app shell) and is listed in
  `<meta-repo>/apps.json`.
- New user-facing copy exists in all three locales (`<default-locale>` default,
  `<second-locale>`, `<third-locale>`), and a change to the locale set is a recorded decision.
- Documentation touched by the change is updated (README, PROJECT.md, PLAN.md, OpenAPI).
