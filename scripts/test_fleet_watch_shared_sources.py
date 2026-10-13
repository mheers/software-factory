#!/usr/bin/env python3
"""The order the sweep gates in, and the not-fetching-every-sweep: observed, not read.

Run: `python3 scripts/test_fleet_watch_shared_sources.py` (no test runner, no network, no sleeps).

## Why this file exists, separately from `test_fleet_watch.py`

The shared-sources fix: `fleet-watch.py` ran `showboat verify demo.md` and nothing else, and never ran
`scripts/fetch-shared-sources.sh` first. For every repository that provisions its siblings from
the group, the gate was therefore red for a reason that is not a defect:

    GATE-RED  clean tree, gate red
        -> demo.md does not verify: block 2:

Block 2 is *a clean checkout builds one binary with the SPA inside it*, and the SPA build needs
the fetched `ui`. So the verdict named a **transcript block** while the missing thing
was a **prerequisite**. The documented response to `GATE-RED` is *go and gate it*, so it sent a
coordinator to reject a **green** branch — twice.

**A verdict that names a symptom rather than a prerequisite is the tell.** That is the rule worth
keeping; the fetch is the instance.

## What is in which file, and why it is split

`test_fleet_watch.py` holds the two **verdicts**, because a verdict is a claim about one row: a
tree that already has its sources is `MERGEABLE`, and a tree that cannot get them is `NO-GATE`
naming the fetch.

This file holds the two **sequences**, because a sequence cannot be asserted by reading the code
that claims it — it has to be watched happening:

1. `fetch` happens **before** `go vet`, before `make check` and before `showboat verify`. The
   order is the defect in the other direction: `make check` creates `web/node_modules`, and a
   fetch after that runs `pnpm install` in a directory that already has one, which **waits** for
   a `Proceed?` a non-terminal cannot answer rather than failing.
2. A tree that already has the shared sources is **not fetched at all**. Four or five sibling
   clones per pane per sweep is its own outage, and the checkout on disk is a better cache than a
   dict in this process: it survives a restart and every sweep shares it.

## How these observe rather than assert a message

`go`, `make` and `showboat` are stubbed on a temp `PATH` and each appends its own name to a log,
so the assertions are about a sequence that actually happened. A Python copy of the ordering
would pass while the script stayed broken — the same reason `test_cleanup_workers.py` drives the
real bash script through stub `herdr`, `git` and `glab` rather than re-implementing its loop.

`showboat`'s stub exits 0 **and prints nothing**, because that is what the real one does on
success and `demo_gate` reads an exit status rather than an absence of output.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# The sibling's machinery, reused rather than re-implemented: the worktree builder, the fetch
# stubs and the by-path loader all live in `test_fleet_watch.py` and are imported from there. A
# second copy of the sweep's fixture would be free to disagree with the first, which is the
# failure this family keeps hitting in every other language.
from test_fleet_watch import (  # noqa: E402  (the path insert above has to come first)
    fetch_stub,
    load,
    make_repo_with_a_branch,
    refusing_fetch_stub,
)

fw = load("fleet-watch")


class TheOrderIsFetchThenGateThenDemo(unittest.TestCase):
    """Every command in the gate, recorded in the order the sweep ran it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"
        self.marker = self.tmp / "fetch-ran"

        # Each stub logs `program:subcommand` and exits 0, so one log holds the whole gate.
        #
        # `showboat:extract` is a **fourth** call, added with `demo_verdict` in the transcript rule, and it is
        # here because this test's whole method is watching the sequence rather than reading the
        # code that claims it. `showboat extract` parses the markdown and prints the commands that
        # would rebuild it; it executes nothing, so it cannot bind a port and cannot need the
        # shared sources — which is why it is safe after the fetch and does not weaken the
        # fetch-first claim these assertions exist to hold. It is logged rather than hidden so
        # that a future subcommand added to the demo step shows up here as a diff on this list
        # instead of silently slipping past a check that only looks at `verify`.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for program in ("go", "make", "showboat"):
            stub = bindir / program
            stub.write_text(f'#!/bin/sh\nprintf "{program}:%s\\n" "$1" >> "{self.log}"\n')
            stub.chmod(0o755)

        real_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{real_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", real_path)

    # -- the fixture ---------------------------------------------------------------

    def worktree(self, *, name: str = "wt", go: bool = True, demo: bool = True,
                 shared: tuple[str, ...] = (), fetch: str | None = None) -> str:
        """A real git worktree with a real fetch script, under a directory of its own.

        `name` gives each scenario its own `<name>/repo`, `<name>/wt` and `<name>/shared`, so two
        scenarios in one test cannot see each other's provisioning — which matters for the
        mutation below, where one run must still be looking at an unprovisioned tree.
        """
        root = self.tmp / name
        root.mkdir(parents=True, exist_ok=True)
        repo, wt = root / "repo", root / "wt"
        make_repo_with_a_branch(str(repo), str(wt), gate_files=not go,
                                branch="issue-94-order", fetch_script=fetch,
                                shared=tuple(shared))
        if go:
            # Only its presence matters: `go` is a stub on PATH, so nothing is compiled and
            # nothing is downloaded.
            (wt / "go.mod").write_text("module synthetic\n\ngo 1.22\n")
        if demo:
            (wt / "demo.md").write_text("# a demo\n")
        return str(wt)

    def calls(self) -> list[str]:
        """The commands run since the last read, then emptied."""
        if not self.log.exists():
            return []
        seen = self.log.read_text().split()
        self.log.write_text("")
        return seen

    # -- the claims ----------------------------------------------------------------

    def test_the_fetch_runs_before_vet_before_test_and_before_showboat(self):
        """THE ORDER. `showboat verify` reads `../shared`, so the fetch has to precede it.

        This is the case the ticket is about, watched end to end: the sources are absent, the
        fetch provisions them, and only then does anything try to build. A sweep that gated
        first and provisioned afterwards would produce `fetch, go:vet, go:test, showboat:verify,
        make:check` — the `make:check` last is the trap, because that is the command that
        creates `web/node_modules`.

        `showboat:extract` is last because `demo_verdict` asks `verify` first and `extract` only
        afterwards, to say how much the green covered. Both are after the fetch, which is the
        claim; `extract` executes nothing so it adds no ordering constraint of its own.
        """
        wt = self.worktree(fetch=fetch_stub(str(self.marker), str(self.log)))
        state, why = fw.gate(wt)

        self.assertEqual(self.calls(),
                         ["fetch", "go:vet", "go:test", "showboat:verify", "showboat:extract"],
                         "the fetch must precede every command that reads ../shared, and the "
                         "sweep must say which order it used")
        self.assertEqual(state, "GREEN", why)
        for name in ("go", "ui"):
            self.assertTrue((self.tmp / "wt" / "shared" / name).is_dir(),
                            f"the fetch ran but did not provision {name}: {why}")
        print(f"    go repo      -> {state}; fetch, vet, test, showboat")

    def test_the_fetch_runs_before_make_check_which_is_what_creates_web_node_modules(self):
        """Same order, for the other gate — and this one is where the hang lives.

        `make check` runs the frontend install, so a tree that has run it has `web/node_modules`;
        the fetch then ends in `pnpm install --frozen-lockfile` in a directory that already has
        one, and pnpm asks `Proceed?` instead of failing. Measured in `media`: 19
        seconds green without it, still running after ten minutes with it.
        """
        wt = self.worktree(name="mk", go=False, demo=False,
                           fetch=fetch_stub(str(self.marker), str(self.log)))
        state, why = fw.gate(wt)

        self.assertEqual(self.calls(), ["fetch", "make:check"],
                         "provisioning after `make check` is a pnpm install in a directory "
                         "that already has node_modules, which waits rather than fails")
        self.assertEqual(state, "GREEN", why)
        print(f"    make repo    -> {state}; fetch, make check")

    def test_a_tree_that_already_has_the_shared_sources_is_never_fetched(self):
        """The reuse decision, observed: not one call, and not one clone.

        All eighteen copies of the script in this family target
        `SHARED="${CI_PROJECT_DIR:-$(pwd)}/../shared"`, and every worker worktree is a sibling
        under one workspace root — so that is **one** shared directory for the whole fleet, not
        one per worktree. Fetching per sweep would mean four or five clones per pane per sweep
        into the same place, and the pnpm install at the end of the script is the half that
        hangs.
        """
        wt = self.worktree(shared=("go", "ui"),
                           fetch=fetch_stub(str(self.marker), str(self.log)))
        state, why = fw.gate(wt)

        self.assertEqual(self.calls(),
                         ["go:vet", "go:test", "showboat:verify", "showboat:extract"],
                         "nothing was missing, so the fetch must not have run")
        self.assertFalse(self.marker.exists(),
                         "the fetch script ran even though both sources were already present")
        self.assertEqual(state, "GREEN", why)
        self.assertIn("reused", why, f"and the row must record the reuse: {why}")
        print(f"    reused       -> {state}, and the fetch never ran")

    def test_a_refused_fetch_stops_the_sweep_rather_than_reporting_a_block(self):
        """Nothing runs after a refusal, because nothing can be believed before one.

        This is the row that manufactured work. Reporting `GATE-RED` sends a coordinator to gate
        a green branch, so the state is `NONE` — a name rather than a lie — and the reason is
        the fetch's own refusal, which names the variable that is missing.
        """
        wt = self.worktree(name="refused",
                           fetch=refusing_fetch_stub(str(self.marker), str(self.log)))
        state, why = fw.gate(wt)

        self.assertEqual(self.calls(), ["fetch"],
                         "vet, test and showboat must not run on a tree that could not be "
                         "provisioned; whatever they print would be a symptom")
        self.assertEqual(state, "NONE", f"a refused fetch is not a red gate: {why}")
        self.assertIn("fetch-shared-sources.sh", why,
                      f"the reason must name the missing fetch: {why!r}")
        self.assertIn("SHARED_SOURCES_TOKEN", why,
                      f"and carry the script's own refusal: {why!r}")
        self.assertNotIn("demo.md does not verify", why,
                         f"naming a transcript block is the defect: {why!r}")
        print(f"    refused      -> {state}: {why[:72]}")

    def test_the_fetch_list_is_read_from_the_script_and_not_hardcoded_to_the_family(self):
        """The list differs across the family, so it is parsed rather than assumed.

        `media` provisions three sources and `identity` four, and
        `meta` is in nobody's list until `web/vite.config.ts` started reading its
        `apps.json` at BUILD time. A hardcoded list would invent a source one repository does
        not provision and miss the reason another one does.

        So this repository's script fetches `alpha` alone — and a tree carrying
        `go` and `ui` is still **not** provisioned, so the sweep fetches.
        """
        script = ("#!/usr/bin/env bash\n"
                  "set -euo pipefail\n"
                  f'touch "{self.marker}"\n'
                  f'printf \'fetch\\n\' >> "{self.log}"\n'
                  'SHARED="${CI_PROJECT_DIR:-$(pwd)}/../shared"\n'
                  "for repo in alpha; do\n"
                  '  mkdir -p "$SHARED/$repo"\n'
                  "done\n")
        wt = self.worktree(name="alpha", shared=("go", "ui"), fetch=script)
        fw.gate(wt)
        self.assertEqual(self.calls()[0], "fetch",
                         "the sources this script fetches are absent, whatever else is beside "
                         "the worktree — so the list must come from the script, not from the "
                         "four names this family happens to use")

        # …and with `alpha` there, the same script is not run again. Same repository, same
        # script, one variable different; that is what makes it a claim about the parse.
        wt2 = self.worktree(name="beta", shared=("go", "ui", "alpha"),
                            fetch=script)
        state, why = fw.gate(wt2)
        self.assertNotIn("fetch", self.calls(),
                         "alpha is present, so this script must not be run")
        self.assertEqual(state, "GREEN", why)
        print("    parsed list  -> absent: fetch; present: reuse")

    def test_a_fetch_script_with_no_list_is_refused_rather_than_guessed(self):
        """No `for repo in ...; do` line means this cannot see what the script provisions.

        Guessing has two wrong answers and one right: fetch on every sweep (an outage per pane)
        or assume the worst (a permanent false red). The same reasoning as the family guard that
        refuses to pass against a script fetching nothing — so this refuses, **without running
        it**, and says so.
        """
        script = ("#!/usr/bin/env bash\n"
                  "set -euo pipefail\n"
                  f'touch "{self.marker}"\n'
                  f'printf \'fetch\\n\' >> "{self.log}"\n'
                  'echo "this script provisions nothing this sweep can name"\n')
        wt = self.worktree(name="nolist", fetch=script)
        state, why = fw.gate(wt)

        self.assertEqual(state, "NONE", why)
        self.assertEqual(self.calls(), [], "an unreadable fetch list must not be run on a guess")
        self.assertFalse(self.marker.exists())
        self.assertIn("for repo in", why, f"the reason must name what it could not read: {why!r}")
        print(f"    no list      -> {state}: {why[:72]}")

    # -- the mutation --------------------------------------------------------------

    def test_the_order_assertions_bite_when_the_fetch_step_is_removed(self):
        """THE MUTATION. Delete the provisioning step and require the assertions above to fail.

        A guard that cannot fail proves nothing, and the more dangerous case is a mutation that
        never applied and reads as a passing guard. So this runs the **same fixture** twice —
        once as shipped, once with `provision_shared_sources` replaced by the earlier behaviour,
        which is "no fetch, because there was never a fetch" — and requires the two logs to
        differ in the one way the claim is about.

        The earlier behaviour is expressed as the function that did not exist, rather than as a
        copy of `gate`: a second copy of the gate is a second implementation, and it would pass
        while `gate()` stayed broken.
        """
        good = self.worktree(name="as-shipped",
                             fetch=fetch_stub(str(self.marker), str(self.log)))
        fw.gate(good)
        shipped = self.calls()
        self.assertEqual(shipped[0], "fetch",
                         f"the shipped sweep must fetch first; it logged {shipped}")

        real = fw.provision_shared_sources
        fw.provision_shared_sources = lambda wt: ("NOT-NEEDED", "the fetch step has been deleted")
        try:
            mutant = self.worktree(name="step-deleted",
                                   fetch=fetch_stub(str(self.marker), str(self.log)))
            fw.gate(mutant)
        finally:
            fw.provision_shared_sources = real

        deleted = self.calls()
        self.assertEqual(shipped,
                         ["fetch", "go:vet", "go:test", "showboat:verify", "showboat:extract"])
        self.assertEqual(deleted,
                         ["go:vet", "go:test", "showboat:verify", "showboat:extract"],
                         "the mutation must not have applied, or this proves nothing")
        self.assertNotIn("fetch", deleted,
                         "deleting the fetch step is the defect, and the assertions above are "
                         "what holds it")
        print("    step deleted -> no fetch; the order assertion above bites")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--explain":
        print(__doc__)
        sys.exit(0)
    unittest.main(verbosity=2)
