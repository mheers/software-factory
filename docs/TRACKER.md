# The tracker — conventions, both flavors

The factory needs one tracker, and it needs only a small set of operations from it. This document
is the contract: what the factory assumes, what the traps are, and how the two supported flavors
(GitHub and GitLab) satisfy it. **The rules are flavor-neutral; only the commands change.**

## What the factory needs

| Operation | Used by | What it must do |
|---|---|---|
| `issue_list(state=open)` | every session; the coordinator survey | open issues only, paginated |
| `issue_view(iid)` | workers | body, labels, milestone, comments |
| `issue_create(title, body, labels)` | the main sweep (a red `main`) | returns the new iid |
| `issue_search(query)` | the main sweep's dedupe | find an existing issue by a commit key **before** creating another |
| `issue_state(iid)` | the reclaim sweep | closed or not |
| `issue_comment(iid, body)` | workers, keepalive | append a note |
| `issue_close(iid)` | the coordinator | close with a comment |
| `label_add` / `label_remove` | claim, block, unblock | **add or remove one label** |
| `pr_for_branch(repo, branch)` | the reclaim sweep | is there an open PR/MR for this branch |

The factory reads `status::doing` and `status::blocked`; everything else is for humans. **Exit
code 2 means "cannot tell" and is never a pass** — that convention holds everywhere in the tooling.

## Session workflow (the commands)

**Claim** — move the issue to doing. Never replace the whole label set; add and remove single
labels:

```bash
# GitHub
gh issue edit <iid> --add-label status::doing --remove-label status::todo
# GitLab
glab api "projects/:id/issues/<iid>" --method PUT \
  -f add_labels=status::doing -f remove_labels=status::todo
```

**Progress** — a comment with evidence (test output, measured numbers, a log):

```bash
gh issue comment <iid> --body "…"
glab issue note <iid> -m "…"
```

**Finish** — `Closes #<iid>` in the final commit on the default branch closes the issue
automatically on both trackers; otherwise close it explicitly (`gh issue close <iid>` /
`glab issue close <iid>`).

**Blocked** — set `status::blocked` and name the blocker in a comment: `Blocked by #<other>: …`.
Neither GitHub nor GitLab Free has a usable "blocked by" link type; the comment is the mechanism.

**Survey** — the coordinator's backlog survey is **open** issues, and then group by the `status::`
label:

```bash
gh issue list --state open --limit 200 --json number,title,labels,milestone
glab api "projects/:id/issues?state=opened&per_page=100"
```

## Labels and milestones

The taxonomy (`AGENTS.md` § *Label taxonomy*) is scoped — exactly one value per scope:

- `type::` feature, bug, task, docs, decision, risk, spike
- `area::` api, store, web, cli, worker, docs (extend per project)
- `status::` todo, doing, blocked
- `sev::` high, medium, low (bugs and risks only)

The setup scripts create the labels, the milestones and the issue templates **idempotently**: the
GitLab flavor is `scripts/setup-gitlab.sh` (group-level, can seed an initial backlog), the GitHub
flavor is `scripts/setup-github.sh [-R owner/repo]` (one repository at a time; GitHub has no
group-level labels).

Every issue belongs to exactly one milestone; completed milestones are archived, not deleted.
Status lives in labels; a milestone is a delivery boundary, not a state.

## The traps — learned, and flavor-neutral in spirit

1. **Never update labels by replacing the set.** `glab issue update --label` replaces every label
   the issue carries; the GitHub analogue is `gh issue edit --add-label/--remove-label` only. A
   replace silently drops `status::` and `type::` labels, and the loss is invisible.
2. **A listing that includes closed issues is not a backlog.** `glab issue list -A` (and
   `gh issue list --state all`) counts *closed* work as work to do — measured once at 39 items when
   13 were open. The survey is `state=opened` / `--state open`.
3. **Filtering by one label hides the others.** `--label status::todo` hides `doing` and `blocked`,
   which is most of a live fleet — it once reported a near-empty backlog while 25 items were open.
   Count open first, then read labels.
4. **Deduplicate a red-main issue before creating it.** The main sweep searches for the commit key
   first (an existing issue for the same red), because a sweep that files a new issue every run is
   a sweep that gets muted. `Closes #N` works on both trackers and is the close mechanism.
5. **A subgroup-scoped GitLab token cannot create a project via `POST /groups/:id/projects`** — it
   answers `404`. Create with `POST /projects` and an explicit `namespace_id`. Group-level reads
   above the token's scope answer `403 insufficient_granular_scope`; that is the token, not a broken
   profile.
6. **Tokens and profiles live outside every repository** (a workspace-level directory, `0600`,
   git-ignored). A token pasted into a conversation has been disclosed — rotate it. Never commit
   one, and never point a config directory at a path named `.git` — see `docs/LESSONS.md` §
   *workspace traps*.

## GitHub flavor (`gh`)

- Auth: `gh auth status`; a fine-grained or classic PAT with `repo` scope, or the interactive
  login. The factory itself only needs issue and PR operations plus `gh run list` for CI.
- Issue operations: `gh issue list/view/create/comment/close/edit`, and
  `gh issue list --search "<query>"` for dedupe.
- PR by branch: `gh pr list --head <branch> --state open`.
- CI state: `gh run list --branch main --limit 1 --json status,conclusion,headSha`.

## GitLab flavor (`glab`, the default)

- Auth: a workspace-local profile directory (`GLAB_CONFIG_DIR`) holding a **project- or
  group-scoped** token; `scripts/glab-env.sh` loads it by walking up from its own location, and it
  **refuses rather than exporting an empty credential**. The token and the binary are independent
  facts: the script exports the token even when `glab` is not on `PATH`, because under cron `PATH`
  is minimal and a caller that needs the binary checks for itself.
- Issue operations: `glab issue list/view/create/note/close`, `glab api` for anything the CLI does
  not expose (label changes, filtered lists).
- MR by branch: `glab mr list --source-branch <branch>`.
- CI state: `scripts/gitlab_ci.py` reads the pipeline and classifies `RED` vs `BLOCKED` vs
  `UNKNOWN` — a job that never started (`ci_quota_exceeded`, no runner, no minutes) is **BLOCKED,
  not a code defect**, and the two must never be collapsed into one verdict.

## The tracker adapter

`scripts/tracker.py` is the single adapter, selected by `factory_config.tracker_provider()`:
`gitlab` (`glab`, the shipped default) or `github` (`gh`). Its operations — `open_issues` /
`file_issue` / `issue_state` / `mr_count`, plus the `state` and `mr-count` CLI the shell side
reads — are the interface above. Refusals are explicit: a missing CLI or an unparsable answer is
`unknown` and the CLI exits 2; *cannot tell* is never a verdict. Comment and close are deliberately
absent — no shipped script calls them, and workers comment and close through the tracker CLI in
their brief.
