# CodeGraph — the symbol graph, and where it is blind

**The workspace's main checkouts are indexed; issue worktrees are deliberately
not.** CodeGraph keeps a SQLite symbol graph per repository in
`<repo>/.codegraph/` — about a second to build, 0.2–10 MB, 800–2,400 nodes and
2,000–9,000 edges each.

**Reach for it before grep**, and before reading a directory listing, when the
question is *where is this*, *who calls this*, or *what breaks if I change it*.

```sh
codegraph status <repo>            # files, nodes, edges, and the language breakdown
codegraph sync <repo>              # after edits — the index is a cache, not a source of truth
codegraph query <term> -p <repo>   # symbol search; -p is a flag, the path is not an argument
codegraph explore "<question>" -p <repo>
codegraph uninit -f <repo>         # drop the index
```

## Six things nobody will tell you the second time

1. **The MCP tool has no default project.** The workspace root is a container
   of sibling clones and holds no `.codegraph/`, so `codegraph_explore`
   **fails** without `projectPath` rather than guessing. It resolves the
   nearest `.codegraph/` at or above the path you pass, and there is no
   setting that makes one project the default — so pass the path on every
   call.

2. **A graph answer is a hypothesis; the source it prints is the evidence.**
   The symbol set and the edges come from the index and go stale between
   `sync`s, so a symbol you added five minutes ago does not exist as far as
   the graph is concerned. The source text is re-read from disk on every
   call, so what you are shown *is* current — but only for files the index
   already knew about. `sync` after a change, before you believe a "nothing
   calls this".

3. **Matching is fuzzy and confidently wrong.** Asked about an authorization concept in one of the
   shared modules, it returned a *branding* policy file instead. Read the
   file paths in the result before you read the answer.

4. **Rego is not indexed, and this is the gap that matters.** A Rego policy
   file **is the platform's authorization model**, and a
   development copy of it ships elsewhere in the workspace. Neither is in
   the graph. A graph can show you the Go that *asks* the policy; it can
   never tell you what a role may do. **Read the Rego policy files directly
   for any authorization question.** Markdown, shell and SQL are not indexed
   either, so every `Makefile`, `scripts/*.sh` and `README.md` in the family
   is invisible to it — which is where the gate and the deployment wiring
   actually live.

5. **A worktree is not indexed.** Workers run in `<repo>-<iid>` siblings,
   which carry no `.codegraph/`, so a query rooted there resolves nothing
   and you fall back to Read/Grep. That is correct behaviour, not a
   breakage. Index a worktree only when a branch is doing real work
   (`codegraph init .` inside it) — and remember it goes stale on every
   rebase and every merge.

6. **A prose repository indexes zero files, and that is not a failure.** A
   roadmap repository is markdown and a meta repository holds one Python
   script; `codegraph status` on either reports `0` or `1` file and *is up
   to date*. An empty index and a broken index look identical from outside,
   so read `status` before concluding that anything failed.

`.gitignore` is honoured, so a large gitignored
`web/node_modules` costs nothing — two repositories carrying one indexed
119 and 110 files respectively.

## Which repositories are indexed

**The workspace's main checkouts are indexed; issue worktrees are deliberately
not** — see 5.

Indexing is the user's decision, not the agent's. The main checkouts are
indexed, so use them; another repository is one `codegraph init` away, but ask
first, because a `.codegraph/` in a fresh clone is a directory nobody asked
for and 70 MB the family did not need.

**CodeGraph v1.6.2 is available; a server running v1.5.0 keeps serving
v1.5.0 until upgraded.**
