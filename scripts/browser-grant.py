#!/usr/bin/env python3
"""browser-grant — one `agent-browser` session, one holder, and a failure you can name.

WHY THIS EXISTS
---------------
`agent-browser` reports a busy or broken daemon as `exit status 1`, with the real reason
printed underneath:

    agent-browser click button[title="Français"] failed: exit status 1
    Failed to read: Resource temporarily unavailable (os error 11)
    (after 5 retries - daemon may be busy or unresponsive)

That is `EAGAIN` — a statement about the transport, not about the product. It reached a reader
as an ordinary Go test failure because `t.Fatalf` cannot tell the two apart, and the failure it
produced was on a **user-visible assertion**. As the incident record has it, that is worse than Dagger's contention:
a contended golden path that fails for an unrelated reason and passes on a rerun is taken as
proof, so the failure mode is not a false negative but a false positive that gets believed
twice. A retry is only safe if you know *which kind* of failure you are retrying — which is why
the shape here is worth more than the retry.

So there are two things, and they are not the same thing:

    PASS             ran, exit 0                                   evidence
    FAIL             ran, non-zero, no transport token             evidence about the code
    BUSY             the transport token, and contention was possible   NOT evidence — retry
    BROWSER-FAULT    the transport token, and we held the session   NOT evidence, and not a lock
    SKIPPED          never ran, and says why                        NOT evidence

`BROWSER-FAULT` is the state this whole file is for. If **we** held the session exclusively,
then another run cannot have caused the token, so "busy" is a false reading and the daemon is
genuinely unwell — which a retry will not fix and which must not be filed as a product defect.
A classifier that only matched text could not tell those two apart, and would hand back exactly
the ambiguity this ticket exists to remove.

## The lock is the evidence, not the text

`held` is not an assumption. It is the answer to "did *this process* take the session lock and
still hold it while the command ran" — asked of a different mechanism than the one parsing the
text. A guard that asks the thing it is checking cannot catch it; `fleet-watch.py` carries the
same note about a `gate()` that agreed with itself.

## And it refuses rather than collides

Because the hazard is sharing a session name (measured in `docs/browser-grant.md`), the fix is a
partition, not a queue: when the lock is already held, **the command is not run at all** and the
caller gets `BUSY`. No assertion is evaluated, so nothing can be falsified. `--wait` exists for
the one case where waiting is right — a single worker racing itself — and it defaults to 0.

## This is additive. It never grants.

The coordinator-held grant (`READY-FOR-BROWSER:` → `BROWSER RELEASED —`) is a human decision a
person can see, override and re-order. This file answers a strictly narrower question a machine
can answer: is the session I am about to touch mine alone. It therefore never authorises a run,
never queues silently, and reports a held session with no `--granted-by` as `UNGRANTED` for the
coordinator to see rather than treating that as permission. `force-release` is the human's way
out, and it says out loud that it was used.

    python3 scripts/browser-grant.py status
    python3 scripts/browser-grant.py run --session press-gp --label "press #7" -- agent-browser open http://127.0.0.1:18081
    python3 scripts/browser-grant.py run --session press-gp --label "press #7" -- scripts/verify-golden-path.sh
    python3 scripts/browser-grant.py classify --exit 1 --held < failure.txt
    python3 scripts/browser-grant.py force-release --session press-gp --reason "worker wedged"

Exit codes are the machine-readable half of the shape, so a caller never parses prose:
0 PASS, 1 FAIL, 75 BUSY (EX_TEMPFAIL), 76 BROWSER-FAULT, 77 SKIPPED.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factory_config  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
BUSY = "BUSY"
BROWSER_FAULT = "BROWSER-FAULT"
SKIPPED = "SKIPPED"
UNDECIDED = "UNDECIDED"

EXIT_CODE = {PASS: 0, FAIL: 1, BUSY: 75, BROWSER_FAULT: 76, SKIPPED: 77, UNDECIDED: 78}

# Only the four that the evidence in docs/browser-grant.md supports. Anything else a human adds
# is a guess about a string they have not seen fail.
#
# **Matched with word boundaries, not `in`.** A plain substring test makes `os error 111`
# (EADDRINUSE) match `os error 11`, which would file "the port is already bound" as daemon
# contention — the same class of false excuse this file exists to remove, arrived at through a
# one-character difference. `test_a_near_miss_errno_is_not_a_transport_token` holds it.
TRANSPORT_TOKENS = (
    ("os error 11", re.compile(r"os error 11\b")),
    ("eagain", re.compile(r"\beagain\b")),
    ("resource temporarily unavailable", re.compile(r"resource temporarily unavailable")),
    ("daemon may be busy", re.compile(r"daemon may be busy")),
)

# `agent-browser` confirms successful commands in prose ("✓ Opponent Brief"), so its stdout is
# not a failure signal and text alone cannot decide anything. Verbatim from
# opponent-brief/internal/api/goldenpath_browser_test.go:509, which says the same
# thing about the same CLI.
PROSE_SUCCESS = "✓"

STATE_DIR = Path(os.environ.get(
    "BROWSER_GRANT_STATE", str(factory_config.path("browser_grant"))))


# ----------------------------------------------------------------------- classify


def transport_token(text: str) -> str | None:
    """The transport token in `text`, or None. The first match, named, so the reason can quote it."""
    low = (text or "").lower()
    for display, pattern in TRANSPORT_TOKENS:
        if pattern.search(low):
            return display
    return None


def classify(text: str, exit_code: int | None, held: bool | None) -> tuple[str, str]:
    """`(verdict, reason)`. The whole recognisable shape, as one pure function.

    Three-valued on `held` on purpose. `held=True` and `held=False` are facts from the lock;
    `held=None` is "nobody asked", and it must not collapse into either. A caller that skipped
    the lock check gets `UNDECIDED`, which is a name rather than a guess — the same reason
    `fleet-watch.py` has a `NONE` that never becomes `MERGEABLE`.

    `exit_code=None` is `UNDECIDED` too, and separately: prose on stdout is not a failure
    signal, so the text alone genuinely cannot decide, and saying so beats picking one.
    """
    token = transport_token(text)

    if exit_code is None:
        return UNDECIDED, (
            f"no exit status given, and the text alone cannot decide: agent-browser confirms "
            f"success in prose (\"{PROSE_SUCCESS} ...\"), so output is not a failure signal"
            + (f". (the text does carry the transport token {token!r})" if token else ""))

    if exit_code == 0:
        if token:
            return PASS, (f"exit 0, so the command did what it was asked; the transport token "
                          f"{token!r} appears in the output but the CLI reported success")
        return PASS, "exit 0"

    if token and held is None:
        return UNDECIDED, (
            f"the transport token {token!r} is present, but nobody checked whether this run "
            f"held the session lock — so 'busy' and 'the daemon is unwell' are the same text "
            f"and cannot be told apart from it alone")

    if token and held:
        return BROWSER_FAULT, (
            f"the transport token {token!r} appeared WHILE THIS RUN HELD THE SESSION LOCK, so "
            f"another run cannot have caused it. This is a sick daemon, not contention: retrying "
            f"will not fix it and it is not evidence about the code")

    if token:
        return BUSY, (
            f"the transport token {token!r} appeared and this run did not hold the session lock, "
            f"so another run can have caused it. NOT evidence about the code — a retry is safe, "
            f"and this result must not be filed as a defect")

    return FAIL, f"exit {exit_code} with no transport token, so it is a real failure and not contention"


def banner(verdict: str, reason: str, session: str) -> str:
    """One greppable line. A shape nobody can grep is a shape nobody uses."""
    return f"BROWSER-GRANT: {verdict} session={session} exit={EXIT_CODE.get(verdict, '?')} :: {reason}"


# ------------------------------------------------------------------------- locking


def _safe(name: str) -> str:
    """A session name turned into one path component."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "")


def _state_dir(state: Path | str | None) -> Path:
    """Coerce to Path.

    `--state` arrives from argparse as a **string**, and `str / str` is a TypeError rather than a
    path. Every unit test passes a `Path`, so this only ever fired through the real CLI — the
    second bug in a row that the parser-only path found and the unit tests could not.
    """
    return Path(state) if state else STATE_DIR


def lock_path(session: str, state: Path | str | None = None) -> Path:
    return _state_dir(state) / f"{_safe(session)}.lock"


def record_path(session: str, state: Path | str | None = None) -> Path:
    return _state_dir(state) / f"{_safe(session)}.json"


class SessionLock:
    """An exclusive claim on one `agent-browser` session.

    `flock` rather than an `O_EXCL` sentinel file, for one reason: the kernel drops it when the
    holder dies. A sentinel written by a worker that Herdr restarts away would keep the session
    locked for ever, and the coordinator would have to know to clear it — the wedge the grant
    table exists to prevent. The JSON beside it is a *record* for humans; the kernel's lock is
    the truth, and a record whose lock is free is reported `STALE` rather than believed.
    """

    def __init__(self, session: str, state: Path | None = None):
        self.session = session
        self.state = Path(state or STATE_DIR)
        self.path = lock_path(session, self.state)
        self.fd: int | None = None

    def acquire(self, wait: float = 0.0, label: str = "", granted_by: str = "") -> bool:
        self.state.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    return False
                time.sleep(0.2)
        self.write_record(label=label, granted_by=granted_by)
        return True

    def write_record(self, label: str, granted_by: str) -> None:
        record_path(self.session, self.state).write_text(json.dumps({
            "session": self.session,
            "pid": os.getpid(),
            "label": label,
            "granted_by": granted_by,
            "since": time.time(),
        }, indent=1))

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None
        record_path(self.session, self.state).unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def read_record(session: str, state: Path | None = None) -> dict:
    try:
        return json.loads(record_path(session, state).read_text())
    except (OSError, ValueError):
        return {}


def is_locked(session: str, state: Path | None = None) -> bool:
    """Is somebody holding this session right now? Asked without taking the lock."""
    path = lock_path(session, state)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def session_state(session: str, state: Path | None = None) -> dict:
    """`FREE` / `HELD` / `STALE`, plus whether a coordinator grant was recorded.

    Three states, and the third is the point. `HELD` is a live holder; `STALE` is a **record
    whose process is gone or whose lock the kernel already released** — the thing that should
    have stopped existing did not — and it is reported rather than cleaned, because silently
    deleting it is how a wedged session becomes an invisible one. `FREE` with a record is
    exactly that case. "I ran nothing" and "nothing is running" must not read alike.
    """
    rec = read_record(session, state)
    locked = is_locked(session, state)
    pid = rec.get("pid")
    if locked:
        verdict = "HELD"
    elif rec:
        verdict = "STALE"
    else:
        verdict = "FREE"
    return {
        "session": session,
        "state": verdict,
        "pid": pid,
        "pid_alive": alive(pid) if isinstance(pid, int) else None,
        "label": rec.get("label", ""),
        "granted_by": rec.get("granted_by", ""),
        "since": rec.get("since"),
        # Reported, never enforced: this file cannot see GitLab, so it has no opinion about
        # whether a grant was actually issued. It only says nobody said so.
        "ungranted": bool(locked and not rec.get("granted_by")),
    }


def known_sessions(state: Path | None = None) -> list[str]:
    root = Path(state or STATE_DIR)
    if not root.is_dir():
        return []
    names = []
    for lock in sorted(root.glob("*.lock")):
        rec = read_record(lock.stem, state)
        names.append(rec.get("session") or lock.stem)
    return names


def agent_browser_sessions() -> list[str]:
    """The CLI's own list of session names. Empty list plus a reason, never a bare empty list.

    Same rule as `demo_gate` in fleet-watch.py: a check that cannot see anything must say so,
    or its silence reads as a clean bill of health.

    **These are names, not running browsers.** `agent-browser session` offers no delete
    operation, so `close` shuts a browser down and leaves its name registered — measured on
    2026-10-03, when four closed sessions were still listed with no Chrome behind them. The
    caller must not present this as a count of live browsers.
    """
    if shutil.which("agent-browser") is None:
        return []
    try:
        p = subprocess.run(["agent-browser", "session", "list"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    out, seen = [], False
    for line in (p.stdout or "").splitlines():
        if not seen:
            if line.strip().lower().startswith("active sessions"):
                seen = True
            continue
        if line.strip():
            out.append(line.strip())
    return out


# ---------------------------------------------------------------------------- run


def emit(verdict: str, reason: str, session: str, out=sys.stderr) -> None:
    print(banner(verdict, reason, session), file=out)


def run_locked(session: str, argv: list[str], *, label: str, granted_by: str,
               wait: float, state: Path | None, out=sys.stderr) -> int:
    lock = SessionLock(session, state)
    if not lock.acquire(wait=wait, label=label, granted_by=granted_by):
        held = session_state(session, state)
        emit(BUSY, (
            f"refused before running: session {session!r} is already held by pid "
            f"{held.get('pid')} ({held.get('label') or 'no label'}). The command was NOT "
            f"executed, so no assertion was evaluated and nothing was falsified — this is not "
            f"evidence about the code. Give this run its own session name, or wait "
            f"(--wait {wait:g}s)."), session, out)
        return EXIT_CODE[BUSY]

    try:
        completed = subprocess.run(argv, capture_output=True, text=True)
        output = (completed.stdout or "") + (completed.stderr or "")
        sys.stdout.write(completed.stdout or "")
        sys.stderr.write(completed.stderr or "")
        sys.stdout.flush()
        verdict, reason = classify(output, completed.returncode, held=True)
        emit(verdict, reason, session, out)
        return EXIT_CODE[verdict]
    finally:
        lock.release()


# ---------------------------------------------------------------------- commands


def cmd_status(args) -> int:
    rows = [session_state(s, args.state) for s in known_sessions(args.state)]
    live = agent_browser_sessions()
    if args.json:
        print(json.dumps({"sessions": rows, "agent_browser_sessions": live}, indent=1,
                         sort_keys=True))
        return 0
    print("browser-grant — who holds a session, and whether the coordinator said so")
    if not rows:
        print("  no session has been claimed through this tool. That is not the same as the "
              "workspace being idle — see `agent-browser session list` below.")
    for r in rows:
        flag = "  !! UNGRANTED — nobody recorded a coordinator grant for this run" if r["ungranted"] else ""
        print(f"  {r['state']:<6} {r['session']:<34} pid={r['pid']} alive={r['pid_alive']} "
              f"label={r['label'] or '-'}{flag}")
        if r["state"] == "STALE":
            print(f"         a record survives for pid {r['pid']} (alive={r['pid_alive']}) but the "
                  f"lock is free — the holder is gone and the record was not cleaned. It is "
                  f"reported rather than deleted so the wedge stays visible.")
    print()
    if shutil.which("agent-browser") is None:
        print("  agent-browser is not on PATH, so the daemon's own session list was NOT read.")
    else:
        print(f"  agent-browser still lists {len(live)} session name(s): "
              f"{' '.join(live) or '(none)'}")
        print("  That is the CLI's own list, not a count of running browsers: `agent-browser "
              "session` has no")
        print("  delete operation, so `close` shuts a browser down and leaves its name behind. "
              "Do not read")
        print("  it as evidence that a browser is alive — the lock above is the authority for "
              "that.")
    print("\n  HELD = a live process holds it. STALE = the lock is free but a record survives.")
    print("  FREE = nothing. This tool never grants: a grant is the coordinator's.")
    return 0


def cmd_run(args) -> int:
    """One claim, one command.

    There is deliberately no separate `hold` verb. It would behave identically — the lock is
    held by this process for the child's whole lifetime either way — and two names that do the
    same thing imply a distinction that does not exist, which is how a reader ends up trusting
    the one that sounds stronger. Passing a script where a command is expected is the supported
    way to cover a whole golden path:

        browser-grant.py run --session press-gp -- scripts/verify-golden-path.sh

    **The claim covers the session name it was given and nothing else.** If the script inside
    drives a different `--session`, or the bare `default`, this lock is silent about it. That
    is a real limit of the mechanism and `BROWSER-FAULT` is reported with that caveat rather
    than as a clean conclusion.
    """
    argv = list(args.cmd)
    # `argparse.REMAINDER` hands the `--` separator through as the first element, so a command
    # written the documented way (`run --session s -- agent-browser open ...`) would be executed
    # as a program literally named `--`. Found by running it, not by the tests: every test called
    # run_locked() directly and so never went near the parser.
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("browser-grant: nothing to run. Usage: run --session NAME [-- opts] -- <command ...>",
              file=sys.stderr)
        return 2
    return run_locked(args.session, argv, label=args.label, granted_by=args.granted_by,
                      wait=args.wait, state=args.state)


def cmd_classify(args) -> int:
    """`--json` on stdout, the banner on stderr — the same split `run` uses.

    stdout is the payload a caller might parse or redirect; the banner is commentary about the
    run, and `run` already puts it on stderr. A verdict that changed stream between the two
    commands would be a small trap in a file whose whole purpose is that its output is
    greppable in one place.
    """
    text = sys.stdin.read() if not sys.stdin.isatty() else ""
    verdict, reason = classify(text, args.exit, args.held)
    if args.json:
        print(json.dumps({"verdict": verdict, "reason": reason, "exit_code": EXIT_CODE[verdict]},
                         indent=1, sort_keys=True))
    else:
        emit(verdict, reason, args.session or "-")
    return EXIT_CODE[verdict]


def cmd_force_release(args) -> int:
    """The coordinator's way out — and it is loud on purpose.

    An override nobody can see is how an automated lock quietly becomes the authority the grant
    table says it is not. So this refuses silently nothing: it prints before and after, and
    appends to an audit file. It also refuses to pretend it freed a live holder.
    """
    before = session_state(args.session, args.state)
    if before["state"] == "HELD" and not args.force_live:
        print(f"browser-grant: session {args.session!r} is HELD by live pid {before['pid']}. "
              f"Refusing to force it: that is a running browser, and clearing it would pull the "
              f"daemon out from under a worker mid-walk. Pass --force-live if you mean it.",
              file=sys.stderr)
        return 2
    if before["state"] == "FREE":
        print(f"browser-grant: session {args.session!r} was already FREE — nothing to release. "
              f"Reported rather than silently succeeding, because 'I released it' and 'there was "
              f"nothing there' are different facts.", file=sys.stderr)
        return EXIT_CODE[SKIPPED]

    root = Path(args.state or STATE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "overrides.log").open("a") as fh:
        fh.write(json.dumps({"session": args.session, "by": args.granted_by or "(unnamed)",
                             "reason": args.reason, "was": before["state"],
                             "pid": before.get("pid"), "at": time.time()}) + "\n")
    record_path(args.session, args.state).unlink(missing_ok=True)
    print(f"browser-grant: OVERRIDE — released {args.session!r} (was {before['state']}, "
          f"pid {before.get('pid')}); reason: {args.reason}")
    print(f"browser-grant: appended to {root / 'overrides.log'}. State is now "
          f"{session_state(args.session, args.state)['state']}. The coordinator still has to "
          f"grant the next run.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="browser-grant.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=None,
                    help="override the state directory (default $BROWSER_GRANT_STATE or "
                         "/tmp/opencode/browser-grant)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="who holds a session, and whether a grant was recorded")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("run", help="run one command (or one whole golden path) under the lock")
    p.add_argument("--session", required=True,
                   help="the agent-browser --session name this run uses. ONE PER RUN: two runs "
                        "sharing a name corrupt each other's page and viewport "
                        "(docs/browser-grant.md)")
    p.add_argument("--label", default="", help="human label, shown by `status`")
    p.add_argument("--granted-by", default="",
                   help="who granted the BROWSER grant. Recorded so `status` can show a run "
                        "nobody authorised. This tool does not check it and does not require it.")
    p.add_argument("--wait", type=float, default=0.0,
                   help="seconds to wait for the lock. 0 by default: refusing is visible, "
                        "blocking is not. Use a few seconds only for a worker racing itself.")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("classify", help="name a failure from its text and exit status")
    p.add_argument("--exit", dest="exit", type=int, default=None,
                   help="the command's exit status. Without it the verdict is UNDECIDED: "
                        "agent-browser confirms success in prose, so text alone cannot decide.")
    p.add_argument("--held", dest="held", action="store_true", default=None,
                   help="this run held the session lock (the caller took it and still holds it)")
    p.add_argument("--contended", dest="held", action="store_false",
                   help="this run did not hold the session lock")
    p.add_argument("--session", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("force-release", help="coordinator override for a wedged holder")
    p.add_argument("--session", required=True)
    p.add_argument("--reason", required=True, help="recorded, and required on purpose")
    p.add_argument("--granted-by", default="")
    p.add_argument("--force-live", action="store_true",
                   help="release even though the holder's process is alive")
    p.set_defaults(func=cmd_force_release)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Normalised once here so nothing downstream has to wonder whether it was handed a Path or
    # a string. `--state` is the only value that can be either.
    args.state = _state_dir(args.state)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
