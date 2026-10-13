# software factory — a template

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-mheers%2Fsoftware--factory-blue.svg)](https://github.com/mheers/software-factory)

A **software factory** is a way of running a workspace of repositories with AI agents:

- one **meta repository** (this one) carries the registry, the factory tooling, the contract and
  the runbook;
- every product repository is a **sibling clone** with its own issues, its own gate and its own
  copy of the contract;
- a **coordinator** session dispatches **one worker per issue**, each in its own git worktree and
  Herdr workspace, and merges the results.

This repository is the generic template for such a workspace: the factory itself, its tooling and
its templates — the product decisions are left for the workspace you build from it.

## What is in here

| Path | What it is |
|---|---|
| `AGENTS.md` | the workspace contract — the file every session reads |
| `FACTORY.md` | the coordinator runbook — mechanics, watchdogs, merge discipline |
| `META-PROJECT.md` | the cross-system design skeleton of a workspace (fill it in) |
| `docs/ADOPTION.md` | how to stand up a workspace on this factory |
| `docs/RUNTIME.md` | herdr, opencode, cron, tracker CLIs — exact setup |
| `docs/TRACKER.md` | tracker conventions, GitHub and GitLab flavors |
| `docs/LESSONS.md` | the incidents behind the rules |
| `docs/TESTING-MAILPIT.md`, `docs/PIPELINES-DAGGER.md`, `docs/TOOLING.md`, `docs/browser-grant.md` | deep dives the contract points at |
| `docs/STATUS.md` | what is genericized, what is not yet |
| `scripts/` | the factory tooling (see `scripts/README.md`) |
| `templates/` | per-repo contract, project skeleton, issue templates, config example |
| `.fleet-state/` | fleet state; only `expected-ci.json` is committed, by design |

## Bootstrap prompt — paste this into a fresh agent

A fresh agent session can stand the whole workspace up for you. Copy everything inside the block
below into the new session (start it in the empty directory that should become the workspace, or
name the path when it asks). The agent fetches this repository, reads the instructions, asks you
for the decisions it must not invent — workspace layout, tracker, **tech stack**, identity and
locales — and then follows `docs/ADOPTION.md` end to end.

```text
Bootstrap a software factory workspace from the template repository
`https://github.com/mheers/software-factory`. Clone it the way your GitHub access allows
(`gh repo clone`, or `git clone` with credentials — the owner controls who can read it).

1. Fetch the template before doing anything else. If I have not told you where the workspace
   should live, ask me first, then clone it as the workspace's meta repository:
     git clone https://github.com/mheers/software-factory.git <workspace>/<meta-repo>
   Do not improvise a factory from memory — the repository is the only source of truth.

2. Read, in this order: AGENTS.md, FACTORY.md, docs/ADOPTION.md, docs/RUNTIME.md,
   docs/TRACKER.md. Treat AGENTS.md as binding.

3. Then, and before creating anything, collect the workspace decisions from me with your
   structured question tool (ask/question), one decision per question, your recommendation
   first and what each option costs:
     a. the workspace root directory and the meta repository's name
     b. what the product is, in one sentence — and the project's name
     c. the tracker: GitHub or GitLab, the owner/namespace, and whether the repositories
        exist yet (account for creating them)
     d. the tech stack: keep the default (Go + chi + GORM/SQLite + OpenAPI + Vue 3/PrimeVue)
        or change it — and if it changes, which parts of templates/PROJECT_TEMPLATE.md and
        AGENTS.md must change with it
     e. identity and authorization: P0 now or deferred, and with which provider
     f. the locale set (the default template ships three)
     g. the shared modules: their names, or "not yet" if there are none
     h. the worker model pin and the fleet ceiling — the defaults are fine, confirm them
   Do not invent an answer to any of these. If I say "you decide", propose one option with the
   reason and continue.

4. Then stand the workspace up by following docs/ADOPTION.md end to end: factory.json,
   META-PROJECT.md filled in, the specialised AGENTS.md in every member repository, the
   tracker's labels/milestones/templates, the cron entries, and the first issues.

5. Ask before every action that touches the remote or the machine outside the workspace:
   creating remote repositories, pushing, installing cron entries.

6. Finish with a short report: what was created, what was asked and answered, and what
   remains to be decided — as issues, not as prose.
```

Keep the prompt up to date with `docs/ADOPTION.md`: the two are the same procedure, one for an
agent and one for a person.

## Quickstart

1. **Read `docs/ADOPTION.md`.** It walks through requirements, the workspace layout, the tracker
   setup, and the first coordinator session.
2. Stand up a workspace: a directory of sibling clones plus this repository as the **meta**
   (`<workspace>/<meta-repo>/`).
3. Copy `templates/factory.json.example` to `factory.json` at the meta root and fill it in;
   copy `templates/AGENTS.repo.md` into each repository as its `AGENTS.md` and specialise it.
4. Point the model pin in the workspace's `opencode.json` at the worker model you want.
5. Start the coordinator session, read `FACTORY.md` end to end, and dispatch the first worker.

## Runtime requirements

Linux, git, python3, a tracker CLI (`glab` by default, or `gh`), `herdr`, `opencode2`, cron, `agent-browser`,
`showboat`, and Docker for the images a gate may need. The exact setup — including the cron entries
for the keepalive and the coordinator wake-up — is `docs/RUNTIME.md`.

## Using this repository as a template

- The workspace contract is `AGENTS.md`; a member repository copies it in and specialises the
  `<placeholders>` (`templates/AGENTS.repo.md` says what to change).
- The project skeleton is `templates/PROJECT_TEMPLATE.md` — the default stack is Go + chi +
  GORM/SQLite + OpenAPI + Vue 3, kept as a template, not a requirement.
- The issue templates are `templates/issue_templates/` (`Task`, `Bug`, `Decision`).
- Nothing in this repository names a client, a customer, or a product. Keep it that way: a factory
  that carries one product's specifics is a factory nobody can reuse.

## The factory's own gate

The block in `AGENTS.md` § *This repository's own gate* is the only thing that executes the
factory's tests, and `scripts/test_meta_gate.py` runs that block as written. Run it after any
change to `scripts/`.

## Script status

The factory tooling is extracted from a working implementation and sanitized of its names and
paths. It assumes the factory's layout — a meta repo beside member repositories, the `shared/`
convention, a tracker — with the constants resolved through `factory_config.py` (environment →
`factory.json` → shipped defaults) and tracker/CI reads through provider-selected flavors (`glab`
by default, `gh` kept). `scripts/README.md` lists each script with its status; `docs/STATUS.md`
carries the remaining work. Read them before wiring a script into a new workspace.

## License

MIT — see [`LICENSE`](LICENSE). Contact: marcel@heers.it.
