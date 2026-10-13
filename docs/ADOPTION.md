# Adoption — standing up a workspace on this factory

This walks through going from this template to a working factory workspace: one meta repository,
a set of member repositories, a tracker, and a coordinator session dispatching workers.

**Agent-driven alternative:** `README.md` § *Bootstrap prompt* holds a copy-paste prompt a fresh
agent session can run — it fetches this repository, asks for the same decisions this document
collects, and then follows this file end to end. Keep the two in sync.

## 0. What you need

- Linux, git, python3 (3.12+), cron.
- `herdr` and `opencode`/`opencode2` (see `docs/RUNTIME.md` for versions and the model pin).
- A tracker CLI: `glab` (GitLab, the default) or `gh` (GitHub), authenticated.
- `agent-browser`, `showboat`, Docker, Node/pnpm for gates that need them.
- Optional: `codegraph` per repository.

## 1. Create the workspace

A workspace is a **container directory** — not a git repository — holding sibling clones:

```sh
mkdir -p <workspace> && cd <workspace>
git clone <this-repo> <meta-repo>          # the meta: registry, tooling, contract
git clone <repo> <repo>                    # one clone per member repository
mkdir -p shared && git clone <shared-go> shared/<go> && git clone <shared-ui> shared/<ui>
```

The name of the meta directory matters only to configuration; the reference setup calls it
`<project>-meta`.

## 2. Configure the factory

Copy the example and fill it in:

```sh
cp <meta-repo>/templates/factory.json.example <meta-repo>/factory.json
```

The fields are documented in the example. Resolution is three steps per key: the environment,
then `factory.json`, then the shipped template defaults — a workspace with no `factory.json` runs
on the documented layout. A config the operator **named** (`FACTORY_CONFIG`) that cannot be read,
or a `factory.json` that does not parse, is a hard exit 2 — never guess a workspace path or a
tracker.

At this point, also decide:

- **the tracker provider and owner** (`glab`/GitLab by default, or `gh`/GitHub);
- **the branch prefix** for worker branches (`issue-` is the convention the tooling parses);
- **the worker model pin** in `<workspace>/opencode.json` (`docs/RUNTIME.md` § *The model pin*).

## 3. Specialise the contracts

1. Copy `AGENTS.md` to each member repository and specialise the `<placeholders>` — the contract
   must be about *that* repository. `templates/AGENTS.repo.md` lists what changes and what must
   survive the copy.
2. Fill in `META-PROJECT.md` for this workspace: the system at a glance, the keystone entity, the
   authorization model, the decisions register.
3. Copy `templates/PROJECT_TEMPLATE.md` into each member as its `PROJECT.md` and fill the
   placeholders; adjust the stack if this workspace is not the default Go + Vue stack.

## 4. Set up the tracker

- Create the repositories (GitLab: remember the subgroup-token trap in `docs/TRACKER.md` § *The
  traps*).
- Run the setup script to create labels, milestones and issue templates idempotently — the GitLab
  flavor is `scripts/setup-gitlab.sh` (group-level), the GitHub flavor is
  `scripts/setup-github.sh -R owner/repo` (one repository at a time); see `docs/TRACKER.md`.
- Create the first issues **in the member repositories**, not in the meta: one issue ≈ one session ≈
  one mergeable change.

## 5. Arm the watchdogs

Add the two cron entries from `docs/RUNTIME.md` — the keepalive + coordinator wake-up every 30
minutes, and the main sweep hourly. They are backstops, not the trigger: a nudge that is needed in
order to act is a failure of the coordinator, not a feature of the cron.

## 6. The first coordinator session

1. Start an opencode session in the meta repository, label it `AI · coordinator`, export
   `HERDR_ENV=1`.
2. **Read `FACTORY.md` end to end before dispatching anything.**
3. Confirm the model pin is what you think it is: launch one worker and read its status line.
4. Dispatch one worker. Prompt — do not block — then arm `fleet-watch.py --watch` as a background
   command and return to managing the fleet.
5. Merge with the merge gate (`scripts/merge-gate.py`), confirm the ancestor check, then close the
   issue and **dispatch the replacement in the same turn**.

## 7. The rules that bite in the first week

Short list; each has a story in `docs/LESSONS.md`.

- **Agent status is not a delivery signal.** Commits ahead + clean tree + gate green is.
- **An uncommitted tree is indistinguishable from no work.** Put "commit as you go" in every brief.
- **A merge frees a slot.** Dispatch the next worker in the same turn as the merge.
- **Merge first, close second.** Confirm with `git merge-base --is-ancestor` against the branch you
  actually merged.
- **Do not believe a guard you have not broken.** Mutation-test it or say you did not.
- **One worker per migration per repository.** Disjoint subject lines are not disjoint files.
- **One session name per browser run.** The grant is additive; the lock is what refuses.
- **Count open issues, not one label's issues**, and remember `-A` includes closed ones.
- **Never create a directory named `.git`** outside a repository.
- **Never write Chinese.** The rule is absolute and the instruction is in the worker brief.
