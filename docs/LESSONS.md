# Lessons

These are the incidents behind the factory's rules: dates, measurements, exit codes, quotes, and the wrong conclusions they produced. The rules themselves are compressed into `FACTORY.md` and `AGENTS.md`; this file is the evidence for them — the document to read when a rule looks arbitrary, or before you re-derive it the hard way.

Commands appear in the GitLab flavor the incidents were measured in; the tracker itself is covered
neutrally in `docs/TRACKER.md`. A few scripts named in the stories belong to the reference workspace
this template was extracted from and are not shipped here — `scripts/README.md` is the inventory.

## Workspace traps

### The build that failed because of a directory named `.git`

The workspace root is a container of sibling clones and is not itself a git repository, so a directory named `.git` there is a lie that tooling believes. `go build -o` and `go list` stamp VCS information by running `git status` from the module's parent directory, and Go only accepts `.git` as a repository root when it is a directory. A linked worktree's `.git` is a file, so Go skips the worktree, walks up, finds the workspace root's `.git`, and runs `git status` there — which exits 128 and fails the build:

```
error obtaining VCS status: exit status 128
    Use -buildvcs=false to disable VCS stamping.
```

This reads as the worker's own code failing, and `go vet` and `go test` stay green throughout because neither stamps VCS. It cost a real diagnosis on 2026-10-03: the directory had been created by a worker exporting `GLAB_CONFIG_DIR=<main-checkout>/.git/glab-cli` and reading "main checkout" as the workspace root. `-buildvcs=false` is the per-repo fix (a meta ticket); keeping config out of a directory named `.git` is the fix that stops it recurring — and a family-wide guard held it across the repositories in the reference workspace, where the rule was measured.

**The rule it earns: the build should not care what is above it.**

## Knowing when a worker finished

`herdr agent wait <pane> --until done` is not a callback and must not be used as one. It does not fire when a worker finishes by going idle — and in this project most workers finish that way: the agent signs off with its session line and the TUI sits idle. Eight completions went unnotified on the first day; every one was caught by accident, during a status sweep that happened to be running.

The attempts to patch it all made it worse, and the reasons are worth keeping:

| Attempt | Why it fails |
|---|---|
| `--until done` | Never fires for a worker that ends `idle`. Misses most completions. |
| add `--until idle` | A pane is idle for a moment right after a prompt lands, and an agent passes *through* blocked while thinking. False positives, indistinguishable from real deliveries. |
| add `--until blocked` | Same. A `blocked` return is a worker asking a question — often legitimate — not a delivery. |
| a longer `--timeout` | A timeout returns `{"error":{"code":"timeout"}}` and the shell reports success, so it looks like a clean return unless the payload is read. Four of these fired within a minute and all four workers were still working. |

The root cause is that `agent_status` is not evidence. `done` has meant finished and green, finished with 353 lines uncommitted, finished with 11 files dirty, and finished with code that does not compile. `idle` has meant both "waiting for a go-ahead on two findings" and "signed off, tree clean, 9 commits". No single status separates those.

### The signal is evidence

A pane is actionable on three facts:

1. it has commits ahead of its base branch,
2. its working tree is clean,
3. the gate is green — `GOPROXY=off go vet ./...` and `GOPROXY=off go test ./... -count=1`.

That triple has no ambiguous reading. A crashed worker has none of the three. A worker mid-edit is missing (1) or (2). A worker that reported `done` with uncompilable code fails (3). A worker that finished perfectly passes all three.

The tool is `scripts/fleet-watch.py`:

```bash
scripts/fleet-watch.py --once                    # sweep now, print the table, exit
scripts/fleet-watch.py --record <pane> <worktree> # at dispatch time, records the mapping
scripts/fleet-watch.py --watch --every 90         # THE CALLBACK: blocks, exits when actionable
scripts/fleet-watch.py --watch --stalled 8        # also exits if nothing has moved for 8 checks
```

`--watch` runs as a background command. It prints one line per pane whose *verdict changed*, so a stable pane is silent and the coordinator is resumed exactly once per real event — which is what a per-pane watch cannot do, and why re-arming produced duplicate notifications four times in one session.

The status line, unlike the status field, is worth reading: it is what tells you which model a worker is on. Confirm the pinned worker model before prompting.

### The gate order, and the fetch

The sweep's third fact is the gate: `provisioning → go vet → go test → showboat verify` for a Go repository, and `provisioning → make check` otherwise. The provisioning is first, and the order is the claim.

That order binds the coordinator too, which I broke again on 2026-10-06 and paid for twice. I gated a worktree by hand as `gofmt → vet → test → showboat`, and `showboat` answered

```
bash: line 77: bin/media: No such file or directory
```

which is a missing prerequisite, not a code fault. The transcript executes a binary the repository builds; nothing had built it. Building first (`GOPROXY=off make build`) made `showboat` pass with no change to the branch at all. The second cost was the same mistake in a worse place: `gofmt → vet → test → showboat` also skipped `scripts/fetch-shared-sources.sh`, which is what produced the block-2 verdict below.

So: gate in the order the sweep gates, by running the sweep. A verdict naming a symptom rather than a prerequisite is the tell, and `bin/<binary>: No such file or directory` from a transcript is the prerequisite every time.

### The block-2 verdict that named a symptom

It used not to exist. `fleet-watch.py` ran `showboat verify demo.md` and nothing else, never `scripts/fetch-shared-sources.sh`, so for every repository that fetches its siblings from the group the row read

```
wPG:p1  issue-22-groupfetch  GATE-RED  clean tree, gate red
    -> demo.md does not verify: block 2:
```

Block 2 is *a clean checkout builds one binary with the SPA inside it*, and the SPA build needs the fetched shared UI. The verdict named a transcript block while the missing thing was a prerequisite — and since the documented response to `GATE-RED` is *go and gate it*, it sent a coordinator to reject a green branch. Twice.

That tell generalises further than this bug: a verdict that names a symptom rather than a prerequisite is the tell. Same shape as the `withRepo`/`node_modules` order trap, and same shape as `required flag(s) "meta" not set`, which named a missing flag when the real problem was a stale binding.

### Reuse, not fetch per sweep

All eighteen copies of `fetch-shared-sources.sh` in this family compute the same target, `SHARED="${CI_PROJECT_DIR:-$(pwd)}/../shared"`, and every worker worktree is a sibling under one workspace root. So there is one shared directory for the whole fleet, not one per worktree — which means "fetch once per (repository, worktree)" is "fetch five times per sweep, into the same place". The clone is the cheap half anyway; the script ends with `pnpm install --frozen-lockfile && pnpm build` inside `shared/ui`, and `pnpm install` in a directory that already has a modules directory does not fail, it waits for a `Proceed?` a non-terminal cannot answer (measured in `media`: 19 s green without `web/node_modules`, ten minutes and counting with it).

So the shared checkout is the cache — a better one than a dict in the process, because it survives a restart and every sweep shares it. When everything the script provisions is already beside the worktree, the fetch does not run at all, which is also why a workstation sweep needs no deploy token: nothing is cloned. When something is genuinely missing it runs once, and what it leaves behind *is* the cache.

The list of what a repository provisions is parsed from the script's `for repo in ...; do` line, not hardcoded — `media` fetches three sources, `identity` four, and the meta repo is in nobody's list until `web/vite.config.ts` started reading its `apps.json` at build time. A script with no such line is reported `NO-GATE` without being run: guessing has two wrong answers and one right.

A fetch that is refused (no token, no network) and a script it cannot read are both `NONE`, carrying the fetch's own refusal — not `RED` with a transcript block in it. The trade, stated rather than hidden: a branch that is genuinely broken *and* whose sources are missing reports `NO-GATE` rather than `RED`, because without the prerequisite the sweep cannot tell the two apart. It wakes the coordinator either way, and after the fetch it reports the real failure.

### After a Herdr restart

After a Herdr restart, the git worktrees survive and the workspaces do not. This was measured on 2026-10-03: a restart dropped from 126 workspaces to 46 and took five worker panes with it, while all eight worktrees were still on disk with every commit intact. The asymmetry is the whole lesson, and it is why "commit and push as you go" is not merely good hygiene — it is what makes a restart a non-event.

Recovery, in order:

1. Inventory the branches, not the panes. `git -C <worktree> rev-list --count main..HEAD` and `git status --porcelain` over each worktree tells you what survived. A pane that will not answer tells you nothing about work.
2. Rebuild the manifest — `fleet-keepalive.py --rebuild`. It reports `ORPHAN (no pane): <path>` for a worktree whose workspace died, which is the signal to re-attach rather than to remove.
3. Re-attach with `herdr workspace create --cwd <worktree>` and take the new pane id from `.result.root_pane.pane_id`. The old id is dead, and using it fails in a way that looks like the agent ignoring its brief.
4. Commit anything uncommitted *before* restarting the agent. An untracked `demo.md` is one restart from gone. If the worker is already dead, commit it yourself as an explicit save point, and say in the message that a commit there is a save point and not an endorsement — unreviewed, ungated, unproven — so the next reader knows why a file's author and its committer differ.
5. Brief the replacement to recover from `git log`, not to start over. A restarted agent knows nothing, and a brief that says "continue your ticket" produces a worker that re-derives what is already on the branch.

And a restarted keepalive reports on pane ids that no longer exist, naming workers as ready that have no agent at all. Rebuild the manifest before reading any sweep, and treat a pane the sweep names but `herdr agent get` cannot resolve as not running.

### The manifest, and `--record`

`herdr worktree list` is scoped to the current repository, so there is no fleet-wide query to ask herdr. The manifest at `<repo>/.fleet-state/fleet.json` is written by `--record` and doubles as the record of what is live. It is gitignored, so recording a worker never dirties the tree. The path is `fleet-watch.py`'s `MANIFEST`, overridable with `FLEET_MANIFEST` — not `/tmp/opencode/fleet.json`, which is what an earlier version of the paragraph said and where a stale file from that era still sits. A `--record` that reports success has written to the code's path, so reading the wrong one shows a manifest full of workers that finished hours ago and none of the ones you just dispatched. A worktree is discovered by its branch starting with `issue-`, not by its directory name — worktrees are created as `<repo>-<iid>` and only the branch says a worker is in flight.

### What to do when it fires

It says look, not merge — but unlike a status, "look" here means a specific thing: run the gate yourself and read the verdict column.

- `MERGEABLE` — run the gate independently, merge with `-F`, confirm `git merge-base --is-ancestor <branch> HEAD`, then close the issue.
- `GATE-RED` — a real failure. The detail column carries the first line of the error. Never merge on a worker's claim that its own gate passed; run it.
- `IN-FLIGHT` — commits exist but the tree is dirty. The worker is mid-edit. Do not merge; if it has been `IN-FLIGHT` across many checks, read the pane (`herdr pane read <pane>`) and send the narrowest possible ask.
- `NOTHING` — no commits. Either just started, or stopped without committing, which is the ambiguous case: read the pane before concluding anything.

Still true, and still needed: `herdr agent wait <pane> --until working --timeout 120000` after prompting, to confirm the worker engaged before you walk away — a one-shot confirmation, not a completion signal. Read the pane before concluding anything about an ambiguous state; that is how two sets of good findings were recovered from workers that looked idle, and how a worker that stopped mid-import was distinguished from one waiting for a decision. And one watch per live worker, with a fired watch spent.

The three states, so a sweep row can be read without inference:

| commits | dirty | what it is | what to do |
|---|---|---|---|
| 0 | N | mid-edit, or stopped mid-edit | wait; nudge if unchanged across checks |
| 0 | 0 | never started, or died before its first edit | ask, with the brief path |
| N | 0 | finished | gate it |

**The rule it earns: a status field cannot distinguish delivery; a pane is actionable on commits ahead, a clean tree, and a green gate — and nothing else.**

## The keepalive and the wake-up

`fleet-keepalive.py` runs from cron every 30 minutes:

```sh
python3 scripts/fleet-keepalive.py --stall 2 --cooldown 45    # what cron runs
python3 scripts/fleet-keepalive.py --dry-run                   # report only, send nothing
```

It never merges, never closes an issue, never dispatches a ticket. Those are the coordinator's, and each needs something a timer does not have: the gate re-run, a merge message written for the reasoning, and `git merge-base --is-ancestor` against the ref that was actually merged. A timer that merges unattended is how a branch lands without anyone having read it. The script's worst outcome is a worker nudged to commit, and it is rate-limited to one action per pane per cooldown, so a wedged worker produces one message per cooldown rather than one per run.

### Records, because a log nobody reads is not a report

The sweep's reds reach this report because `main_sweep.py` records every completed run — including its hourly `--dry-run` — in `.fleet-state/main-sweep.json`, and both the keepalive and the coordinator wake-up read that record through `scripts/main_sweep_state.py`. The rows used to live only in `/tmp/opencode/main-sweep.cron.log` (a meta ticket), and `social-studio`'s main was red for three hours while the sweep gated it seven times — in a log no person read. A partial run (`--repo`) is never recorded, because a partial record would read as a complete one whose unread repositories were green.

And the row is readable on its own, which the summary alone was not. Each red row names `main-sweep-red-<repo>-<commit>.log` beside the record, holding the full output of the failing gate step (a meta ticket) — because `first_useful_line` is a judgement about which lines to show, and a red whose message it truncated sent its reader back to re-run the gate. `identity`'s panic was sighted three times without ever yielding its message; `portal`'s `errno` was unprovable from the record. Each file is capped at 256 KiB with head and tail kept, and every completed run prunes the logs it does not reference — so a green run never grows storage.

The main-sweep record missing, stale or unreadable is not a pass: `main` is the ref every branch is created from, so "not swept" must not read like "swept and green".

### Red on purpose

`media`'s golden path was reverted from `main` and rebuilt on a branch that fails deliberately, because the ticket *is* the conflict between two already-merged features. Without a declaration, the keepalive reported that branch `GATE-RED` every thirty minutes for as long as the work takes — and a report that is always true is a report nobody reads, which is the whole reason this is written down.

So `/tmp/opencode/fleet-expected-red.json` declares `{"worktrees": ["<abs path>"]}`, written when the branch is created and removed when it lands. Declared-red is still reported, in its own section, because silence is not the alternative — it is just a report that cannot be wrong, which is worse.

### Reading the machine, not the table

It reads `fleet-watch.py --json`, never the printed table. A wide table invites reading a number out of the wrong column, and that has already produced a confident claim about a bug that did not exist (the story is in its own section below). The table is for eyes; the script is a program.

And it cross-checks the manifest against the sweep, because the sweep only walks worktrees that exist — so a worker whose worktree was deleted vanishes with no row and no error, which looks identical to "it finished". It found two stale entries in the manifest the first time it ran, from tickets merged hours earlier. That is the check that the thing which should still be there is.

Logs: `/tmp/opencode/fleet-keepalive.log` and `.cron.log`. Nothing is mailed; the log is the signal.

### Waking the coordinator

`notify-coordinator.py` runs on the same cron entry, immediately after the keepalive:

```sh
python3 scripts/notify-coordinator.py --dry-run    # report only
python3 scripts/notify-coordinator.py --force      # wake regardless of whether it changed
```

A notification is not a wake-up. `herdr notification` tells a human something happened; it does not resume an idle agent session. What actually works is `herdr agent prompt` against the coordinator's pane, because that arrives as user input — the only thing a parked session reacts to. Verified, not assumed: the first real fire produced the expected message in the coordinator session.

The pane is resolved every run, never hardcoded: the pane at the workspace root that is not a recorded worker, preferring the focused one. A hardcoded id is a lie waiting for the next session — when this one ends and another takes its place the id changes, and a stale value either wakes nothing or wakes the wrong session. The second is worse, because it looks like it worked.

Rate-limited by content, not by clock: the state file holds a hash of the *set of ready panes*, so the coordinator is woken when that set changes — new work wakes, unchanged work stays quiet, and "all clear" is announced once. A cron job that wakes you every 30 minutes for the same unmerged branch is worse than no cron job: the second time it fires you learn the alert is noise, and a check you have learned to ignore is one you stop reading. Silence and all-clear are different facts, and a keepalive that cannot tell you which it is reporting is not reporting. A test asserts that the second consecutive all-clear sends nothing.

### The policy lives in the file, not in the script

The coordinator always continues; only something critical stops it. An empty queue is a fact about the queue, not about the work.

`notify-coordinator.py` reports what it can see and points at one question: is there work available that is not already in flight? It does not answer that question, and it must not try to. Deciding it means reading the tracker, knowing the capacity, and judging interference — and interference is the coordinator's judgement by definition. A script that tried would be a coordinator nobody briefed, running unattended every thirty minutes.

An earlier version put the policy in the all-clear body — "dispatch in this same turn", "a merge frees a slot". That turned a reminder into a coordinator and put a convention in the file that runs every 30 minutes, where nobody reads conventions. A change of scheduling policy should be an edit in the rulebook, not a code change in a cron script. `test_notify_coordinator.py` asserts the script carries no policy, so the creep cannot come back quietly.

**The rule it earns: the machinery reports facts and wakes a human session; the scheduling judgement stays with the coordinator, and a report that cannot be wrong is worthless.**

## The all-clear was wrong, three times

### Health, not readiness

The first version filtered on `MERGEABLE` alone. So "nothing is ready" became "nothing needs you", and the tool announced "no worker is ready to merge and nothing is stalled, the fleet is 8 panes and moving" while one worker sat `GATE-RED` with `the program failed for the wrong reason` and two others held large uncommitted trees. That is the exact failure the rules exist against: a check reporting something other than the truth. And it is worse in a monitor than in a table, because a monitor is what you stop reading.

So `GATE-RED` and `TOOL-INCONSISTENT` both count as actionable. `IN-FLIGHT` and `NOTHING` deliberately do not — a worker mid-edit, or one that has not committed yet, is what a working worker looks like, and waking the coordinator for those is a cry of wolf. Stalling is the keepalive's judgement over several checks; this script only ever sees one.

### The pane nobody watched

The sweep walks worktrees that exist. A worker whose worktree was deleted — merged and cleaned up, or lost to a Herdr restart — produces no row and no error, which is indistinguishable from "it finished and was reclaimed".

Measured 2026-10-05: `fleet-keepalive.py` logged `4 needing attention`, and `notify-coordinator.py` printed `all clear. No action needed` 0.2 seconds later, from the same run. Two sentences that cannot both be true, and the coordinator was told by one of them that there was nothing to see.

So the all-clear is now conditional on every recorded worker having been swept — it reads the same manifest the keepalive reads, so the two components cannot disagree about whether the fleet is whole. A ghost gets its own report, naming the pane and routing to `cleanup-workers.sh`.

The real fix is reclaiming the worktree, not reporting the ghost. Reporting it is what stops the report from lying; it is not what makes the fleet healthy. A branch already merged into `main` is not a worker, and leaving it in the manifest is what created the ghost.

### The transition, and the dry run that mutated state

Two smaller things the same pass turned up:

- The all-clear promised "the one you will get" and was wrong. The set goes ready → empty → ready → empty as tickets are merged and dispatched, and each transition into all-clear is legitimately news. It now reports a transition rather than promising silence forever.
- `--dry-run` was writing the state it had just computed, so the next real run believed a wake-up had already been sent. A dry run that mutates state is worse than no dry run, because it moves the machine you were trying to inspect.

The wake-up body restates the merge discipline rather than assuming it is remembered: run the gate, merge with `-F`, confirm `--is-ancestor` against the ref actually merged, close, and dispatch in the same turn.

**The rule it earns: an all-clear is a claim about health, not readiness — and it may only be said when every recorded worker has actually been looked at.**

## Reading a table is a skill with a failure mode

A wide table invites reading a number out of the wrong column and building a conclusion on it. This happened: a sweep printed `working 4 1 0 MERGEABLE`, I reported "MERGEABLE with a dirty tree, which contradicts the documented criteria", and went looking for a bug in `fleet-watch.py`. There was no bug. The `1` was the *behind* column of a different row; `fleet-watch.py:171` said `elif dirty: IN-FLIGHT` and was correct. The tool was right and I was reading it wrong.

The cause was not carelessness about the tool. It was fusing two observations taken at different times: the sweep said `dirty=0`, then a separate `git status` thirty seconds later showed a file the worker had started editing, and I joined the two into one false claim.

Three rules, each from that:

1. **One command, one observation.** A claim gets exactly one command behind it. If a claim needs two facts, run one command that prints both, together. Never assert a single fact from two commands run at different moments — the gap between them is where the false claim lives.
2. **A verdict must carry its own reason.** `fleet-watch` now prints why on each row (`IN-FLIGHT  4 uncommitted file(s)`), so nothing has to be inferred by cross-reading columns. It also recomputes its own verdict from the recorded numbers by a second independent expression of the rule, and prints `TOOL-INCONSISTENT` and a warning banner if the two disagree. That guard is mutation-tested: making `assess()` claim `MERGEABLE` on a dirty tree produces the banner. If you see `TOOL-INCONSISTENT`, fix the script — do not act on the row. A tool that cannot vouch for its own output makes every other row something to double-check by hand.
3. **Distinguish suspicion from finding in the words.** "That looks inconsistent" and "the criteria say X and the row says Y" are different claims. I asserted a defect from one glance at a table. The correct sequence is: notice it looks wrong → say it looks wrong → read the code or re-run the check → then either report the defect or report that there wasn't one.

The general form, the same rule as everywhere else: check that the thing which should have stopped existing did. Here what should have stopped existing was my reading of the row, not the verdict. A number I have not checked against its own header is an unverified input.

**The rule it earns: one command, one observation; a verdict carries its own reason; and a suspicion is not a finding until the code or the check has been read.**

## Four learned rules, not preferences

Each of these exists because I got it wrong in this project. They are here so the mistake is not repeated, not so the rule sounds authoritative.

### 1. A merge frees a slot

Dispatch the next worker in the same turn as the merge. Merging and then reporting, without refilling, is the default failure at any ceiling. A run of five consecutive merges left the fleet at three of fifteen because I treated the merge as the end of the turn's work. The merge is not the last step; the next dispatch is. If nothing is genuinely unblocked, say so in the same breath — but check first, because there is almost always something.

### 2. Agent status is not a delivery signal; the gate is

`done` has meant: finished and green; finished with 353 lines uncommitted; finished with 11 files dirty; and finished with code that does not compile. `idle` has meant both "waiting for a go-ahead with good findings" and "crashed". So:

```
GOPROXY=off go vet ./... && GOPROXY=off go test ./... -count=1
git -C <worktree> status --porcelain      # empty?
git -C <worktree> rev-list --count main..HEAD   # non-zero?
```

A worker with zero commits and N files in flight is the ambiguous case. Read the pane with `herdr pane read <pane>` before concluding anything — that is how two sets of good findings were recovered from workers that looked idle. If it stopped mid-edit, send it back with the narrowest possible ask: usually just `git add -A && git commit && git push`. Do not restate the ticket.

And the fully empty case is a third state, not a worse version of the second. Measured 2026-10-06: two workers reported `done`, and both had zero commits *and* a clean tree — so neither `NOTHING` (mid-edit) nor `MERGEABLE` describes them, and both columns being zero is what distinguishes them. The panes showed each had read its brief, printed one line of orientation — *"I have what I need on the ports walk. Now the repo layout and gate:"* — and stopped. No stash, no other branch, nothing in the reflog: the work had not started.

A clean tree is not evidence of tidiness — it is equally the signature of a session that died before its first edit. The narrowest possible ask is the right response and it is short: name the two facts that are missing, point at the brief, and say commit as you go. Do not restate the ticket and do not apologise for asking; a worker that read its brief and stopped needs the work restated far more than it needs a shorter message.

And "narrowest" means narrower the second time, not the same ask again. Measured 2026-10-06 on `media` #38: a worker reported `done` with a clean tree and two good commits, and its branch was red on exactly one line of `showboat verify demo.md` —

```
[ "$copied" = 0 ] || { echo 'a copy of a shared source is committed, so this transcript and the tree
                        disagree' >&2; exit 1; }
```

`copied` is 1, because the committed tarball is the fix. Two sends-back produced two more good commits and left that line red, because each ask described the problem and "re-record it" is the instruction a hurried reader gives. The third ask quoted the three lines and said what to replace them with.

So the ladder is by what kind of defect it is:

| the defect is | what to send |
|---|---|
| a concept the worker must hold | describe it — they may see further than the description |
| a specific line that must change | quote the line and the replacement — three lines of text beat three paragraphs |
| a guard that may be wrong | ask for a fixture — four times in one session the proof was a fixture and never an argument |

And do not count the attempts as the worker's failing. Those two nudges produced `027339a`, which took the pipeline off `fetch-shared-sources.sh` entirely — the right call, in the same spirit as the ticket. And when you do quote the line, quote the sentence above it too: the block also printed "installed as a link to the sibling", which the change made false, so a worker who inverted the assertion and left that echo has fixed half of it and `showboat` still records a claim that is untrue.

A transcript that prints a statement its own change made false is a document that is confidently wrong — and `contracts` named the general case when it wrote a new section for the same migration: *an absence recorded as a fact about the product stops being a fact the moment the product changes.*

### 3. An uncommitted tree is indistinguishable from no work

One worker's complete, correct, 24-file implementation survived two coordinator checks without landing because it never committed. It had one unused import to fix, and the fix was made — and the work was still invisible and one `git gc` from gone. Tell every worker to commit the moment it has something working and keep going. Put it in the brief.

### 4. Do not believe a guard you have not broken

A guard is a claim about a failure mode, and reading it proves nothing. Mutation-test it: add the sensitive field, delete the row the assertion depends on, and confirm it goes red for the stated reason. This has paid for itself repeatedly — the registry's reviewed-field-list gate, the vault's append-only log, a Go repository's drift check all turned out to be real. And when a mutation fails to break the guard, say so. Twice in one session I could not construct an effective mutation (Go does not resolve an unused `require`; a vendored path is not a `require`), and the honest merge message said "I have not demonstrated it failing" rather than claiming a guard I had not broken. Never report proof you do not have.

### 4a. A mutation that did not apply is not a finding — it is the worst one

The rule above covers a mutation that ran and failed to break the guard. The more dangerous case is a mutation that never took effect, where the guard correctly passes and the pass is read as evidence. All three of these happened on 2026-10-03, in three different repositories:

| What I did | What I concluded | What was true |
|---|---|---|
| wrote `vcs_probe.go` | "the gate is green" | it was never a test — Go wants `*_test.go`, so nothing ran |
| added non-string fields to a model | "the guard fired, exit 1" | build failure — the guard never ran |
| replaced `dagger call ci lint` | "the guard did not fire" | that string does not exist in the file; `replace` was a no-op and the guard was right to pass |

So three checks before a mutation result means anything, each cheap:

1. **Did it apply?** Assert the file actually changed — `assert s != before` in the edit, or re-read the line you edited. A silent `str.replace` miss looks exactly like a passing guard.
2. **Did it compile?** `go build ./...` (or the language's equivalent) and read the exit status. A build failure exits non-zero, so `exit=1` from a mutated tree is not evidence the guard fired. Grep the output for the guard's own failure message; do not read the exit code alone.
3. **Is the failure the stated one?** A guard that fails to compile has not been tested. Only `--- FAIL: <the guard's own name>` with its reason is evidence.

The general form: a passing guard and an inert mutation are indistinguishable from outside, so the burden of proof is on the mutation, not the guard.

**The rule it earns: a merge frees a slot, status is not delivery, uncommitted work does not exist, and a guard you have not broken — or a mutation you have not proved applied — is not evidence.**

## Interference is not file overlap

Before starting worker N+1, run the interference check and start it anyway unless it actually fails. "I should check first" is not a reason to check; the check is the work. The check asks: same repository (fine only if the areas are disjoint — name the files each will touch), same files (serialise), a real dependency (serialise, set `status::blocked`, note the blocker), same fixtures, seed or golden path (serialise), and an exclusive resource (serialise per the grants table, with a scheduled grant).

Files are not the whole question, and getting that wrong breaks `main`. `media` #10 (golden path) and #6 (CLIP benchmark) touched genuinely disjoint files and passed the check on every row — and still broke the build. #10 added a face proposal to the seeded estate, because the golden path needs one to demonstrate a photograph nobody has confirmed. #6's benchmark asserted a census count, with a comment recording that the number was a finding about that machine.

So: a count asserted over a shared fixture is a property of the fixture, not of the component under test. Ask what your ticket writes that another ticket also reads — seeds, `testdata/`, golden paths, the synthetic estate, a fixture census, a port number, a registry entry — and if the other ticket asserts on it, serialise even when no file is shared.

And run the gate, then act on the number. I read `fails=3`, merged, and closed the issue. The gate being red is not a formality to note in passing; it is the gate. When the count is not zero, the merge does not happen — not because the gate is usually right, but because a number I did not act on is a number I stopped reading, and a check you do not act on is one you have already decided is optional. The revert is in the history with that reasoning in it, so the next person sees why rather than assuming the revert was a mistake to undo.

Disjointness is a merge-order fact, not a hope: two tickets that each touch `go.mod`, a `Makefile`, a shared `types/` package or a `PROJECT.md` are not disjoint, however different their titles look. `docs` tickets are usually disjoint from `code` tickets in the same repo, and whole repositories are always disjoint from each other.

Two tickets in one repository conflict even when their subjects differ, and the subject is what makes you think they don't. Measured 2026-10-06: #34 (derive every port) and #23 (one browser session per run) were dispatched into `contracts` simultaneously, and both edited `scripts/verify-golden-path.sh` — the port migration rewrites the preamble the session work rewrites, so the file collides while the tickets look like they are about different things. Three files conflicted.

The conflicts were disjoint additions, so keeping both sides was right twice. The third time it was wrong: `main` carried the earlier spelling `b() { "$BROWSER" …; }` and taking it reintroduced the exact bypass #23 removes — a direct CLI call can never reach `browser-grant.py`'s exit-75 refusal, so a collision is cross-talk rather than a refusal. The repository's own guard caught it:

```
names the browser executable 4 time(s) (1 claim, 1 assignment, 1 probe, 1 unclassified),
and this reader expects exactly three. Either the script changed shape and the permitted list
here has to be revisited, or this reader stopped recognising a form it used to — and a guard
that cannot see the file it is guarding cannot fail on it.
```

A conflict resolution is an edit, and it gets no more trust than an edit. It is the inert-mutation case from the other direction: the work was real, the merge succeeded, and the result was quietly wrong in the one place that mattered. Never resolve a conflict by taking one side. Read both sides, keep both, and then run the target's own guards — because the thing that catches a bad resolution is the guard the resolution was about to weaken.

So the concrete rule this earns: a per-repository migration does not license a second worker in that repository. One migration, one worker per repository, and the next ticket waits for the merge.

Parallel workers never share a working tree: give each its own git worktree and keep the areas disjoint (`internal/store` vs `web/`) so the merge stays trivial. Create the next worker's worktree only after the previous merge has landed on `main` — never in the same batch as a merge, because a worktree created mid-merge branches from the old `main` — and check `git merge-base main issue-<iid>-<slug>` before briefing the worker.

**The rule it earns: interference is about what tickets read and write — fixtures, ports, seeds, migrations — not about whether their file lists overlap.**

## Guards and their read sets

### A false positive that arrived with an alarming story

The renderer and the guard that reads its output failed in opposite directions, and neither test could see the other. The guard reported three services `INSIDE-REPO` — `agent-hub`, `player-registry`, `social-studio` — every one of them a correct `additional_contexts:` entry naming the meta repo on purpose: the whole point is to hand a Dockerfile the registry. `host_paths()` returned `(service, path)` and dropped which key the path sat under, so the bind-mount rule was applied to build contexts. The consequence it printed with — *OPA serves zero policies while reporting healthy* — belongs to a `volumes:` entry and cannot happen to a build context at all.

A false positive that arrives with an alarming story is worse than one that arrives quietly, because it is believed. Acting on that report would have broken three correct build contexts, and the only reason it did not is that I resolved the paths instead of reading the finding: `../../meta` exists and holds `apps.json`.

Same string, opposite verdicts, decided by the key it sits under. The test asserts both directions, because a fix that silences a finding is indistinguishable from a fix that deletes the check unless the defect cases are held too.

### A read set is a floor, not the job

Every guard here reads a fixed set of files, and a defect outside that set is invisible for exactly as long as nobody goes and looks. `check-demo-ports.py` reads `demo.md`, `scripts/verify-golden-path.sh` and `scripts/player-360.sh`; migrating all nine repositories to derived ports found a port-binding file outside that set in every single one that was checked — `dev-stack.sh`, `verify-container.sh`, `demo.sh`, `capture-journey.sh`, `capture-way-in.sh` — each of which had been typing a port for as long as the rule was being written, with nothing complaining because nothing read them.

A repository is not finished because the guard stopped reading.

### Text versus value

The deeper form has now appeared three times in three different shapes — a guard that reads the *text* of an expression cannot see an expression that is wrong in its *value*:

| what hid the defect | why every check above it was green |
|---|---|
| `28000-28099`, the unallocated gap | the guard read the wrong region — no application owns it, so no scan reaches it |
| `HOST_PORT="${1:-${VERIFY_PORT:-28085}}"` | the number sat inside an expansion, and `PORT=1234` does not look there |
| `SESSION="...-golden-path-$$}"` — one brace too many | `bash -n` balanced it and the pid guard saw `$$`; only `bash -c 'source it; echo $SESSION'` saw the literal `}` in the name |

So: ask what the thing *is*, not what it is written as. Source the script and print the variable; resolve the expansion and look at the value; bind the port and read what the OS reports. A guard that inspects a file is reading a description of the behaviour, and the description was wrong in all three.

Three shapes the same migration found, one per repository, and all three invisible to a grep for `NAME=1234`:

| Shape | Where | Why the search misses it |
|---|---|---|
| a series that reads as a formula | `portal`: `18200`–`18243`, `+10` per block | arithmetic looks like a derivation; every member was typed |
| a number inside a default argument | `contracts`: `${VERIFY_PORT:-28085}` | `PORT=1234` does not look inside a `${…:-…}` expansion |
| a number the kernel may lease | `opponent-brief`: `38086` = `18086` with the digit changed | it is in neither band, contains the app's own port, and looks derived |

Arithmetic reads as a derivation. A series incrementing by a round number every block is the most convincing typo there is. Ask what computes it; if nothing does, the pattern is decoration.

Ask what *owns* the number, not merely whether it is free. `net.ipv4.ip_local_port_range` here is `32768`–`60999`, leased to outgoing connections from any process on the host — so a typed port in the `38xxx` strip is a number the kernel can hand to somebody else's `curl` or `docker pull` underneath a run. "Free when the script ran" is not the same fact as "free by construction". Every allocated block sits below that floor.

And the unallocated gap is invisible by construction. `28000-28099` and the `38xxx` strip belong to no application, so `SAME PORT` and `IN ANOTHER'S BLOCK` — which only compare *owned* blocks — cannot see a defect there. Four repositories found it independently, from four directions. `TYPED` is the most reassuring thing that guard can say, and in the gap it is the wrong one.

### A guard needs a positive control

One of `press`'s mutants proved the point: the grant guard asked whether the body contained a presence test *and contained a `fail` somewhere*, so a script whose grant resolution was softened to best-effort while four other `fail` calls stayed satisfied it. The fixed guard reads the shape the shell reads — the test line *and the fail attached to it* — and carries the softened version as a positive control, because a guard whose only test is "does the bad thing fail" cannot tell a guard from a guard that is switched off.

One more thing decides whether a guard means anything: `fetch-shared-sources.sh` skips a target that already carries a `.git`, so "provided by the fetch" and "current" are different facts — and a guard that resolves one copy of the meta repo and refuses on it passes or fails depending on which machine runs it. Try both paths, or assert neither is assumed.

So: grep the repository for the thing the guard looks for, and fix what the guard misses. State the read set as a floor in the guard's own `--explain`, so the next worker's assumption is visible rather than implicit.

**The rule it earns: a guard's read set is a floor and its text is not its value — and a guard that cannot see the file it is guarding must not be trusted to have found nothing.**

## The merge gate

### `MERGEABLE` is a prompt to gate it yourself

The merge gate is the coordinator's own run, and it is not the keepalive's verdict. `MERGEABLE` from `fleet-watch` means the branch has commits, a clean tree, and the gate it knows how to run is green. It is a prompt to go and gate it yourself. On 2026-10-03 the keepalive reported four branches green and all four were red under a gate that included `showboat verify demo.md`.

Run it, do not type it. `scripts/merge-gate.py <worktree>` is that gate as a program:

```bash
python3 scripts/merge-gate.py <worktree>   # 0 green, 1 red, 2 unverified, 3 the gate is broken
```

It runs the shared sources first, then the repository's own gate, then `showboat verify demo.md` — in that order, because `go vet` and `showboat verify` both read `../shared/` and `make check` creates `web/node_modules`. It never merges, never closes an issue, never pushes and never re-records a transcript block; that is a discipline for whoever merges, and a tool that guessed which blocks a merge invalidates would re-record the wrong ones confidently.

So the merge gate is: run `merge-gate.py` and read its exit status, which it also prints (`showboat verify` exits 0 in silence, so "I ran it and saw nothing" is not evidence); the mutation proofs the change claims, if it claims any; re-record the transcript blocks this merge invalidated, in the same merge — a transcript is a snapshot of a run, so a merge that moves the thing it snapshots has to move it too; then `git merge -F`, `git merge-base --is-ancestor`, push, close.

### Four states, not two

A green from the repository's own gate alone is not a green from the merge gate. `demo.md` is the third step, and it reports four states rather than two: `VERIFIED`, `STALE`, `NO-DEMO` and `NOT-VERIFIED`. Two of those are why it is a program:

- A repository with no `demo.md` says so. `NO-DEMO` is printed. An absent line reads as a pass, which is the defect.
- A `demo.md` recording zero exec entries is `NOT-VERIFIED`, not green. `showboat verify` exits 0 on it, in silence, having compared nothing — measured on showboat 0.4.0. So the gate asks `showboat extract` what the document records, and zero entries is exit 2.

`NOT-VERIFIED` is where the sweep and the merge gate deliberately differ. `fleet-watch` folds it to `GREEN` with the reason stated, because a sweep that reports every pane unverified because `showboat` is not installed is a sweep nobody reads — while a merge authorised on a transcript nobody checked is the failure the merge gate exists against. Two folds over one verdict, asserted in `scripts/test_merge_gate.py` so neither can drift unnoticed.

### A transcript may not depend on unrecorded machine state

Measured 2026-10-06 while landing #39's tarballs: `pnpm install` prints `Already up to date` only when `node_modules` is already current, and a committed tarball removed the resolution step that used to make a warm tree the normal case:

```
warm tree (node_modules current)  ->  "Lockfile is up to date, resolution step is skipped"
                                      "Already up to date"
cold tree (no node_modules)       ->  the same first line, then Progress:/Packages:/+…, and NO
                                      "Already up to date"
```

A transcript recording that line verifies on a warm tree and goes red on a fresh clone — and a fresh clone is the one state the document exists to be provable in. Recording a line whose presence depends on unrecorded state trades a green for a lie.

There are two correct answers and the choice is per block, because the property is "verifies in a fresh clone", not "uses the same filter list": either the output is never recorded (as in `opponent-brief`: `pnpm --dir web install --frozen-lockfile >/dev/null 2>&1`), or the output is recorded and the normaliser removes what depends on the state (as in `portal`: a bare install, and `norm()` filters `Done in`, `Packages:`, `Progress:`, `Resolved:`, `Already up to date`, `dependencies:`, `devDependencies:`). Measure the cold tree rather than guessing which lines are conditional — `portal`'s last pair were added because they are pnpm's install-summary headers and appear only when pnpm does real work. And check both directions: after the filter, `make api-check` normalises byte-identically warm and cold, and that comparison is the evidence.

### Merge first, close second

Four times in one session the coordinator closed an issue and then discovered the merge had failed, which leaves a closed issue with no code behind it. The merge is what makes the issue true; closing is a consequence of it, never a parallel step. After every merge confirm with `git merge-base --is-ancestor <branch> HEAD` before closing anything.

Verify the merge against the branch you actually merged, not `origin/<branch>`. This check has produced a false negative as well as false positives, and the cause is the same: a branch that was rebased cannot be force-pushed (the pre-receive hook blocks it), so `origin/<branch>` still points at the pre-rebase tip while the merge used the local one. `git merge-base --is-ancestor origin/issue-N HEAD` then reports "not merged" about a merge that is on `main` and pushed. Use the local branch, or better the merge commit's second parent:

```
git merge-base --is-ancestor issue-N-slug HEAD      # the branch you merged
git rev-parse --short HEAD^2                         # equals the branch tip for a --no-ff merge
```

A false negative is the safe direction — it stops a close that should not have happened — but it trains you to distrust the check, and a check you distrust is a check you stop running.

### Backticks in `-m`, and the rebased branch

Write merge messages to a file and pass `-F <file>`. A message in `git merge -m "..."` is a double-quoted shell string, so backticks inside it are command substitution. A message containing a backticked word runs it, prints `command not found`, and hands git a mangled argument list — the merge then fails quietly enough to look like it worked, because the next command in the chain still runs and the output still looks like a success. After any worker merge, verify the message survived: `git log -1 --format=%B <sha>` and read it. A mangled message is a small loss, but an unnoticed one means the habit is absent when the message matters.

A rebased worker branch cannot be force-pushed — the pre-receive hook blocks it, so a rebase makes the branch unpushable. Do not merge the remote branch in to reconcile it: the two histories contain the same changes on different bases, and you get a second `go.mod` conflict plus a merge commit recording duplicate work. Instead push the rebased branch under a new name (`issue-<iid>-<slug>-v2`), merge that, and let the pre-receive hook keep the old branch. Merging a branch name that only exists on the remote fails with "not something we can merge"; fetch `refs/heads/*:refs/remotes/origin/*` rather than relying on the default fetch refspec.

And watch for workers committing to `main`. A worker that writes to the main checkout instead of its worktree is not a merge problem, it is a fence failure — check `git reflog` on the main checkout when a branch's base looks wrong.

**The rule it earns: the merge gate is the coordinator's own run — merge first, close second, and verify the merge against the ref you actually merged.**

## The reclaim sweeps

A merged ticket leaves a worktree folder, a local and a remote branch, and a Herdr workspace behind. The coordinator reclaims them on demand with `scripts/cleanup-workers.sh` — dry-run by default, `--yes` to apply. Nothing runs automatically as part of a merge; the `✅` pane mark is the hand-off into this sweep. Coordinator only: a worker never runs it, not even on its own worktree, because it is executing inside the directory the sweep removes.

Each worker workspace is only reclaimed when all of these hold: the worktree's repo root is this repository's root; it is a linked worktree; its tree is clean; the branch is an ancestor of `main`; there is no open MR for the branch; the issue is closed; and no agent in the workspace is `working`. Removal order is `herdr worktree remove` → `git worktree prune` → `git branch -d` (never `-D`, so git itself refuses an unmerged branch) → `git push origin --delete` → `herdr workspace close`, each step warning and continuing rather than aborting the sweep.

### Scoping, and the sweep that reported other repositories' work

The sweep is scoped to one repository, and `--iid` with it. `herdr workspace list` is not scoped to a repository — `herdr worktree list` is — so the candidate filter reads `.worktree.repo_root` and compares it to the repository root itself. Without that filter, `--repo` decided only which checkout to read, and every repository's sweep named every other repository's workers: measured on 2026-10-03, a dry run from a meta worktree reported 19 candidates, none of them this repository's. The foreign workspaces are reported as a per-repository count, never listed — a table of another repository's running workers is the defect, not a longer version of it.

`--iid` is repo-scoped and says so in `--help`, because `media`, `identity` and `press` all have a #2, and an unscoped `--iid 2` means "ticket #2 of some repository" in fact and "#2 of this one" only by intent. A workspace with no `worktree` object cannot be attributed to any repository; it is reported in its own section, never filtered out, because hiding it to make the filter tidy trades a false report for an invisible one.

`nothing to do.` was removed from the report. The old closing line printed it after listing 19 candidates. *Found nothing*, *found N and reclaimed none*, and *found N that all need a decision* are three different facts and now print three different sentences; every section carries a count, and every row carries its workspace id — a refusal without a workspace id cannot be acted on. Sessions are never deleted: the transcript lives in `opencode.db` independently of the pane and stays exportable and recallable, and the sweep prints the session id it preserved. Anything the predicate rejects — an abandoned worker, a dirty worktree, an open MR — is reported, not removed; discarding work is the owner's call, not the sweep's. Stale registrations from a crashed worker are pruned, and the sweep never `rm -rf`s a path it did not resolve as a worktree of this repository.

### The workspace that was never closed

A merged agent is not reclaimed until `herdr workspace close <ws>` has run and reported. Worktree gone, branch merged, issue closed — but workspace still open is not done, and that is the leak the second sweep exists for.

`cleanup-workers.sh` classified it as *refused*, and refused for a reason that was both invisible and untrue. It read the branch from `WT_BRANCH`, which git populates from `git worktree list` — so a workspace whose checkout had already been deleted or pruned had no entry, and the sweep reported `no branch registration for <path>`. That reads as "this is somebody else's problem"; it is actually the one bucket nothing else can reach. Measured on 2026-10-03: 12 such workspaces open, after 28 had been closed by hand.

The fix keys off the path on disk, not on `is_linked_worktree` and not on a registration. A linked worktree whose checkout no longer exists is the case, and it is neither reclaimable nor refused: there is nothing to remove, only a workspace left open. It is refused only when a branch git can still name (`issue-<iid>-*`) is not merged into `main` — using the sweep's existing `merge-base --is-ancestor` and its existing MR/issue pair, because a new check in a second place is a check that will drift.

The close is delegated, not duplicated. `--close-gone --yes` hands exactly those workspaces to `close-gone-workspaces.py --only …`. That script owns the decision and its refusals with 28 tests behind it; a second implementation in bash would be free to disagree with them, and two pruning implementations that disagree is the failure this family keeps hitting. Delegation is opt-in: one sweep deletes branches and the other closes workspaces, and both acting unattended in a single command is a bigger claim than either makes on its own.

```sh
python3 scripts/close-gone-workspaces.py          # dry-run, prints the table
python3 scripts/close-gone-workspaces.py --yes    # close them
python3 scripts/close-gone-workspaces.py --audit  # classify every workspace, close nothing
```

It needs no git and no repository: a directory that no longer exists cannot be a live worktree, so it is fleet-wide by construction and cannot be confused by which repo it was run against. Order it after `cleanup-workers.sh`. It also takes `--only <workspace>…`, which narrows the candidate set and nothing else: every refusal still applies to every named workspace, and a `--only` naming a workspace Herdr does not know is reported, not ignored — a filter that silently matched nothing looks exactly like a sweep that found nothing to do. Refused when the directory still exists; when an agent in it is `working`; when any pane's runtime answers `herdr pane read`; when the workspace is the coordinator's (`HERDR_WORKSPACE_ID`, resolved per run, never hardcoded); or when a pane is in `.fleet-state/fleet.json`.

### `herdr pane read` is not JSON

`herdr pane read` returns raw terminal text, not JSON. A probe that runs `json.loads` on it gets a `JSONDecodeError`, and a caller that reads that exception as "no error returned" concludes every live pane is dead — one step from closing the coordinator's own workspace. That is not hypothetical: it happened while this was being written, and the dry run caught it. `parse_pane_read()` keys on a JSON payload containing an `error` key, and `test_pane_read_is_never_assumed_to_be_json` holds it.

### Re-verify at the moment of the close

Another coordinator session in the same workspace reaps worktrees concurrently; the fleet moved from 92 workspaces to 40 during a single audit. So the live-pane and `working` checks run per workspace immediately before `workspace close`, not from an audit taken earlier.

**The rule it earns: a reclaim is only complete when the workspace is closed and reported — and every check runs under the repository's own scope, at the moment of the decision.**

## Exclusive resources

A ticket that needs a shared, stateful resource does not imply permission to touch it now. Exactly one AI session may hold each exclusive resource, and only while the coordinator has granted it. The pattern is a resource table per project: live stack (the running compose stack, verify scripts against real services, anything binding the public ports, the main checkout's data directory), upstream soak (long real-data runs and scripts that rewrite committed fixtures), toolchain (shared build toolchains, container images and caches every tree reads), browser (the `agent-browser` daemon — every `verify-golden-path.sh`, every browser walkthrough, anything driving a real page), and the dagger engine (`dagger call` runs). Each has a marker pair — `READY-FOR-LIVE:` → `LIVE RELEASED — <state>`, and so on.

The grant table is only as good as its last entry, and a resource nobody wrote down is a resource everybody collides on. Two were missing and both were found the same way — a gate red for a reason that was not the code.

### Dagger contention reads as an OOM

Concurrent `dagger call` runs contend for one engine and report `buildctl dial-stdio ... exit 137`, which reads as an OOM and is not one. The symptom being mistaken for an OOM is what makes it dangerous: the reflex is to free memory, and the cause is a lock.

### Browser session names cross-talk

`agent-browser` is one browser instance per *session*, not one for the whole workspace. The CLI keeps `/run/user/1000/agent-browser/<session>.sock`, and its own help says *"each session has its own browser instance with separate cookies, storage, and state"*. So eleven repositories running their golden paths do not contend with each other, and the demo evidence is not only reproducible one at a time. That was believed on 2026-10-03 and measured the same day; `docs/browser-grant.md` holds the numbers.

What does collide is a session name, because a run's page, viewport and daemon are shared by anything using that name. Two runs on one name were measured silently replacing each other underneath a walk — one read `h1="AAAA"`, the other changed it to `"BBBB"` without issuing a command, and the first's next assertion was against the second's page. That is a false red on a user-visible assertion with no lock and no `EAGAIN` involved.

So the rule is one session name per run, not one golden path at a time. Three things share a name and are wrong for it: every script's `SESSION="<repo>-golden-path"` is a constant, so two workers in one repository collide; `media` and `media-dagger` both use `media-golden-path`; `player-registry` and `reg-p360` both use `registry-golden-path`. Every golden-path run still takes the grant, including a worker's own local run — the unbidden local run is the common case, not the exception.

### The browser grant has a mechanism, and it is not the grant

`scripts/browser-grant.py` is additive: it never authorises a run. The coordinator-held grant answers *may this worker touch the browser at all*, and it is a human decision a person can see, override and re-order. The script answers the narrower question a machine can answer — *is the session I am about to touch mine alone* — and it reports what it cannot verify rather than enforcing it: a run with no `--granted-by` is reported `UNGRANTED` by `status`, not blocked; `force-release` refuses a live holder without `--force-live`, and appends every override to `overrides.log` with its reason; nothing queues silently, default `--wait 0`, so a collision refuses with exit 75 and the command never runs — no assertion is evaluated, so nothing can be falsified. A worker blocked inside a verify script cannot answer a human and is lost to a Herdr restart, so refusing beats waiting; `--wait` is for a worker racing itself. A refused run names the holder, so the fix is a different session name rather than a rerun:

```bash
python3 scripts/browser-grant.py status          # HELD / STALE / FREE, and UNGRANTED
python3 scripts/browser-grant.py run --session "$S" --label "press #7" --granted-by coordinator -- scripts/verify-golden-path.sh
```

The failure gets a name, and the name is not always the same one. `agent-browser` reports a busy daemon as `exit status 1` with the reason underneath, and it arrives as an ordinary test failure. `classify` turns it into one of five verdicts, and the last is the point:

| Verdict | Meaning | Retry? |
|---|---|---|
| `PASS` / `FAIL` | ran; evidence about the code | n/a |
| `BUSY` (75) | transport token, and this run did **not** hold the session — **not evidence** | safe |
| `BROWSER-FAULT` (76) | transport token while this run **held** the session, so contention was impossible: a sick daemon | **no** |
| `UNDECIDED` (78) | no exit status, or nobody checked the lock — a guess refused | n/a |

`UNDECIDED` exists because `agent-browser` confirms success in prose, so its output alone is not a failure signal. And `os error 11` is matched on word boundaries: a plain substring test reads `os error 111` (a bound port) as daemon contention.

### Grants: prepare first, ask, release cleanly

Prepare off-resource first: reading code, unit tests, `go vet`, `gofmt`, the SPA build and `make api-check` are always allowed. Starting live services, real-data soaks and toolchain rebuilds are not, until granted. A waited-on worker that is ready for the resource comments `READY-FOR-LIVE: <plan + exact commands + what it will start/replace>`, ends its turn idle and waits; the coordinator prompts it "<resource> granted" in the same session once the resource is free. This is a scheduled grant, not a blocker — do not set `status::blocked`. Before the resource moves on, the holder restores a known-good state and says so, comments `<RESOURCE> RELEASED — <state>` and stops. The coordinator owns the queue: orders grants by priority, verifies release state before the next handoff, and never lets two holders overlap.

**Standing grant for a program of work, when the owner gives one.** For named work the coordinator may answer `<RESOURCE> granted` itself — a per-run owner approval is not required. Nothing else relaxes: one holder per resource at a time, the marker names the plan and the exact commands, the holder restores a known-good state and comments `<RESOURCE> RELEASED — <state>`, and the coordinator never lets two holders overlap. Two things stay the owner's: anything that would touch a third party's environment, and anything that publishes or spends money. The coordinator also supplies what a deployment deliberately does not default (an origin value, a seed credential), and a grant to a worker names the stack state it hands over: what is up, what was rebuilt, from which head.

**The rule it earns: one holder per exclusive resource, granted by the coordinator, prepared before and released after — and a resource nobody wrote down is a resource everybody collides on.**

## Working at the coordinator's desk

### Parallelism is the default

Prove serial, do not assume it. The coordinator's job is to keep several workers busy at once, not to walk a queue one issue at a time. A single running worker while the coordinator sits waiting on it is a coordination failure, not caution. The coordinator never blocks its own turn on a worker: prompt it, confirm the pane went to `working` with `herdr agent wait <pane> --until working --timeout 120000`, then return and manage the other workers. A long wait turns the coordinator into a supervisor of one worker instead of a coordinator of several. If a wait times out that is not a failure — re-check `herdr agent get` and carry on. Serialise only what genuinely conflicts — same file, same worktree, same exclusive resource, or a real dependency — and in that case say which pair and why, in a note on the blocking issue.

Be proactive: when something becomes schedulable, schedule it. Do not wait to be told.

| situation | what happens |
|---|---|
| a worker finishes and its branch merges | dispatch the replacement in the same turn, before reporting the merge |
| a worker's verdict is `GATE-RED` or `NOTHING` and you send it back | you freed a slot — fill it in the same turn |
| the fleet reports all-clear | that is the strongest signal there is, not an absence of one: go and find work |
| the cron nudge arrives | it is a backstop, not the trigger — if the nudge is what made you look, the nudge is doing the coordinator's job |
| you notice a dispatchable ticket while doing something else | dispatch it then, not at the end of the session |

A nudge that is needed in order to act is a failure of the nudge's owner. On 2026-10-05 the fleet sat at 1 of 15 workers with 25 open issues across nine repositories, and the reason it sat there was that the coordinator dispatched on being *asked* rather than on noticing. The all-clear had fired; the all-clear is the loudest signal the tool has. So the test of a turn is not "did I finish my task" but **"is anything schedulable that I did not schedule."** If the answer is yes, the turn is not finished. Sitting idle at a quarter of the ceiling is a failure state, not a quiet one — and `agent_status` will never tell you, because an idle coordinator and a busy one look identical from outside.

### The ceiling

The fleet is sized up to 15 concurrent workers. The owner authorised up to fifteen AI agents running in parallel — treat that as the working ceiling, not a target. Being under it is the default failure — sitting at three because dispatching more "feels like a lot" is the exact coordinator failure the proactive rule names. Fifteen is a limit on damage, not a quota: an interference check that genuinely fails is still a reason not to start, and 7 well-chosen workers beat 15 with two forced into the same file.

The ceiling was raised from 10 to 15 on 2026-10-02 by the owner. The reasoning that justified 10 still holds — the number bounds how much can go wrong at once, and only the coordinator's judgement of interference bounds quality — so raising it does not relax the interference check. It changes one thing operationally: more of the family is in flight at once, so branches fall behind `main` more often, and rebasing on return is the normal path, not the exception. A ceiling you never reach is a ceiling that bought nothing.

### Never claim the backlog is empty without a survey that can see it

Filtering on `status::todo` hides every `status::blocked` and `status::doing` ticket, and on 2026-10-03 that filter reported a nearly-empty backlog while 21 work items were open — ten of them in the meta repo alone, including both Dagger pipeline tickets. A scarcity claim that stops dispatching has to be earned by counting *open* issues, not open issues carrying one particular label.

The error has run in both directions. Measured 2026-10-06: `glab issue list -A` returned 39 items and I read it as a 39-item backlog, while `GET /projects/:id/issues?state=opened` returned 13. `-A`/`--all` includes closed issues, so a backlog survey on it is inflated roughly threefold and every "there is nothing left to do" drawn from it is wrong. The two mistakes are the same mistake:

| what you read | how it lies |
|---|---|
| `glab issue list -A` | counts **closed** issues as work to do |
| `glab issue list --label status::todo` | hides `doing` and `blocked`, which is most of a live fleet |

So the survey is `state=opened`, and then read the labels:

```sh
glab api "projects/:id/issues?state=opened&per_page=100"   # then group by status:: label
```

A claim about how much work exists is a claim about a **query**, and the query is part of the claim.

### Count the guard's rows, not the plan you wrote

#34 was dispatched as "6 migrations" from the first brief onward, because the number was typed into a template and the template was then treated as the plan. `check-demo-ports.py` had said `9 TYPED` from the start. An ordinal in a brief is a label; a guard's count is a fact, and dispatch decisions are made from facts.

### Do not let a wrong belief stand unchallenged

One project was a reference implementation, not an audience for the product. It was described as the audience for weeks before anyone said otherwise, and every intermediate conclusion drawn from it was wrong in the same direction. A wrong belief about the world that was never checked is the failure this whole section exists to stop.

And a new or complex topic is never worked at the coordinator's desk — it is a ticket, and a worker works it. When a question, an investigation or a design arrives, the coordinator captures what it already knows — the measurement that justifies the ticket, the constraints that bound it — files the issue, and delegates, without being asked to. The coordinator reads enough to scope the ticket and stops there. Coordination itself stays at the desk (merges, fleet sweeps, filings, the estate's upkeep); anything a worker could be briefed to do is a worker's. This is the correction of 2026-10-07: a matching design was investigated at the coordinator's desk while the rule was that the desk files tickets.

**The rule it earns: the desk coordinates — dispatch from facts on the same turn, count open issues with a query that can see them, and put anything a worker could be briefed to do on a ticket.**
