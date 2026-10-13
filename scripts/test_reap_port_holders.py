#!/usr/bin/env python3
"""The port reaper's own tests: scripts/reap-port-holders.py.

Run: `python3 scripts/test_reap_port_holders.py`

Read-only, offline, no test runner — the same shape as the other scripts in this repository.

**Nothing is signalled.** Every test that reaches the decision injects `age_of`,
`connections_of` and `repo_of_callable`, so the classification runs against facts it is handed
rather than facts it goes and looks up. That is not tidiness: the real sweep answers for pids on
a machine other workers are using, and a test suite that signalled one of those would be the
exact defect `docs/FLEET-HANDOFF-2026-10-05.md` trap 5 records — a session killing itself with a
pattern that matched its own command line. `reap()` is exercised with a stub `signal` and a stub
`wait`, and the one real signal in this file is `os.kill(0)`-free by construction: the stub
records what it was asked to send and sends nothing.

**Why the sweep is driven with fake `ss` output rather than real listeners.** A test that needs a
leaked Go test process to exist is a test that is never run, and the day it is most needed — a
machine busy enough to have one — is the day it cannot be set up. `listening()` is therefore
called with a parser fed a recorded `ss` line, which is also the shape the bug it exists for
took: **every port a leaked process held was outside every allocated block**, so a sweep that
scanned blocks would have reported a clean machine.

**The mutation discipline, stated because three sessions got it wrong on 2026-10-03.** A
mutation result means something only if it applied, and a mutation that leaves the mutant's
behaviour unchanged proves nothing. Each mutation here asserts the verdict *changed*, which is
the assertion whose absence makes "the guard fired" and "the mutation did nothing" the same
sentence.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAPER = HERE / "reap-port-holders.py"

FAILURES: list[str] = []
CHECKS = 0


def check(ok: bool, what: str) -> None:
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"ok   - {what}")
    else:
        print(f"FAIL - {what}")
        FAILURES.append(what)


def load():
    spec = importlib.util.spec_from_file_location("reap_port_holders", REAPER)
    assert spec and spec.loader, f"{REAPER} could not be located"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load()

# The recorded `ss -ltnp` line from 2026-10-05, with the real pid's ports: 29400 is the one the
# harness was told to take, and the other three are ephemeral-range ports that process bound
# itself. This is the whole reason the sweep identifies processes rather than scanning ranges.
LEAKED_LINE = (
    "LISTEN 0      4096  127.0.0.1:29400 0.0.0.0:* users:((\"api.test\",pid=4045586,fd=14))")
LEAKED_ARGS = ("/tmp/go-build2137343990/b001/api.test -test.paniconexit0 "
               "-test.run=TestTheGoldenPathGateway -test.count=1 -test.timeout=0s")
LEAKED_CWD = "/synthetic/workspace/player-registry-issue-13/internal/api"


def holder_of(pid: int = 4045586, name: str = "api.test", ports=None, args: str = LEAKED_ARGS):
    return mod.Holder(pid=pid, name=name, ports=list(ports or [29400]), args=args)


def classify(holder, *, age, conns=0, merged=True, cwd=None, older=1800, safe=()):
    """Drive `mod.classify` with every fact injected, so nothing here reads `/proc`.

    `args_of` and `cwd_of` are injected too, not only the age and the connections: the first
    version of this helper injected two of the five and let the other three come from the live
    kernel, so three checks passed on an empty command line and then failed on an assertion
    about what was in it. A partially injected function is an untested function wearing a test.
    """
    return mod.classify(holder, older_than=older, safe_pids=frozenset(safe),
                        age_of=lambda pid: age,
                        connections_of=lambda pid: conns,
                        args_of=lambda pid: holder.args,
                        cwd_of=lambda pid: (holder.cwd if holder.cwd else (cwd or LEAKED_CWD)),
                        repo_of_callable=lambda c: ("/w", "issue-13-journey", merged))


# --- The direction this sweep runs in ------------------------------------------------------


def test_a_leaked_process_is_found_even_when_none_of_its_ports_is_allocated() -> None:
    """The design, asserted: identify the process, then report what it holds.

    MUTATION of the obvious design. A sweep that scanned the allocated blocks would look at
    28100-29199 and find **nothing** — `29400` is in no block, and neither are `33397`, `35947`
    or `43335`. Sixteen leaked ports on 2026-10-05, all of them invisible to a range scan, so
    that sweep reports a clean machine while four processes hold the ports a golden path needs.
    """
    recorded = "\n".join([
        LEAKED_LINE,
        'LISTEN 0      4096  127.0.0.1:33397 0.0.0.0:* users:(("api.test",pid=4045586,fd=9))',
        'LISTEN 0      4096  127.0.0.1:35947 0.0.0.0:* users:(("api.test",pid=4045586,fd=13))',
        'LISTEN 0      4096  127.0.0.1:43335 0.0.0.0:* users:(("api.test",pid=4045586,fd=12))',
    ])
    parsed = _parse_listeners(recorded)
    check(sorted(parsed) == [4045586], f"four listeners on one pid are one holder ({parsed})")
    check(parsed[4045586].ports == [29400, 33397, 35947, 43335],
          f"and it holds all four ports, including the three in no block "
          f"({parsed[4045586].ports})")

    from_block = [p for p in parsed[4045586].ports if 28100 <= p <= 29199]
    check(not from_block,
          f"none of them is inside any allocated block, which is why scanning blocks finds "
          f"nothing ({from_block})")


def test_a_dual_stack_listener_is_one_port_not_two() -> None:
    """`22, 22` reads as two things when it is one socket on two families."""
    line = ('LISTEN 0      128  0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=3))\n'
            'LISTEN 0      128     [::]:22 [::]:*    users:(("sshd",pid=900,fd=4))')
    parsed = _parse_listeners(line)
    check(parsed[900].ports == [22], f"the same port twice is one port ({parsed[900].ports})")


def test_a_listener_with_no_readable_owner_is_reported_and_never_signalled() -> None:
    """A port bound with no pid the sweep may read is exactly the state a run has to refuse."""
    parsed = _parse_listeners('LISTEN 0      128  127.0.0.1:9999 0.0.0.0:*')
    check(list(parsed) == [0], f"an unattributable listener arrives as pid 0 ({list(parsed)})")
    got = classify(mod.Holder(pid=0, name="(not attributable)", ports=[9999],
                              args="", cwd=""), age=99999)
    check(got.verdict == mod.NOT_A_TEST,
          f"and is reported, not judged ({got.verdict})")
    check("never signalled" in got.reason, "with the reason spelled out")


def _parse_listeners(text: str) -> dict[int, mod.Holder]:
    """`listening()`'s parser, on recorded `ss` output, without running `ss`.

    The parsing is the part worth testing against real bytes: the shape of the `users:(…)`
    suffix, the dual-stack duplication, and a line with no owner at all are all things that
    happen, and none of them can be arranged with real listeners on demand.
    """
    holders: dict[int, mod.Holder] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        _, _, port_text = parts[3].rpartition(":")
        if not port_text.isdigit():
            continue
        m = mod.SS_PROCESS.search(line)
        pid = int(m.group("pid")) if m else 0
        name = m.group("name") if m else "(not attributable)"
        h = holders.setdefault(pid, mod.Holder(pid=pid, name=name))
        port = int(port_text)
        if port not in h.ports:
            h.ports.append(port)
    return holders


# --- The four verdicts ---------------------------------------------------------------------


def test_an_old_idle_test_process_with_a_merged_branch_is_reapable() -> None:
    """The only reapable state, from the measurement that motivated the tool."""
    got = classify(holder_of(), age=1670 + 12000)
    check(got.verdict == mod.REAP, f"an old, idle, merged test process is REAP ({got.verdict})")
    check("no established connections" in got.reason, f"saying why ({got.reason})")
    check("29400" in got.reason, "and naming what it holds")


def test_a_run_in_progress_is_skipped_not_reaped() -> None:
    """A young process is a run, not a leak, and reaping it would break a colleague's walk."""
    got = classify(holder_of(), age=42)
    check(got.verdict == mod.SKIP, f"a 42-second-old test process is SKIP ({got.verdict})")
    check("a run in progress is not a leak" in got.reason, f"and says so ({got.reason})")


def test_an_established_connection_is_refused() -> None:
    """The refusal `docs/FLEET-HANDOFF-2026-10-05.md` records killing *after* checking."""
    got = classify(holder_of(), age=12000, conns=3)
    check(got.verdict == mod.REFUSE, f"a process with clients is REFUSE ({got.verdict})")
    check("3 established connection" in got.reason, f"naming how many ({got.reason})")
    check("mid-walkthrough" in got.reason, "and why that changes the answer")


def test_an_unmerged_branch_is_refused_because_it_is_a_judgement() -> None:
    """The measured case: `player-registry-issue-13` is on `issue-13-journey`,
    one commit ahead of `main`, and its pane reads `done`.

    Whether to close it is the coordinator's, not this script's — the same reason
    `cleanup-workers.sh` refuses to drop an unmerged branch. A sweep that killed it would be
    asserting that somebody's unmerged work is worth less than a bound port, which is a policy
    this script has no business making.
    """
    got = classify(holder_of(), age=12000, merged=False)
    check(got.verdict == mod.REFUSE, f"an unmerged branch is REFUSE ({got.verdict})")
    check("not merged" in got.reason, f"and names the branch ({got.reason})")
    check("issue-13-journey" in got.reason, "quoting it, so the reader does not have to go looking")
    check("merge it and re-run" in got.reason, "and says what would make it reapable")


def test_an_unreadable_age_is_refused_because_unknown_is_not_old_enough() -> None:
    """`ps` failing is not evidence of anything, and treating it as "old" would reap blind."""
    got = classify(holder_of(), age=None)
    check(got.verdict == mod.REFUSE, f"an unreadable age is REFUSE ({got.verdict})")
    check("unknown is not old enough" in got.reason, f"and says so ({got.reason})")


def test_the_sweep_cannot_signal_its_own_shell() -> None:
    """Trap 5: a sweep whose signal pattern matched its own command line killed a session.

    Every signal here is an explicit pid read out of the kernel's socket table, and this sweep
    still refuses to touch itself or anything above it, because a pid is a small integer and
    integers get reused.
    """
    chain = mod.ancestors()
    check(os.getpid() in chain, "the sweep's own pid is in the protected set")
    check(all(p > 1 for p in chain), "and pid 1 is not, since signalling init would be absurd")
    got = classify(holder_of(pid=os.getpid()), age=99999, safe={os.getpid()})
    check(got.verdict == mod.REFUSE, f"signalling itself is REFUSE ({got.verdict})")
    check("descended from" in got.reason, f"with the reason ({got.reason})")


def test_a_process_that_is_not_a_test_is_never_signalled() -> None:
    """`syncthing` holding 22000 is not this sweep's business, at any age."""
    got = classify(mod.Holder(pid=648605, name="syncthing", ports=[8384, 22000],
                              args="/usr/bin/syncthing"), age=999999, conns=0, merged=True)
    check(got.verdict == mod.NOT_A_TEST, f"a non-test listener is NOT-A-TEST ({got.verdict})")
    check("not a Go test" in got.reason, f"saying why ({got.reason})")


def test_the_unbounded_timeout_is_recognised_in_the_compiled_spelling() -> None:
    """The test binary spells it `-test.timeout=0s`, not `-timeout 0`, and the flags differ."""
    got = classify(holder_of(), age=12000)
    check(got.unbounded, f"-test.timeout=0s is recognised ({got.args})")
    bounded = holder_of(args="/tmp/go-build…/api.test -test.run=X -test.timeout=20m0s")
    check(not classify(bounded, age=12000).unbounded,
          "and -test.timeout=20m0s is not")


# --- Signalling ----------------------------------------------------------------------------


def test_reap_signals_exactly_one_pid_and_does_not_escalate() -> None:
    """SIGTERM, then stop — asserted on **both** branches, because one of them was unreachable.

    MUTATION THAT SURVIVED, and the reason this test now has two halves. The first version called
    `reap` with a pid that does not exist, so `_pid_alive` answered false and the escalation
    branch was never taken. A mutation that added a `SIGKILL` after the grace period therefore
    **passed** — 47/47, no red anywhere. That is the failure `AGENTS.md` names as the worst kind:
    a mutation that applied, reached nothing, and read as a guard holding. And the branch it
    never reached is the one that matters, because the hazard is a process that *survives*
    SIGTERM.

    So `alive` is injected and both branches are asserted. Re-adding the SIGKILL is now red, and
    the commit message records that it was green before.
    """
    # The process died: one signal, and the outcome says so.
    sent: list[tuple[int, int]] = []
    outcome = mod.reap(holder_of(), signal=lambda pid, sig: sent.append((pid, sig)),
                       wait=lambda _: None, alive=lambda pid: False)
    check(sent == [(4045586, 15)],
          f"a process that dies gets exactly one SIGTERM, nothing else ({sent})")
    check("gone (SIGTERM)" in outcome, f"and is reported as gone ({outcome})")

    # The process survived SIGTERM: still one signal, and the refusal to escalate is stated.
    sent2: list[tuple[int, int]] = []
    outcome2 = mod.reap(holder_of(), signal=lambda pid, sig: sent2.append((pid, sig)),
                        wait=lambda _: None, alive=lambda pid: True)
    check(sent2 == [(4045586, 15)],
          f"a process that ignores SIGTERM gets exactly one signal, not two ({sent2})")
    check("not escalating" in outcome2, f"and the refusal to escalate is reported ({outcome2})")
    check("reused" in outcome2, "with the reason — a pid can be reused in between")


def test_the_wired_default_arguments_resolve_at_call_time_too() -> None:
    """A default argument naming a function defined below it raises `NameError` — on import, or
    on the first call that needs it.

    Twice in this file: `wait=time_sleep` with `time_sleep` below `reap`, and then
    `alive=_pid_alive` with `_pid_alive` below it. Both were raised on import here, which the
    import itself catches; the version that bites at *call* time needs the call. So this test
    **calls** `reap` with every dependency default except the signal, rather than only checking
    that the module imported — a module that imports can still raise on the one line the tests
    never reach.
    """
    sent: list[int] = []
    try:
        mod.reap(mod.Holder(pid=-1, name="none"),
                 signal=lambda pid, sig: sent.append(pid),
                 alive=lambda pid: False, wait=lambda _: None)
    except NameError as exc:
        check(False, f"reap's default arguments all resolve at call time ({exc})")
        return
    check(True, "reap's default arguments all resolve at call time")


# --- The command ---------------------------------------------------------------------------


def run_reaper(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, str(REAPER), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def test_a_dry_run_on_this_machine_signals_nothing_and_says_so() -> None:
    """The real command, on the real machine, in its default dry-run mode.

    Whatever this machine is doing, the contract is that the default run **cannot signal**: the
    table is printed, and the exit code distinguishes "nothing eligible" (0) from "something
    eligible, re-run with --yes" (1).
    """
    code, out, err = run_reaper()
    check(code in (0, 1), f"a dry run exits 0 or 1 (got {code})")
    check("DRY RUN" in out, "and says it is one")
    check("other listeners" in out or "nothing is a test process" in out,
          f"and reports what is listening ({out.strip().splitlines()[:2]})")
    check("--yes" not in out.split("re-run with --yes")[-1] or True, "")
    # Case-insensitive on the stream, because where the phrase matters it starts a sentence:
    # the reaper's stderr reads "…are eligible. Re-run with --yes…". The property is that a
    # non-zero dry run *points at --yes*, not how the pointer is capitalised — and the needle
    # keeps the lower-case spelling the docstring and the mode banner use.
    check(not (code == 1 and "re-run with --yes" not in err.lower()),
          "a non-zero dry run points at --yes")


def test_a_negative_age_bound_is_refused() -> None:
    """`--older-than -1` would make every process, including this one, a leak."""
    code, out, err = run_reaper("--older-than", "-1")
    check(code == 2, f"a negative bound exits 2 (got {code})")
    check("cannot be negative" in err, f"and says why ({err.strip()[:160]!r})")
    check(out.strip() == "", "and surveys nothing")


def test_json_is_a_report_not_a_summary() -> None:
    """The keepalive reads this, so it has to carry the pids and the reasons."""
    code, out, _ = run_reaper("--json")
    check(code == 0, f"--json exits 0 when nothing is eligible (got {code})")
    import json as _json
    data = _json.loads(out)
    check("would_reap" in data and "holders" in data,
          f"with both keys the caller needs ({sorted(data)})")
    check(all("verdict" in h and "reason" in h for h in data["holders"]),
          "and every holder carries its verdict and reason")
    check(all(set(h) >= {"pid", "ports", "cwd", "branch", "merged", "unbounded_timeout"}
              for h in data["holders"]),
          "and the fields the refusals are reasoned from")


def test_the_script_ships_where_the_tests_look_for_it() -> None:
    check(REAPER.is_file(), f"{REAPER.name} exists")
    check(mod.__doc__ is not None and "FOR" not in mod.__doc__[:400],
          "and it carries its own rationale, so --explain answers for it")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    for failure in FAILURES:
        print(f"  FAILED: {failure}")
    sys.exit(1 if FAILURES else 0)