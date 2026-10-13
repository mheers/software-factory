#!/usr/bin/env python3
"""The one parser that turns gate output into "the line a reader should look at".

**A `t.Logf` is not a failure, and inside a `--- FAIL:` block it is indistinguishable from a
`t.Fatalf`.** That sentence is the whole reason this file exists, and it is why the parser
below does not *pick*.

    --- FAIL: TestTheRegistryNeverWritesToTheProvider (0.00s)
        import_test.go:1456: read 11 sources in internal/registry; none of them binds a
            provider client or writes to one          <- t.Logf, narrating a PASSING scan
        import_test.go:1462: football.Provider exposes 7 methods; the import path is
            written against a seam with eight         <- t.Fatalf, the actual failure
    FAIL

On 2026-10-03 a sweep reported line 1456 as the reason `player-registry` was RED,
and the coordinator went looking for a defect in a provider-client scan that was working
perfectly. `go test` without `-v` prints a failing test's **entire** output, so both lines sit
in the same block with the same `file.go:NNNN:` shape and there is no reliable way to tell
them apart from the text.

The obvious repair — report the first line inside the `--- FAIL:` block — is therefore still
wrong. So this returns **every** diagnostic in the block, in order, and says how many there
are when there is more than one, because "here are three things and one of them is why" is a
different claim from "here is why", and only the first is supportable from the output. One
diagnostic is reported plainly, without hedging. With no `FAIL` block at all — a compile
error, a linter, a `make` echo — the first line naming a position is right, because then
there is only one candidate.

## Why this is a module and not a function inside one caller

`fleet-keepalive.py` renders `GATE-RED` with this line as its `->` detail, and that log is
what the coordinator reads to decide what went wrong. Two copies of the function that formats
*why a red is believed* is a risk, not tidiness — and the risk had already been cashed in
before this file existed.

`scripts/fleet-watch.py` and `scripts/main_sweep.py` each carried a copy, and **they drifted**:

| | capped the | effect |
|---|---|---|
| `fleet-watch.py` | each diagnostic | the real `t.Fatalf` survives a long `t.Logf` |
| `main_sweep.py` | the **joined** string | a `t.Logf` over 160 chars pushed the real `t.Fatalf` out entirely |

So `main` was gated by the broken copy while a worker's branch was gated by the fixed one —
two answers to "what failed", from one function's name. The second is the original defect
reaching the same outcome by a different route: a truncated report that begins with a
narration line reads exactly like a report about that narration line. Measured on the drifted
copy: the real failure disappears once the log line exceeds 160 characters.

## The import, and why it is written the way it is

Callers reach this module as `import first_useful_line` after inserting this directory on
`sys.path`, which resolves on every way these scripts are actually run — by absolute path from
cron and from other repositories, by relative path from a checkout, and loaded by `importlib`
with `sys.path` untouched (`merge-gate.py` loads `fleet-watch.py` that way). `__file__` is
set in all three cases, so the directory to insert is known without asking the caller's cwd.

The cost is that a script **copied without this file** cannot run, and two test harnesses copy
`fleet-watch.py` into a temp directory. That is the right way to fail: a loud
`ModuleNotFoundError` at import, never a parser that has quietly reverted to a private copy.
Both harnesses copy this module beside the script for exactly that reason.
"""

from __future__ import annotations

# The width ONE diagnostic may occupy. It bounds a single line and must never bound the joined
# report — see `first_useful_line`. `fleet-watch.py` uses this value because the line it
# produces is a table cell; `main_sweep.py` passes a wider one because its line is a
# standalone report. That difference is a deliberate presentation choice, which is why it is a
# parameter and not a second implementation.
DIAG_WIDTH = 160


def first_useful_line(out: str, skip: tuple[str, ...] = (), *,
                      width: int = DIAG_WIDTH,
                      empty: str = "no output") -> str:
    """The failure, reported so the reader cannot be sent to the wrong line.

    `width` bounds one diagnostic. `empty` is what to say when there is nothing at all, which
    differs per caller because the two scripts phrase their own fallbacks differently
    elsewhere. Everything that decides *which* line is the failure is the same for both, and
    lives here rather than in either of them.
    """
    lines = [l.rstrip() for l in (out or "").splitlines()]

    # make's own directory banners are never the failure, in any caller, and this is not a
    # hypothetical: **`make` prints them only when it believes it is a sub-make**, so a sweep
    # invoked from a Makefile sees them and the same sweep run by hand does not. Measured
    # 2026-10-07 — `make guard` runs `test_main_sweep.py`, whose scratch gate runs `make check`
    # with `MAKELEVEL=1` inherited, and the RED detail became
    # `make[1]: Entering directory '/tmp/…/redrepo'` instead of the failure the Makefile
    # printed. `skip=("make:",)` cannot catch it: the sub-make's own prefix is `make[1]:`.
    def is_make_noise(s: str) -> bool:
        return s.startswith("make") and (": Entering directory" in s or ": Leaving directory" in s)

    def skipped(s: str) -> bool:
        return is_make_noise(s) or any(s.startswith(p) for p in skip)

    for i, raw in enumerate(lines):
        if not raw.strip().startswith("--- FAIL:"):
            continue
        diags: list[str] = []
        for follow in lines[i + 1:]:
            s = follow.strip()
            if not s:
                continue
            if s.startswith("--- "):
                break  # the next test's banner ends this block
            if follow == follow.lstrip():
                # Unindented: a package-level summary, not a diagnostic under the test.
                if s.startswith(("FAIL", "ok  ", "PASS", "#")):
                    break
            if skipped(s):
                continue
            diags.append(s)
        if not diags:
            return raw.strip()[:width]
        if len(diags) == 1:
            return diags[0][:width]
        # Every diagnostic gets `width`, and the joined report is deliberately NOT capped at
        # `width`. Truncating the joined string lands inside the *first* diagnostic — which is
        # the t.Logf — and hands the reader back exactly the line this function exists to stop
        # reporting. That is not a hypothetical: it is what the copy in `main_sweep.py` did,
        # and it dropped the real failure from the report for every log line over 160 chars.
        return (f"[{len(diags)} diagnostics] "
                + " | ".join(d[:width] for d in diags))

    # No FAIL block: a compile error, a linter, a `make` echo, or a frontend gate. There is
    # then only one candidate. `go vet` leads with a bare `# package/path` header and a test
    # run with a bare `FAIL`, so those are skipped in favour of the line naming a position.
    # The `.ts:`/`.vue:` patterns are kept on purpose — dropping them would not shorten a
    # frontend worker's reason, it would remove it.
    for raw in lines:
        s = raw.strip()
        if not s or skipped(s):
            continue
        if ".go:" in s or ".ts:" in s or ".vue:" in s or ": error" in s:
            return s[:width]
    for raw in lines:
        s = raw.strip()
        if not s or skipped(s):
            continue
        return s[:width]
    return empty