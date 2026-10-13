# FACTORY.md — the coordinator runbook

**Read end to end before the first dispatch.** `AGENTS.md` is the contract for every session; this
file is the coordinator's mechanics — launching workers, watching them, keeping the fleet honest,
merging and reclaiming. Every rule here has an incident behind it; the stories are in
`docs/LESSONS.md`, referenced per section.

Only the session the user explicitly designates as coordinator uses this file. A worker never
coordinates.

## What the factory is

Coordinator + one worker (subagent session) per issue, each worker in its own Herdr workspace and
git worktree; the coordinator works the unblock chain and merges results. Requires `HERDR_ENV=1`;
read the Herdr skill via `herdr --skill` once per session if you have not used it before. Runtime
setup is `docs/RUNTIME.md`.

## Launch one worker

**Default: one Herdr workspace per worker, created together with its git worktree.** Workers never
share the coordinator's tab or workspace — each gets its own labelled workspace, so the
workspace/agents panel stays a clean overview and finished workers can be left in place. (Sibling
panes in the coordinator's tab are only for throwaway shell commands, never workers.)

1. Create worktree + workspace from the repository root, keeping the caller's focus:

   ```sh
   herdr worktree create --cwd "$PWD" --branch issue-<iid>-<slug> --label "#<iid> <slug>" --no-focus
   ```

   Read the worker pane from `.result.root_pane.pane_id` and the workspace from
   `.result.workspace.worktree.open_workspace_id`. Fall back to
   `git worktree add ../<repo>-<iid> -b issue-<iid>-<slug>` + `herdr workspace create` only if
   `herdr worktree create` is unavailable.

   **Worktree path:** a workspace whose repositories consume shared code through relative paths
   must create worktrees as **siblings inside the workspace**
   (`--path <workspace>/<repo>-issue-<iid>`). A worktree under `~/.herdr/worktrees/<repo>/<branch>`
   breaks those paths. Never add a `go.work` that points outside the worktree — use the committed
   `replace` for the shared module. The module path is the repository path, not a vanity path,
   deliberately, so a tagged version is consumable without a `replace` at all.

2. Start the worker TUI — `herdr pane run <pane> "opencode2 --auto"` — and wait until
   `herdr agent get <pane>` reports a detected agent in `idle`.

   **The worker model comes from configuration**, pinned for the whole workspace in the
   `opencode.json` one file above the repositories, so every worktree inherits it:
   `opencode2 --auto` carries no model flag. **Read the status line and confirm it names the
   intended model before prompting** — a worker on another model is mislaunched, and the status
   line is the cheapest place that says which model a worker is actually on.

   **When a limit bites, fall back per worker — the owner's rule:** the preferred model first,
   the coordinator's own provider (`opencode-go` in the reference setup) when the preferred model
   is rate-limited. That is the whole order and there is no middle rung. A rate-limited worker
   shows `Error: Rate limit exceeded. Please try again later.` in its transcript and waits at the
   composer; herdr may then classify it `blocked`, and `herdr agent prompt` refuses with
   `agent_blocked` — inspect the pane first, and when it is that error card rather than a question
   dialog, drive the pane surface instead (`herdr pane run <pane> "<continue text>"`). Switch the
   model in the TUI (`ctrl+x m`, or `ctrl+p` → "Switch model"), confirm the ● marker or the status
   line before continuing. The sign-off names the model that actually ran. **When the limit lifts,
   a worker on the fallback moves back up to the preferred model** rather than being left where it
   landed: the order is the state, not the history. New dispatches start from the pin, and the pin
   in the workspace's `opencode.json` never changes for any of this.

3. Name it after its issue and mark it in the agents panel:

   ```sh
   herdr agent rename <pane> ai-<iid>
   herdr pane rename <pane> "AI · #<iid> <slug>"
   herdr pane report-metadata <pane> --source ai-coordinator --display-agent "AI-managed"
   ```

   (Pane id first; the socket method `pane.report_metadata` with a long `ttl_ms` does the same.)

4. Prompt with the worker brief (below):
   `herdr agent prompt <pane> "<brief>" --wait --timeout 2400000`. A wait timeout does **not** mean
   failure — check `herdr agent get`, then re-arm with
   `herdr agent wait <pane> --timeout 3600000`; never resend the brief.

5. Watch agent state + issue state. Always target the **pane id** in waits — Herdr can drop an
   agent name on pane churn, and name-based waits then fail with `agent_not_running`. **Arm the
   completion watch as a background command, in two steps** — see below. Arming it in the same
   turn as the prompt gives a false positive, because the pane is still `idle` at that moment and
   `wait` matches `idle` by default.

6. Mark the pane `AI · #<iid> ✅ <slug>` once its branch is merged. The workspace stays in place
   for inspection until a cleanup sweep reclaims it (see *Cleanup after merge*).

## Knowing when a worker finished — use `fleet-watch`, not `herdr agent wait`

**`herdr agent wait <pane> --until done` is not a callback and must not be used as one.** It does
not fire when a worker finishes by going `idle` — and most workers finish that way: the agent signs
off with its session line and the TUI sits idle. Eight completions went unnoticed on the first day.
→ `docs/LESSONS.md` § *Knowing when a worker finished*.

The patch attempts all made it worse: `--until done` misses most completions; adding `--until idle`
and `--until blocked` produces false positives (a pane is briefly idle right after a prompt lands,
and a `blocked` return is usually a worker asking a legitimate question); a longer `--timeout`
returns a `timeout` payload **and the shell reports success**, so it looks like a clean return
unless the payload is read.

The root cause: **`agent_status` is not evidence.** `done` has meant finished-and-green, finished
with 353 lines uncommitted, finished with 11 files dirty, and finished with code that does not
compile. `idle` has meant both "waiting for a go-ahead" and "signed off, clean tree".

### The signal is evidence, and a pane is actionable on three facts

1. it has **commits ahead** of its base branch,
2. its working tree is **clean**,
3. **the gate is green** — `GOPROXY=off go vet ./...` and `GOPROXY=off go test ./... -count=1`.

That triple has no ambiguous reading. A crashed worker has none of the three. A worker mid-edit is
missing (1) or (2). A worker that reported `done` with uncompilable code fails (3).

### The tool

```bash
scripts/fleet-watch.py --once                     # sweep now, print the table, exit
scripts/fleet-watch.py --record <pane> <worktree> # at dispatch time, records the mapping
scripts/fleet-watch.py --watch --every 90         # THE CALLBACK: blocks, exits when actionable
scripts/fleet-watch.py --watch --stalled 8        # also exits if nothing has moved for 8 checks
```

Run `--watch` as a **background command**. It prints one line per pane whose *verdict changed*, so
a stable pane is silent and the coordinator is resumed **exactly once per real event** — which is
what a per-pane watch cannot do, and why re-arming produced duplicate notifications.

### The gate runs the fetch first, and reuses what is already there

The sweep's third fact is `provisioning → go vet → go test → showboat verify` for a Go repository,
and `provisioning → make check` otherwise. **The provisioning is first, and the order is the
claim.** Gating by hand in a different order names a symptom rather than a prerequisite: a
transcript failing with `bin/<binary>: No such file or directory` is a missing prerequisite, not a
code fault, and building first makes it pass with no change to the branch.

- The list of what a repository provisions is **parsed from its fetch script's `for repo in …`
  line**, not hardcoded. A script with no such line is reported `NO-GATE` **without being run**:
  guessing has two wrong answers and one right.
- **The shared checkout is the cache.** Every worktree in a workspace resolves the same shared
  directory, so "fetch once per worktree" is "fetch several times into the same place". When
  everything a script provisions is already beside the worktree, the fetch does not run at all.
  This is also why a workstation sweep needs no deploy token: nothing is cloned.
- A fetch that is refused (no token, no network) and a script it cannot read are both `NONE`,
  carrying the fetch's own refusal — not `RED` with a transcript block in it. The trade, stated
  rather than hidden: a genuinely broken branch whose sources are missing reports `NO-GATE` rather
  than `RED`; it wakes the coordinator either way.

### After a Herdr restart, the git worktrees survive and the workspaces do not

Measured once: a restart dropped from 126 workspaces to 46 and took five worker panes with it,
while **all eight worktrees were still on disk with every commit intact**. That asymmetry is why
"commit and push as you go" is not merely hygiene — it is what makes a restart a non-event.

Recovery, in order:

1. **Inventory the branches, not the panes.** `git -C <worktree> rev-list --count main..HEAD` and
   `git status --porcelain` over each worktree directory tell you what actually survived. A pane
   that will not answer tells you nothing about work.
2. **Rebuild the manifest** — `fleet-keepalive.py --rebuild`. `ORPHAN (no pane): <path>` is the
   signal to re-attach, not to remove.
3. **Re-attach with `herdr workspace create --cwd <worktree>`** and take the *new* pane id from
   `.result.root_pane.pane_id`. The old id is dead and using it fails in a way that looks like the
   agent ignoring its brief.
4. **Commit anything uncommitted *before* restarting the agent.** If the worker is already dead,
   commit it yourself as an explicit **save point**, and say in the message that it is a save point
   and **not an endorsement** — unreviewed, ungated, unproven — so the next reader knows why a
   file's author and its committer differ.
5. **Brief the replacement to recover from `git log`, not to start over.**

A restarted keepalive reports on pane ids that no longer exist, naming workers as ready that have
no agent at all: rebuild the manifest before reading any sweep, and treat a pane the sweep names but
`herdr agent get` cannot resolve as not running.

### Record the pane at dispatch time

`herdr worktree list` is scoped to the *current* repository, so there is no fleet-wide query to ask
herdr; the manifest at **`<repo>/.fleet-state/fleet.json`** is written by `--record` and doubles as
the record of what is live. It is gitignored, so recording a worker never dirties the tree. The
path is overridable with `FLEET_MANIFEST`. A worktree is discovered by its **branch** starting with
`issue-`, not by its directory name — worktrees are created as `<repo>-<iid>` and only the branch
says a worker is in flight.

### What to do when it fires

The watch says **look, not merge** — and "look" means run the gate yourself and read the verdict
column.

- **`MERGEABLE`** — run the gate independently, merge with `-F`, confirm
  `git merge-base --is-ancestor <branch> HEAD`, **then** close the issue.
- **`GATE-RED`** — a real failure. The detail column carries the first line of the error. Never
  merge on a worker's claim that its own gate passed; run it.
- **`IN-FLIGHT`** — commits exist but the tree is dirty. The worker is mid-edit. Do not merge; if it
  has been `IN-FLIGHT` across many checks, read the pane (`herdr pane read <pane>`) and send the
  **narrowest possible ask**.
- **`NOTHING`** — no commits. Either just started, or stopped without committing, which is the
  ambiguous case: read the pane before concluding anything.

### Still true, and still needed

- **`herdr agent wait <pane> --until working --timeout 120000` after prompting**, to confirm the
  worker engaged before you walk away. That use is fine — a one-shot confirmation, not a completion
  signal.
- **Read the pane before concluding anything about an ambiguous state.** `herdr pane read <pane>` is
  how good findings were recovered from workers that looked idle.
- **One watch per live worker, and a fired watch is spent.** `fleet-watch` removes the need for
  most of them.

## Worker brief (every worker gets all of this)

- Repository path and working tree (the main checkout or the worktree path), pane id, issue iid +
  title + labels + milestone.
- "Read `AGENTS.md`; you are a worker: no panes, no coordination, no other issues."
- **The worktree is not indexed.** CodeGraph covers the main checkouts, not the `<repo>-<iid>`
  siblings, so `codegraph_explore` with a worktree `projectPath` resolves nothing and Read/Grep is
  the correct fallback — not a failure to report. `codegraph init .` inside the worktree takes
  about a second if the branch is doing real work.
- The issue body (or a pointer: the worker's own tracker CLI can view it).
- The workspace tracker profile, so the worker claims its own issue and posts its own evidence
  (`docs/TRACKER.md`). **Never point the config dir at a directory named `.git`** — it breaks the
  build of every Go module in the workspace. If API calls still fail, put the full evidence in the
  final answer and say so — the coordinator relays.
- Build/test commands and the evidence rules (test output in the issue comment; `git status` clean
  before sign-off).
- The stack invariants this repository carries (see its own `AGENTS.md`).
- **English only. Never write Chinese** — not in code, comments, docs, commits, issue titles, issue
  bodies, issue comments, test names or fixtures, and not in the final answer to the coordinator.
  If a model, a tool or a copied snippet answers in Chinese, that is a defect to fix before sign-off.
- Commit policy: `refs #N` while working, `Closes #N` on the final commit; worktree workers commit
  on their branch, push it, and stop — the coordinator merges to the default branch.
- **Commit the moment there is something working, then keep going — push as you go.** An
  uncommitted tree is indistinguishable from no work. Small commits are free; one uncommitted hour
  is not recoverable. Never commit to the main checkout.
- The sign-off line: `— AI session ai-<iid> (opencode2 / <model>, Herdr <pane>)`
- "Stop when done or blocked, do not pick up other issues."

## Coordination rules

### The keepalive — `fleet-keepalive.py`, from cron every 30 minutes

```sh
python3 scripts/fleet-keepalive.py --stall 2 --cooldown 45    # what cron runs
python3 scripts/fleet-keepalive.py --dry-run                   # report only, send nothing
```

**It never merges, never closes an issue, never dispatches a ticket.** Those are the coordinator's,
and each needs something a timer does not have: the gate re-run, a merge message written for the
reasoning, and `git merge-base --is-ancestor` against the ref that was actually merged. A timer
that merges unattended is how a branch lands without anyone having read it. **The script's worst
outcome is a worker nudged to commit.**

| Condition | Action |
|---|---|
| pane in the manifest, no agent detected | restart the agent, and tell it to recover from `git log` — a restarted agent knows nothing |
| 0 commits + idle + unchanged `--stall` checks | ask whether it is blocked, or nudge it to begin |
| dirty tree + unchanged `--stall` checks | ask it to commit |
| `MERGEABLE` / `GATE-RED` | **report only** |
| a worktree listed in `FLEET_EXPECTED_RED` | reported as **RED ON PURPOSE**, and never a wake-up |
| a `RED` row in the last recorded `main_sweep.py` run | **report only**, and it withholds the all-clear |
| the main-sweep record missing, stale or unreadable | **not a pass**, and it withholds the all-clear |

Rate-limited to one action per pane per `--cooldown` minutes, so a wedged worker produces one
message per cooldown rather than one per run.

- **The sweep's reds reach this report because `main_sweep.py` records every completed run** —
  including its hourly `--dry-run` — in `.fleet-state/main-sweep.json`, and both the keepalive and
  the coordinator wake-up read that record through `scripts/main_sweep_state.py`. A **partial** run
  (`--repo`) is never recorded, because a partial record would read as a complete one whose unread
  repositories were green.
- **Each red row is readable on its own**: it names `main-sweep-red-<repo>-<commit>.log` beside the
  record, holding the full output of the failing gate step. `first_useful_line` is a judgement
  about which lines to show, and a red whose message it truncated sent its reader back to re-run
  the gate. Each log is capped at 256 KiB with head and tail kept, and every completed run prunes
  the logs it does not reference.
- **A branch can be red on purpose, and the keepalive has to know.** `FLEET_EXPECTED_RED` declares
  `{"worktrees": ["<abs path>"]}`, written when the branch is created and removed when it lands.
  Declared-red is still *reported*, in its own section, because silence is not the alternative — it
  is a report that cannot be wrong, which is worse.
- **It reads `fleet-watch.py --json`, never the printed table.** A wide table invites reading a
  number out of the wrong column, and that has produced a confident claim about a bug that did not
  exist. The table is for eyes; the script is a program.
- **It cross-checks the manifest against the sweep**, because the sweep only walks worktrees that
  exist — so a worker whose worktree was deleted vanishes with no row and no error, which looks
  identical to "it finished".

Logs: `/tmp/opencode/fleet-keepalive.log` and `.cron.log`. Nothing is mailed; the log is the signal.

### The cron job that wakes the coordinator

`notify-coordinator.py`, on the same cron entry, immediately after the keepalive:

```sh
python3 scripts/notify-coordinator.py --dry-run    # report only
python3 scripts/notify-coordinator.py --force      # wake regardless of whether it changed
```

**A notification is not a wake-up.** `herdr notification` tells a human something happened; it does
not resume an idle agent session. What actually works is `herdr agent prompt` against the
coordinator's pane, because that arrives as user input — the only thing a parked session reacts to.
Verified, not assumed.

- **The pane is resolved every run, never hardcoded.** It is the pane at the workspace root that is
  not a recorded worker, preferring the focused one. A hardcoded id is a lie waiting for the next
  session: when this one ends and another takes its place the id changes, and a stale value either
  wakes nothing or wakes the wrong session. The second is worse, because it looks like it worked.
- **Rate-limited by content, not by clock.** The state file holds a hash of the *set of ready
  panes*, so the coordinator is woken when that set changes — new work wakes, unchanged work stays
  quiet, and "all clear" is announced **once**.
- **Silence and all-clear are different facts.** A keepalive that cannot tell you which it is
  reporting is not reporting. The all-clear branch is covered by a test that asserts the *second*
  consecutive all-clear sends nothing.

### All-clear is a reminder to look, not a plan — and the policy lives here, not in the script

**The coordinator always continues. Only something critical stops it.** An empty queue is a fact
about the queue; it is not a fact about the work.

`notify-coordinator.py` reports what it can see and points at one question: *is there work available
that is not already in flight?* **It does not answer that question, and it must not try to.**
Deciding it means reading the tracker, knowing the capacity, and judging interference — and
interference is the coordinator's judgement by definition. The rules in this file bound what that
judgement may get wrong; a script cannot apply them, and one that tried would be a coordinator
nobody briefed, running unattended every thirty minutes.

| where | what |
|---|---|
| `notify-coordinator.py` | facts, and the question to go and answer |
| this file | parallelism is the default; the ceiling; the interference check; when to dispatch |

An earlier version put the policy *in the all-clear body* — "dispatch in this same turn", "a merge
frees a slot". That turned a reminder into a coordinator and put a convention in the file that runs
every 30 minutes, where nobody reads conventions. A change of scheduling policy is an edit here,
not a code change in a cron script.

**On all-clear, the coordinator goes and looks.** The reminder says status will not answer it:
`agent_status` has meant both "finished and signed off" and "crashed mid-edit". The evidence is the
worktrees (`rev-list --count main..HEAD`, `status --porcelain`), the last commit subject, and the
tracker — not a status field. Count the open issues **without** filtering on one label, and filter
by *state*, not just by label:

| what you read | how it lies |
|---|---|
| an "all issues" listing that includes closed ones | counts **closed** issues as work to do |
| `--label status::todo` (or the tracker's equivalent) | hides `doing` and `blocked`, which is most of a live fleet |

The survey is *open* issues, and then read the labels. A claim about how much work exists is a
claim about a **query**, and the query is part of the claim. → `docs/LESSONS.md` § *Working at the
coordinator's desk*.

**And a merge frees a slot: dispatch in the same turn as the merge.** If nothing is genuinely
unblocked, say so and name what you checked — an unexplained all-clear is indistinguishable from a
stopped coordinator, and those two look identical from outside.

### A pane nobody is watching is not a pane that is fine

The sweep walks worktrees **that exist**. A worker whose worktree was deleted — merged and cleaned
up, or lost to a Herdr restart — produces **no row and no error**, which is indistinguishable from
"it finished and was reclaimed".

So the all-clear is conditional on **every recorded worker having been swept** — it reads the same
manifest the keepalive reads, so the two components cannot disagree about whether the fleet is
whole. A ghost gets its own report, naming the pane and routing to `cleanup-workers.sh`.

**The real fix is reclaiming the worktree, not reporting the ghost.** Reporting it is what stops
the report from lying; it is not what makes the fleet healthy. A branch already merged is not a
worker, and leaving it in the manifest is what created the ghost.

### The all-clear is about health, not readiness

An all-clear filtered on `MERGEABLE` alone turns "nothing is ready" into "nothing needs you" — and
then a `GATE-RED` worker with a real failure and two large uncommitted trees go unmentioned. That
is the exact failure this file exists against: a check reporting something other than the truth,
and worse in a monitor than in a table, because a monitor is what you stop reading.

**So `GATE-RED` and `TOOL-INCONSISTENT` both count as actionable. `IN-FLIGHT` and `NOTHING`
deliberately do not** — a worker mid-edit, or one that has not committed yet, is what a working
worker looks like, and waking the coordinator for those is a cry of wolf. **Stalling is the
keepalive's judgement over several checks; this script only ever sees one.** The set goes
ready → empty → ready as tickets merge and dispatch, so each transition into all-clear is
legitimately news; the script reports the transition rather than promising silence forever. And
`--dry-run` must not mutate state — a dry run that moves the machine you were inspecting is worse
than no dry run.

### Reading a table is a skill with a failure mode

A wide table invites reading a number out of the wrong column and building a conclusion on it. That
happened: a sweep printed `working 4 1 0 MERGEABLE`, the `1` was the *behind* column of a different
row, and the conclusion drawn was a bug in `fleet-watch.py`. There was no bug; the tool was right
and the reading was wrong.

Three rules:

1. **One command, one observation.** A claim gets exactly one command behind it. If a claim needs
   two facts, run **one** command that prints both, together — never assert a single fact from two
   commands run at different moments. The gap between them is where the false claim lives.
2. **A verdict must carry its own reason** (`IN-FLIGHT 4 uncommitted file(s)`), so nothing has to be
   inferred by cross-reading columns. `fleet-watch` recomputes its own verdict from the recorded
   numbers by a second independent expression of the rule and prints `TOOL-INCONSISTENT` if the two
   disagree. **If you see `TOOL-INCONSISTENT`, fix the script — do not act on the row.**
3. **Distinguish suspicion from finding in the words.** "That looks inconsistent" and "the criteria
   say X and the row says Y" are different claims. Notice it looks wrong → say it looks wrong →
   **read the code or re-run the check** → then report the defect, or that there wasn't one.

### The four rules that were learned, not chosen

Each exists because it was got wrong. → `docs/LESSONS.md` § *Four learned rules*.

1. **A merge frees a slot — dispatch the next worker in the same turn as the merge.** Merging and
   then reporting, without refilling, is the default failure at any ceiling. The merge is not the
   last step; the next dispatch is.
2. **Agent status is not a delivery signal; the gate is.**

   ```
   GOPROXY=off go vet ./... && GOPROXY=off go test ./... -count=1
   git -C <worktree> status --porcelain              # empty?
   git -C <worktree> rev-list --count main..HEAD     # non-zero?
   ```

   A worker with zero commits and N files in flight is the ambiguous case: read the pane before
   concluding anything. If it stopped mid-edit, send it back with the **narrowest possible ask** —
   usually `git add -A && git commit && git push`. Do not restate the ticket.
   **A clean tree with zero commits is a third state, not a worse version of the second:** the work
   never started. The ask is short: name the two facts that are missing, point at the brief, say
   *commit as you go*.
   **"Narrowest" means narrower the second time.** The ladder is by defect kind: a concept the
   worker must hold → describe it; a specific line that must change → quote the line and its
   replacement; a guard that may be wrong → ask for a fixture. And when you quote the line, quote
   the sentence above it too — a transcript that prints a statement its own change made false is a
   document that is confidently wrong.
3. **An uncommitted tree is indistinguishable from no work.** Complete, correct work has survived
   two coordinator checks without landing because it was never committed. Tell every worker to
   commit the moment it has something working — it is in the brief.
4. **Do not believe a guard you have not broken.** Mutation-test it: add the sensitive field, delete
   the row the assertion depends on, and confirm it goes red for the stated reason. When a mutation
   fails to break the guard, say so; **never report proof you do not have**.

   **4a. A mutation that did not apply is not a finding — it is the worst one.** A passing guard and
   an inert mutation are indistinguishable from outside, so three checks are required first:
   (1) did it apply — assert the file changed; (2) did it compile — a build failure exits non-zero,
   so `exit=1` is not evidence the guard fired, grep for the guard's own message; (3) is the failure
   the *stated* one — only `--- FAIL: <the guard's own name>` is evidence. **The burden of proof is
   on the mutation, not the guard.**

## Parallelism, dispatch and interference

- **Parallelism is the default. Prove serial, do not assume it.** A single running worker while the
  coordinator waits on it is a coordination failure, not caution.
- **Be proactive: when something becomes schedulable, schedule it.** Concretely:

  | situation | what happens |
  |---|---|
  | a worker finishes and its branch merges | **dispatch the replacement in the same turn**, before reporting the merge |
  | a worker's verdict is `GATE-RED` or `NOTHING` and you send it back | **you freed a slot — fill it in the same turn** |
  | the fleet reports all-clear | **that is the strongest signal there is**, not an absence of one: go and find work |
  | the cron nudge arrives | it is a **backstop, not the trigger.** If the nudge is what made you look, the nudge is doing the coordinator's job |
  | you notice a dispatchable ticket while doing something else | dispatch it then, not at the end of the session |

  The test of a turn is not "did I finish my task" but **"is anything schedulable that I did not
  schedule."** Sitting idle at a quarter of the ceiling is a failure state, not a quiet one.
- **When you have a question for the user, use the `ask` tool** (or the harness's equivalent) — a
  structured choice with options and a recommendation, not a question in prose at the end of a
  turn. Ask when the answer changes what you do next; never claim the backlog is empty without a
  survey that can see it; put the recommendation first and say what each option costs.
- **The fleet ceiling is stated in the workspace's `opencode.json`/`factory.json` and starts at 15.**
  It is a **limit on damage, not a quota**: an interference check that genuinely fails is still a
  reason not to start, and 7 well-chosen workers beat 15 with two forced into the same file.
  Dispatch up to the ceiling, then check the interference table before each one. More workers in
  flight means branches fall behind `main` more often — rebasing on return is the normal path.
- **The coordinator never blocks its own turn on a worker.** Prompt, confirm the pane went to
  `working`, then return and manage the other workers. A long `--wait` turns the coordinator into a
  supervisor of one worker. A timed-out wait is not a failure — re-check `herdr agent get` and
  carry on.
- **Before starting worker N+1, run the interference check below — and start it anyway unless the
  check actually fails.** "I should check first" is not a reason to check; the check *is* the work.
- **Serialise only what genuinely conflicts** — same file, same worktree, same exclusive resource,
  or a real dependency — and in that case say *which* pair and why, in a note on the blocking issue.

**Interference check — answer all of these before the second worker, and act on the answers:**

| Question | Verdict |
|---|---|
| Same repository? | Fine **only** if the areas are disjoint. Name the files each will touch. |
| Same files? | **Serialise.** A shared file is a merge conflict with a delayed conflict report. |
| Real dependency (A must land first)? | **Serialise**, set `status::blocked`, and note the blocker. |
| Same fixtures, seed or golden path? | **Serialise.** Files being disjoint is not enough — see below. |
| Exclusive resource (live stack, soak, toolchain)? | **Serialise** per the grants table, with a scheduled grant. |

**Files are not the whole question.** Two tickets touching genuinely disjoint files can still break
the build when one writes a *shared fixture* the other asserts on — a seeded estate, a
`testdata/` census, a golden path, a port number, a registry entry. **A count asserted over a
shared fixture is a property of the fixture, not of the component under test.** Ask what your ticket
writes that another ticket also reads, and serialise even when no file is shared.

**And run the gate, then act on the number.** A non-zero count is not a formality to note in
passing: when it is not zero, the merge does not happen. A number you did not act on is a number
you stopped reading.

Disjointness is a merge-order fact, not a hope: two tickets that each touch `go.mod`, a `Makefile`,
a shared `types/` package or a `PROJECT.md` are **not** disjoint, however different their titles
look. `docs` tickets are usually disjoint from `code` tickets in the same repository, and whole
repositories are always disjoint from each other.

**Two tickets in one repository conflict even when their *subjects* differ.** A per-repository
migration does not license a second worker in that repository: **one migration, one worker per
repository**, and the next ticket waits for the merge. → `docs/LESSONS.md` § *Interference is not
file overlap*.

**A conflict resolution is an edit, and it gets no more trust than an edit.** Never resolve a
conflict by taking one side: read both sides, keep both, then run the target's own guards — because
the thing that catches a bad resolution is the guard the resolution was about to weaken.

- **Parallel workers never share a working tree:** each gets its own git worktree, with disjoint
  areas (`internal/store` vs `web/`) so the merge stays trivial. Create the next worker's worktree
  **only after** the previous merge has landed on the default branch (never in the same batch as a
  merge — a worktree created mid-merge branches from the old `main`), and check
  `git merge-base main issue-<iid>-<slug>` before briefing the worker.
- A worktree worker commits on its branch (`refs #N` → `Closes #N`) and pushes it; the coordinator
  verifies it, merges into the default branch and pushes — the merge is what closes the issue.
- The unblock chain is the queue: when a worker closes A and an open issue is blocked by A, flip it
  `status::blocked` → `status::todo` and hand it to the next worker.
- Verify a worker before treating its issue as done: tests/evidence linked, `git status` clean, and
  for worktree workers the branch pushed and merged.
- Small read-only investigations may be delegated to an in-session subagent instead of a full
  worker; the factory rules (one issue, evidence, no coordination from workers) still apply.

## Merging

**The merge gate is the coordinator's own run, and it is not the keepalive's verdict.** `MERGEABLE`
from `fleet-watch` means *the branch has commits, a clean tree, and the gate it knows how to run is
green*. It is a prompt to go and gate it yourself.

**Run it, do not type it.** `scripts/merge-gate.py` is that gate as a program:

```bash
python3 scripts/merge-gate.py <worktree>   # 0 green, 1 red, 2 unverified, 3 the gate is broken
```

It runs the shared sources first, then the repository's own gate, then `showboat verify demo.md` —
in that order, because `go vet` and `showboat verify` both read the shared checkout and `make check`
creates `web/node_modules`. It never merges, never closes an issue, never pushes and never
re-records a transcript block; that is a discipline for whoever merges, and a tool that guessed
which blocks a merge invalidates would re-record the wrong ones confidently.

The merge gate reports four states rather than two: `VERIFIED`, `STALE`, `NO-DEMO` and
`NOT-VERIFIED`.

- **A repository with no `demo.md` says so.** `NO-DEMO` is printed. An absent line reads as a pass,
  which is the defect.
- **A `demo.md` recording zero exec entries is `NOT-VERIFIED`, not green.** `showboat verify` exits
  **0** on it, in silence, having compared nothing. So the gate asks `showboat extract` what the
  document records, and zero entries is exit 2.
- `NOT-VERIFIED` is where the sweep and the merge gate deliberately differ: `fleet-watch` folds it
  to `GREEN` with the reason stated, because a sweep that reports every pane unverified because
  `showboat` is not installed is a sweep nobody reads — while a **merge** authorised on a transcript
  nobody checked is the failure the whole gate exists against. Two folds over one verdict, asserted
  in `scripts/test_merge_gate.py` so neither drifts unnoticed.

So the merge is:

1. **`python3 scripts/merge-gate.py <worktree>`** — and read its **exit status**, which it also
   prints. `showboat verify` exits 0 **in silence**, so "I ran it and saw nothing" is not evidence;
2. **the mutation proofs the change claims**, if it claims any;
3. **re-record the transcript blocks this merge invalidated**, in the same merge — a transcript is a
   snapshot of a run, so a merge that moves the thing it snapshots has to move it too;
4. then `git merge -F`, `git merge-base --is-ancestor`, push, close.

**A transcript may not depend on unrecorded machine state.** Measured example: `pnpm install` prints
`Already up to date` only when `node_modules` is already current, so a transcript recording that line
verifies on a warm tree and goes red on a fresh clone — and a fresh clone is the one state the
document exists to be provable in. There are two correct answers and the choice is per block,
because the property is "verifies in a fresh clone", not "uses the same filter list": either the
output is never recorded, or the output is recorded and the normaliser removes what depends on the
state. **Measure the cold tree rather than guessing which lines are conditional**, and check both
directions.

**Merge first, close second — in that order, always.** A closed issue with no code behind it is what
happens when the order is reversed. The merge is what makes the issue true; closing is a consequence
of it. After every merge confirm with `git merge-base --is-ancestor <branch> HEAD` before closing
anything.

- **Verify the merge against the branch you actually merged, not `origin/<branch>`.** A branch that
  was rebased cannot be force-pushed (the pre-receive hook blocks it), so `origin/<branch>` still
  points at the pre-rebase tip while the merge used the local one — and the check then reports "not
  merged" about a merge that is on `main` and pushed. Use the local branch, or the merge commit's
  second parent (`git rev-parse --short HEAD^2`).
- **Write merge messages to a file and pass `-F <file>`.** A message in `git merge -m "…"` is a
  double-quoted shell string, so **backticks inside it are command substitution**: the message runs
  the command, prints `command not found`, and hands git a mangled argument list — the merge then
  fails quietly enough to look like it worked. After any worker merge, verify the message survived:
  `git log -1 --format=%B <sha>`.
- **A rebased worker branch cannot be force-pushed** — the hook blocks it, so a rebase makes the
  branch unpushable. **Do not merge the remote branch in to reconcile it**: the two histories
  contain the same changes on different bases, and you get a second conflict plus a merge commit
  recording duplicate work. Push the rebased branch under a new name (`issue-<iid>-<slug>-v2`),
  merge *that*, and let the hook keep the old branch. Merging a branch name that only exists on the
  remote fails with *"not something we can merge"* — fetch `refs/heads/*:refs/remotes/origin/*`
  rather than relying on the default fetch refspec.
- **Watch for workers committing to the default branch.** A worker that writes to the main checkout
  instead of its worktree is not a merge problem, it is a fence failure — check `git reflog` on the
  main checkout when a branch's base looks wrong.
- Mark finished panes `AI · #<iid> ✅ <slug>`. They stay inspectable until a cleanup sweep reclaims
  the worktree, workspace and branches; the session is never deleted.
- Stop after the number of workers the user asked for.

## Exclusive grants

A ticket that needs a shared, stateful resource does not imply permission to touch it now. Exactly
one AI session may hold each exclusive resource, and only while the coordinator has granted it.
Define the resource table per project; the pattern:

| Resource | What it covers | Marker |
|---|---|---|
| **live stack** | the running compose stack, verify scripts against real services, anything binding the public ports, the main checkout's data directory | `READY-FOR-LIVE:` → `LIVE RELEASED — <state>` |
| **upstream soak** | long real-data runs and scripts that rewrite committed fixtures | `READY-FOR-SOAK:` → `SOAK RELEASED — <state>` |
| **toolchain** | shared build toolchains, container images and caches every tree reads | `READY-FOR-TOOLCHAIN:` → `TOOLCHAIN RELEASED — <state>` |
| **browser** | the `agent-browser` daemon — every browser walkthrough, anything driving a real page | `READY-FOR-BROWSER:` → `BROWSER RELEASED — <state>` |
| **dagger engine** | `dagger call` runs. Concurrent calls contend for one engine and report `buildctl dial-stdio … exit 137`, which reads as an OOM and is not one | `READY-FOR-DAGGER:` → `DAGGER RELEASED — <state>` |

**The grant table is only as good as its last entry, and a resource nobody wrote down is a resource
everybody collides on.** Two were found the same way — a gate red for a reason that was not the
code: the browser session name (see below) and the Dagger engine.

- **`agent-browser` is one browser instance per *session*, not one for the whole workspace.** The
  CLI keeps `/run/user/<uid>/agent-browser/<session>.sock`, and its own help says *"each session
  has its own browser instance with separate cookies, storage, and state"* — so repositories running
  their golden paths do **not** contend with each other, and the evidence is not only reproducible
  one at a time. What *does* collide is a **session name**, because a run's page, viewport and
  daemon are shared by anything using that name. So the rule is **one session name per run**, not
  one golden path at a time. Every golden-path run still takes the grant, including a worker's own
  local run — the unbidden local run is the common case, not the exception.
- **Dagger is serialised** for the same reason, and the symptom being mistaken for an OOM is what
  makes it dangerous: the reflex is to free memory, and the cause is a lock.

Rules:

- **Batch composition.** At most one holder per resource at a time. Non-exclusive workers (unit
  tests, docs, self-contained code) run in parallel; exclusive tickets get a *scheduled* grant.
- **Prepare off-resource first.** Reading code, unit tests, `go vet`, `gofmt`, the SPA build and
  `make api-check` are always allowed. Starting live services, real-data soaks and toolchain
  rebuilds are not, until granted.
- **Ask, then wait.** A worker ready for the resource comments
  **`READY-FOR-LIVE: <plan + exact commands + what it will start/replace>`** (or the matching
  marker), ends its turn idle and waits. The coordinator prompts it "<resource> granted" in the same
  session once the resource is free. This is a scheduled grant, not a blocker — do not set
  `status::blocked`.
- **Release cleanly.** Before the resource moves on, the holder restores a known-good state, says
  so, and comments **`<RESOURCE> RELEASED — <state>`**.
- **The coordinator owns the queue.** It orders grants by ticket priority, verifies release state
  before the next handoff, prompts the waiting worker, and never lets two holders overlap.
- Every grant brief states the discipline explicitly: who holds the resource, what is allowed before
  the grant (builds, tests, prep), the exact marker to comment, and the state to leave behind.

### The browser grant has a mechanism, and it is not the grant

`scripts/browser-grant.py` is **additive**: it never authorises a run. The coordinator-held grant
above answers *may this worker touch the browser at all*, and it is a human decision a person can
see, override and re-order. The script answers the narrower question a machine can answer — *is the
session I am about to touch mine alone* — and it reports what it cannot verify rather than enforcing
it:

- a run with no `--granted-by` is reported **UNGRANTED** by `status`, not blocked;
- `force-release` refuses a **live** holder without `--force-live`, and appends every override to
  `overrides.log` with its reason;
- nothing queues silently. Default `--wait 0`, so a collision **refuses with exit 75** and the
  command **never runs** — no assertion is evaluated, so nothing can be falsified. A worker blocked
  inside a verify script cannot answer a human and is lost to a Herdr restart, so refusing beats
  waiting; `--wait` is for a worker racing itself.

```bash
python3 scripts/browser-grant.py status          # HELD / STALE / FREE, and UNGRANTED
python3 scripts/browser-grant.py run --session "$S" --label "press #7" --granted-by coordinator -- scripts/verify-golden-path.sh
```

**The failure gets a name, and the name is not always the same one.** `agent-browser` reports a busy
daemon as `exit status 1` with the reason underneath, and it arrives as an ordinary test failure.
`classify` turns it into one of five verdicts, and the fifth is the point:

| Verdict | Meaning | Retry? |
|---|---|---|
| `PASS` / `FAIL` | ran; evidence about the code | n/a |
| `BUSY` (75) | transport token, and this run did **not** hold the session — **not evidence** | safe |
| `BROWSER-FAULT` (76) | transport token while this run **held** the session, so contention was impossible: a sick daemon | **no** |
| `UNDECIDED` (78) | no exit status, or nobody checked the lock — a guess refused | n/a |

`UNDECIDED` exists because `agent-browser` confirms success in prose, so its output alone is not a
failure signal. And `os error 11` is matched on word boundaries: a plain substring test reads
`os error 111` (a bound port) as daemon contention. The mechanism and its measurements are
`docs/browser-grant.md`.

## Cleanup after merge

A merged ticket leaves a worktree folder, a local and a remote branch, and a Herdr workspace behind.
The coordinator reclaims them on demand with `scripts/cleanup-workers.sh` — **dry-run by default,
`--yes` to apply**. Nothing runs automatically as part of a merge; the `✅` mark is the hand-off
into this sweep.

Coordinator only. A worker never runs it, not even on its own worktree: it is executing inside the
directory the sweep removes.

Each worker workspace is only reclaimed when **all** of these hold:

| Check | Guards against |
|---|---|
| `worktree.repo_root == $REPO_ROOT` | acting on another repository's worker |
| `worktree.is_linked_worktree == true` | deleting the primary repository checkout |
| `git -C <checkout> status --porcelain` empty | losing uncommitted work |
| `git merge-base --is-ancestor <branch> main` | dropping an unmerged branch |
| no open PR/MR for `<branch>` | racing a merge still in flight |
| issue `<iid>` closed in the tracker | reclaiming a ticket that did not land |
| no agent in the workspace `working` | killing a live agent |

Removal order: `herdr worktree remove` → `git worktree prune` → `git branch -d` (never `-D`, so git
itself refuses an unmerged branch) → `git push origin --delete` → `herdr workspace close`. Each step
warns and continues rather than aborting the sweep.

- **The sweep is scoped to one repository, and `--iid` with it.** `herdr workspace list` is not
  scoped to a repository — `herdr worktree list` is — so the candidate filter reads
  `.worktree.repo_root` and compares it to `REPO_ROOT` itself. Without that filter, `--repo` decides
  only which checkout to read and every repository's sweep names every other repository's workers.
  Foreign workspaces are reported as a per-repository **count**, never listed.
- **`--iid` is repo-scoped and says so in `--help`**, because issue numbers repeat across
  repositories and an unscoped `--iid 2` means "some repository's #2" in fact.
- A workspace with **no `worktree` object** cannot be attributed to any repository. It is reported
  in its own section, never filtered out: hiding it to make the filter tidy would trade a false
  report for an invisible one.
- **"nothing to do." is not a report.** *Found nothing*, *found N and reclaimed none*, and *found N
  that all need a decision* are three different facts and print three different sentences; every
  section carries a count, and every row carries its workspace id — a refusal without a workspace id
  cannot be acted on.
- **Sessions are never deleted.** The opencode transcript lives independently of the pane and stays
  exportable and recallable; the sweep prints the session id it preserved.
- **Anything the predicate rejects is reported, not removed** — an abandoned worker, a dirty
  worktree, an open PR. Discarding work is the user's call, not the sweep's.
- **Stale registrations** from a crashed worker (path gone, registration left behind) are pruned.
  Never `rm -rf` a path the sweep did not resolve as a worktree of this repository.
- Report the outcome in the issue when a sweep reclaims a ticket.

### The workspace is not closed when the worktree is already gone

**A merged agent is not reclaimed until `herdr workspace close <ws>` has run and reported.**
Worktree gone, branch merged, issue closed — but workspace still open is *not* done, and that is
the leak this second sweep exists for. Measured once: twelve such workspaces open, after twenty-eight
had been closed by hand.

`cleanup-workers.sh` reports these as refused for a reason that is both invisible and untrue when it
reads the branch from `git worktree list` (a workspace whose checkout was deleted has no entry).
The fix keys off **the path on disk**, not on `is_linked_worktree` and not on a registration. It is
refused only when a branch git can still name (`issue-<iid>-*`) is **not merged into main** — using
the sweep's own `merge-base --is-ancestor` and its own tracker PR/issue pair, because a new check in
a second place is a check that will drift.

**The close is delegated, not duplicated.** `--close-gone --yes` hands exactly those workspaces to
`close-gone-workspaces.py --only …`. That script owns the decision and its refusals with 28 tests
behind it; a second implementation in bash would be free to disagree with them, and *two pruning
implementations that disagree* is the failure this family keeps hitting. Delegation is **opt-in**:
one sweep deletes branches and the other closes workspaces, and both acting unattended in a single
command is a bigger claim than either makes on its own.

```sh
python3 scripts/close-gone-workspaces.py          # dry-run, prints the table
python3 scripts/close-gone-workspaces.py --yes    # close them
python3 scripts/close-gone-workspaces.py --audit  # classify every workspace, close nothing
```

It needs **no git and no repository**: a directory that no longer exists cannot be a live worktree,
so it is fleet-wide by construction and cannot be confused by which repository it was run against.
Order it after `cleanup-workers.sh`: the first sweep reclaims a worktree that still exists, the
second reaps the workspaces whose worktree already vanished.

It also takes **`--only <workspace>…`**, which narrows the candidate set and nothing else: every
refusal above still applies. **A `--only` naming a workspace Herdr does not know is reported**, not
ignored: a filter that silently matched nothing looks exactly like a sweep that found nothing to do.

| Refused when | Guards against |
|---|---|
| the directory still exists | closing a live workspace on a stale `(deleted)` marker |
| an agent in it is `working` | killing a worker mid-turn |
| **any** pane's runtime answers `herdr pane read` | closing a TUI that is still up over a deleted directory |
| the workspace is the coordinator's (`HERDR_WORKSPACE_ID`) | closing your own workspace — resolved per run, never hardcoded |
| a pane is in `.fleet-state/fleet.json` | closing a recorded worker whose worktree another sweep just deleted |

**`herdr pane read` returns raw terminal text, not JSON.** A probe that runs `json.loads` on it gets
a `JSONDecodeError`, and a caller that reads that exception as "no error returned" concludes every
live pane is dead — one step from closing the coordinator's own workspace. `parse_pane_read()` keys
on a JSON payload containing an `error` key, and a test holds it.

**Re-verify at the moment of the close, not from an audit taken earlier.** Another coordinator
session in the same workspace can reap worktrees concurrently — the fleet moved from 92 workspaces
to 40 during a single audit. The live-pane and `working` checks run per workspace immediately before
`workspace close`.
