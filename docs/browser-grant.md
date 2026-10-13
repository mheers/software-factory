# Why the browser grant is a partition and not a queue

Measurements taken on 2026-10-03 on the machine that produced the failure, using session names in a
private `ai-probe-*` namespace so no live worker's session was touched (two live worker sessions
were running throughout and were left alone).

## The failure being made distinguishable

Verbatim from a golden-path run in a member repository, driven by a Go test through the CLI:

```
agent-browser click button[title="Français"] failed: exit status 1
Failed to read: Resource temporarily unavailable (os error 11)
(after 5 retries - daemon may be busy or unresponsive)
```

`os error 11` is `EAGAIN`. The message is assembled by the CLI, not by the code under test, so it
says something about the transport and nothing about the brief. It arrives at the reader as an
ordinary Go test failure, because `t.Fatalf` cannot tell the two apart.

## What is actually shared

`agent-browser` keeps **one daemon socket per session**:

```
/run/user/1000/agent-browser/<session-a>.sock
/run/user/1000/agent-browser/<session-b>.sock
```

So "one daemon for the whole workspace" — the starting assumption — is **not accurate**. There is
one daemon *per session*, and sessions partition. Several things nonetheless shared a session name,
and each is demonstrably wrong:

| Sharing | Measured |
|---|---|
| same repository, two workers | every verify script used the same constant, `SESSION="<repo>-golden-path"` |
| different repositories | a repository and its variant checkout used one constant session name |
| different repositories | a repository and its second variant used one constant session name |
| a shell script and its own Go test | the script's `SESSION` and the test's `const browserSession` were the same constant — sequential within one run, so harmless alone |

## What I could reproduce, and what I could not

**I did not reproduce the `EAGAIN` failure.** Three mechanisms, all negative: 8 concurrent instant
`eval 1+1` on one session (0 busy of 8); 6 short probes against a session held by `wait 3000`
(0 busy of 6); two concurrent golden-path-style `close`/`open`/`cmd`/`close` cycles on one name
(0 busy of 18 outputs). So the daemon is **not** a mutex, and overlapping requests do not by
themselves produce `EAGAIN`. I am not claiming to know the precise trigger, and nothing below
depends on having known it.

**What I did reproduce is worse than the reported symptom, and it is silent.** Two runs sharing
one session name corrupt each other's assertions, and neither notices:

```
worker A opens data:text/html,<h1>AAAA</h1>, sets viewport 1500x1100
  A sees h1="AAAA"  viewport="1280x720"
worker B, SAME session name, opens <h1>BBBB</h1> and sets viewport 800x600
worker A asserts again, having issued no command of its own
  A sees h1="BBBB"  viewport="800x600"      <- CROSS-TALK PROVEN
```

The locale loop in an app's verify script (`scripts/verify-golden-path.sh:323`) clicks a language
button and then evals the heading. If another run's `close`+`open` lands between A's `click` and
A's `eval`, A reads the other run's freshly-loaded page and asserts a heading that is simply not
there — a **false red on a user-visible assertion**, and a rerun that passes is taken as proof. It
needs no lock and no `EAGAIN` to happen.

**And distinct sessions are fully isolated** — the positive control:

```
worker A on session ai-meta-19-iso-a: h1="AAAA" viewport 1500x1100
worker B on session ai-meta-19-iso-b: opens <h1>CCCC</h1>, viewport 800x600, writes localStorage
worker A asserts again:               h1="AAAA" viewport="1500x1100"   <- PARTITION PROVEN
```

`localStorage` isolation was *not* tested (the probe used a `data:` URL, where it is disabled), so
page and viewport isolation are measured and storage isolation is inferred, not claimed.

## The conclusion, and why it is not "build a queue"

Since distinct session names isolate and identical ones corrupt, **the hazard is removed by naming
each run's session uniquely**, not by scheduling runs. A queue serialises work that does not need
serialising, still permits the collision whenever the queue is bypassed, and blocks a worker where
it cannot answer a human or report `READY-FOR-BROWSER:`. The coordinator's own grant queue —
comment, end turn, wait for a prompt — is *visible and re-orderable by a person*, strictly better
than a `flock` nobody can see. So: **queue at the level of the grant protocol, partition at the
level of the session name**, and refuse rather than block at the level of the command.

The earlier sentence that demo evidence is "only reproducible one at a time" is **false as written**,
and predates measurement. Demo evidence *is* reproducible in parallel if each run names its own
session: repositories with distinct session names do not contend; the sharing pairs above do.

## Why the enforcement is additive and not a replacement

The coordinator-held grant (`READY-FOR-BROWSER:` → `BROWSER RELEASED —`) answers *"is this worker
allowed to run the browser at all"*, a human decision a person can see, reason about and override
when a worker wedges. `scripts/browser-grant.py` answers a strictly narrower question that a
machine can answer: *"is the session I am about to touch mine alone"*.

- it never grants. A run with no coordinator grant still gets refused on collision, and a held
  session with no `--granted-by` is reported as `UNGRANTED` — a visible protocol violation, not
  an authorisation. `status` reports HELD / STALE / FREE;
- it never queues silently. Default `--wait 0`, so a collision **refuses with exit 75** and the
  command **never runs** — no assertion is evaluated, so nothing can be falsified. A worker
  blocked inside a verify script is lost to a Herdr restart, so refusing beats waiting; `--wait`
  is for a worker racing itself;
- `force-release` clears a wedged holder by hand, refuses a **live** holder without
  `--force-live`, and appends every override to `overrides.log` with its reason.

A refused run names the holder, so the fix is a different session name rather than a rerun:
`python3 scripts/browser-grant.py run --session "$S" --label "<repo> #7" --granted-by coordinator
-- scripts/verify-golden-path.sh`.

**The failure gets a name, and the name is not always the same one.** `agent-browser` reports a
busy daemon as `exit status 1` with the reason underneath, and it arrives as an ordinary test
failure. `classify` turns it into one of five verdicts, and the fifth is the point:

| Verdict | Meaning | Retry? |
|---|---|---|
| `PASS` / `FAIL` | ran; evidence about the code | n/a |
| `BUSY` (75) | transport token, and this run did **not** hold the session — **not evidence** | safe |
| `BROWSER-FAULT` (76) | transport token while this run **held** the session, so contention was impossible: a sick daemon | **no** |
| `UNDECIDED` (78) | no exit status, or nobody checked the lock — a guess refused | n/a |

`UNDECIDED` exists because `agent-browser` confirms success in prose, so its output alone is not a
failure signal. And `os error 11` is matched on word boundaries: a plain substring test reads
`os error 111` (a bound port) as daemon contention.

A tool that could overrule a coordinator would be a replacement. One that shows the coordinator
where two things are about to touch the same daemon is an addition.
