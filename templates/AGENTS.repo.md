# The per-repository contract — what to copy, and what to change

Every member repository carries its **own** `AGENTS.md`. A landing worker session resolves the
contract from its working directory upward, and a sibling meta repository is not on that path — so
"the meta has the rules" is not enough: a worker that lands in a repository with no contract has no
method at all.

## How to create one

1. Copy the workspace's canonical `AGENTS.md` (the meta repository's) into the member repository.
2. Apply the changes below — the copy must be **about that repository**.
3. Keep it current: when the canonical contract changes, roll the change out. A contract that
   names a script or a rule that no longer exists is worse than a short one.

## The skeleton — the part that is always repository-specific

```markdown
# AGENTS.md — <repo>

This repository is <one sentence: what it is, for whom>.

- Repository: `<repo>` (workspace: `<workspace>`)
- Tracker project: <owner>/<repo>
- Tier: <P0 | P1 | P2>; milestone: <M1 …>
- Stack: <the default stack, or this repository's deviations>
- Shared modules consumed: `<shared-go>`, `<shared-ui>` (never forked)
- Area labels in use: `area::<api>`, `area::<store>`, `area::<web>`, …

## What this repository is

<Two or three sentences. What problem it solves, what it explicitly is not, and the
boundary it must not cross. This is the section a worker re-reads before scoping.>

## This repository's own gate

```bash
<the exact commands that gate a change here — and nothing in this block may not exist>
```

## Deviations from the canonical contract

<Every rule this repository tightens, relaxes, or spells differently, with the reason. If there
are none, say "none", so the reader knows it was considered.>
```

## What must survive the copy

These are load-bearing in every repository — do not drop them while specialising:

- **Red → green → refactor.** No implementation lands without a test that failed first.
- **The session workflow:** orient on open issues, claim, comment evidence, finish with
  `Closes #<iid>`, and treat feedback as an issue rather than a chat message.
- **The tracker is the source of truth**; no task lists in markdown.
- **English only — never write Chinese.**
- **Commit as you go; an uncommitted tree is not a delivery.**
- **The definition of done:** gate green, evidence in the issue, `git status` clean, branch merged.
- **Secrets never in commits, issues or logs.**
- The pointer to the meta repository's `FACTORY.md` for coordinators, and to
  `docs/LESSONS.md` for why each rule exists.

## What to change

| Placeholder | Becomes |
|---|---|
| `<repo>` | the repository name |
| `<workspace>` | the workspace root path |
| `<meta-repo>` | the meta repository's directory name |
| `<project>` | the product/project name |
| `<iid>` | the tracker's issue number for this repository |
| stack / shared-module names | this repository's actual stack, or "the default stack" |
| locales | this repository's locale set |

## How it is checked

A workspace may keep a guard that reads every repository's `AGENTS.md` on its default branch and
asserts the invariants above are present. If this workspace has one, it belongs in the meta
repository's gate block (`AGENTS.md` § *This repository's own gate*) — a guard whose tests nothing
runs is a guard nobody watches rot. (No such guard ships in this template yet; it is listed in
`docs/STATUS.md`.)
