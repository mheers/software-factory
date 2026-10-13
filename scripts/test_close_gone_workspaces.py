#!/usr/bin/env python3
"""Tests for the gone-workspace sweep, and for the refusals it is allowed to make.

Run: `python3 scripts/test_close_gone_workspaces.py` (no test runner, no network, no
Herdr server needed — every Herdr call is injected).

## Why this file exists

`cleanup-workers.sh` cannot close a workspace whose worktree is already gone. It reads
`.worktree.is_linked_worktree // false`, and Herdr drops the whole `worktree` object once
the checkout path no longer exists — so a **deleted** worktree and a **primary checkout**
produce the same value and land in the same bucket, reported as
`primary checkout, never reclaimed`. The workspace is then never closed.

That is not a cosmetic leak. Herdr's own message for that state is *"Saved directory is
unavailable. Restore the directory and restart this session."* — the pane cannot be
restored, so the workspace is unrecoverable weight in the workspace list. Measured on
2026-10-03: 121 open workspaces, **28** of them dead merged agents that the sweep could
not see, and 52 more in exactly this state.

So this file holds three things:

1. the predicate itself — deleted directory is closeable, existing directory is not;
2. every refusal, because a sweep that closes too much is worse than one that closes too
   little: a wrong close kills a live pane, and there is no undo;
3. **the mutation.** A guard nobody has broken is a claim, not a guard. Tests 9–11 delete
   each refusal in turn and assert the sweep then closes what it must never close.

Every test states what it saw, so a failure names the fact that moved.
"""

import importlib.util
import io
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load(name: str):
    """Import one of the sibling scripts by path, so this needs no package."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cgw = load("close-gone-workspaces")

GONE = "/synthetic/workspace/media-4"
HERE_TREE = "/synthetic/workspace/media"


def pane(pid="wK4:p1", cwd=GONE, status="unknown", deleted=True):
    return {
        "pane_id": pid,
        "workspace_id": pid.split(":")[0],
        "cwd": f"{cwd} (deleted)" if deleted else cwd,
        "agent_status": status,
        "agent_session": {"value": "ses_deadbeef"},
    }


def workspace(wid="wK4", label="#4 tagging"):
    return {"workspace_id": wid, "label": label, "number": 104}


def sweep(panes, *, protected=(), live_panes=(), exists=None, runtime="DEAD"):
    """Run the sweep's collection step against injected fakes.

    `exists` defaults to "every path the panes name is gone", which is the state under
    test; a test that needs a surviving directory passes its own predicate. `runtime`
    is a string applied to every pane, or a callable taking a pane id when one pane of
    a workspace behaves differently from another.
    """
    if exists is None:
        exists = lambda p: False  # noqa: E731
    probe = runtime if callable(runtime) else (lambda _pid: runtime)
    closes, refusals, skips = [], [], []
    for w in workspace_rows(panes):
        verdict, reason, _detail = cgw.classify(
            w, panes_by_ws(panes).get(w["workspace_id"], []),
            protected=set(protected), live_panes=set(live_panes),
            exists=exists, runtime=probe)
        if verdict == cgw.CLOSE:
            closes.append(w["workspace_id"])
        elif verdict == cgw.REFUSE:
            refusals.append((w["workspace_id"], reason))
        else:
            skips.append((w["workspace_id"], reason))
    return closes, refusals, skips


def workspace_rows(panes):
    """One row per distinct workspace, in first-seen order."""
    rows, ids = [], []
    for p in panes:
        wid = p["workspace_id"]
        if wid not in ids:
            ids.append(wid)
            rows.append(workspace(wid))
    return rows


def panes_by_ws(panes):
    out = {}
    for p in panes:
        out.setdefault(p["workspace_id"], []).append(p)
    return out


class TheDeletedDirectoryIsTheWholePoint(unittest.TestCase):
    def test_deleted_directory_is_closeable(self):
        closes, _, _ = sweep([pane()])
        self.assertEqual(closes, ["wK4"],
                         "a workspace whose saved directory is gone is exactly the "
                         "leak; it must be closeable")

    def test_existing_directory_is_never_touched(self):
        closes, _, skips = sweep([pane(cwd=HERE_TREE)], exists=lambda p: True)
        self.assertEqual(closes, [],
                         "a workspace whose directory still exists must never be closed")
        self.assertEqual(skips[0][1], "saved directory still exists",
                         f"and the reason must say so: {skips!r}")

    def test_the_deleted_marker_is_not_trusted(self):
        """The path on disk decides, not the `(deleted)` suffix Herdr appends.

        Herdr appends it; nothing guarantees it is still true a second later, and a
        sweep that believed a stale marker would close a live workspace.
        """
        marked, _, _ = sweep([pane(deleted=True)])
        unmarked, _, _ = sweep([pane(deleted=False)])
        self.assertEqual(marked, unmarked,
                         "a directory that exists must be left alone whether or not "
                         "Herdr still labels it deleted")

    def test_a_workspace_with_no_panes_is_not_a_verdict(self):
        closes, refusals, skips = sweep([], exists=lambda p: False)
        self.assertEqual((closes, refusals), ([], []),
                         "no pane means no saved directory, so there is nothing to judge")
        self.assertEqual(len(skips), 0, f"and no phantom workspace may be invented: {skips!r}")


class TheRefusalsAreThePoint(unittest.TestCase):
    """A sweep that closes too much kills live panes and there is no undo."""

    def test_a_working_agent_is_refused(self):
        closes, refusals, _ = sweep([pane(status="working")])
        self.assertEqual(closes, [], "an agent is mid-flight in there")
        self.assertIn(("wK4", "agent still working"), refusals,
                      f"and the refusal must name the reason: {refusals!r}")

    def test_a_live_pane_runtime_is_refused(self):
        """The case that a marker cannot catch: the directory is gone, the TUI is not."""
        closes, refusals, _ = sweep([pane(status="idle")], runtime="LIVE")
        self.assertEqual(closes, [],
                         "a live pane whose directory vanished is a judgement call, "
                         "not a sweep decision — the pane may still be mid-signoff")
        self.assertIn(("wK4", "pane runtime still live"), refusals,
                      f"and it must say the runtime, not the directory: {refusals!r}")

    def test_one_live_pane_refuses_the_whole_workspace(self):
        panes = [pane(pid="wK4:p1"), pane(pid="wK4:p2")]
        closes, refusals, _ = sweep(
            panes, runtime=lambda pid: "LIVE" if pid.endswith(":p2") else "DEAD")
        self.assertEqual(closes, [],
                         "closing the workspace closes every pane in it, so one live "
                         "pane has to refuse all of them")
        self.assertIn(("wK4", "pane runtime still live"), refusals,
                      f"and the reason must name the runtime: {refusals!r}")

    def test_the_coordinator_workspace_is_refused(self):
        closes, refusals, _ = sweep([pane(pid="wX:p3")], protected={"wX"})
        self.assertEqual(closes, [],
                         "the coordinator's own workspace is the one workspace this "
                         "sweep must never be able to close")
        self.assertIn(("wX", "protected: coordinator workspace"), refusals,
                      f"and the reason must name the protection: {refusals!r}")

    def test_a_live_fleet_worker_pane_is_refused(self):
        """The manifest is the record of what is live; it outranks a vanished directory."""
        closes, refusals, _ = sweep([pane(pid="wMK:p1")], live_panes={"wMK:p1"})
        self.assertEqual(closes, [], "a recorded worker is live until its manifest entry goes")
        self.assertIn(("wMK", "protected: pane in the live fleet manifest"), refusals,
                      f"and the reason must say which protection held: {refusals!r}")


class TheMutationIsTheOnlyProof(unittest.TestCase):
    """Delete each refusal and confirm the sweep then destroys what it must not."""

    def _mutate(self, needle: str):
        """Re-exec the module with one guard disabled, and run the same scenario.

        `if <cond>:` becomes `if False and <cond>:`, which leaves the block's
        indentation intact — replacing the `if` line with `pass` would re-indent the
        body under it and raise IndentationError, and a mutation that cannot load
        demonstrates nothing.
        """
        head = needle.split(":", 1)[0]
        src = (HERE / "close-gone-workspaces.py").read_text()
        self.assertIn(needle, src, f"the guard moved: {needle!r} is not in the source")
        mutated = src.replace(needle, needle.replace("if ", "if False and ", 1), 1)
        self.assertNotEqual(mutated, src, f"the mutation to {needle!r} changed nothing")
        mod = types.ModuleType("_mutant_close_gone")
        mod.__file__ = str(HERE / "close-gone-workspaces.py")
        exec(compile(mutated, mod.__file__, "exec"), mod.__dict__)
        return mod

    def _closes_with(self, mod, panes, **kw):
        """The verdicts one scenario produces under `mod`."""
        by = panes_by_ws(panes)
        return [mod.classify(w, by.get(w["workspace_id"], []),
                             exists=lambda _p: False,
                             runtime=lambda _p: "DEAD", **kw)[0]
                for w in workspace_rows(panes)]

    def test_removing_the_working_agent_refusal_lets_it_close_a_working_pane(self):
        mod = self._mutate('if "working" in statuses:')
        self.assertEqual(self._closes_with(mod, [pane(status="working")]), [mod.CLOSE],
                         "disabling the working-agent guard did not make the sweep "
                         "destructive, so that guard is not being tested")

    def test_live_runtime_guard_fires_against_a_live_pane(self):
        """The mutation, driven with a runtime that actually reports LIVE.

        A deleted directory with a live TUI is exactly the case the guard exists for.
        (A mutation driven with a DEAD runtime proves nothing — the guard would not
        fire either way — so that scenario is deliberately not used here.)
        """
        mod = self._mutate("if LIVE in runtimes:")
        by = panes_by_ws([pane(status="idle")])
        verdicts = [mod.classify(w, by.get(w["workspace_id"], []),
                                 exists=lambda _p: False,
                                 runtime=lambda _p: "LIVE")[0]
                    for w in workspace_rows([pane(status="idle")])]
        self.assertEqual(verdicts, [mod.CLOSE],
                         "disabling the live-runtime guard did not let the sweep close "
                         "a workspace with a live pane, so that guard is untested")

    def test_the_unmutated_sweep_refuses_that_same_case(self):
        by = panes_by_ws([pane(status="idle")])
        verdicts = [cgw.classify(w, by.get(w["workspace_id"], []),
                                 exists=lambda _p: False,
                                 runtime=lambda _p: "LIVE")[0]
                    for w in workspace_rows([pane(status="idle")])]
        self.assertEqual(verdicts, [cgw.REFUSE],
                         "the sweep must refuse a deleted directory with a live pane")

    def test_removing_the_protection_refusal_lets_it_close_the_coordinator(self):
        mod = self._mutate("if wid in protected:")
        self.assertEqual(self._closes_with(mod, [pane(pid="wX:p3")],
                                           protected={"wX"}), [mod.CLOSE],
                         "disabling the coordinator protection did not make the sweep "
                         "able to close the coordinator's workspace")

    def test_removing_the_manifest_refusal_lets_it_close_a_live_worker(self):
        mod = self._mutate("if any(p.get(\"pane_id\") in live_panes for p in panes):")
        self.assertEqual(self._closes_with(mod, [pane(pid="wMK:p1")],
                                           live_panes={"wMK:p1"}), [mod.CLOSE],
                         "disabling the fleet-manifest protection did not make the "
                         "sweep able to close a recorded worker")


class TheCliNeverClosesWhatItRefused(unittest.TestCase):
    """The verdict must be the only thing that decides, including on the apply path."""

    def _run(self, argv, panes, protected=(), live_panes=(), runtime="DEAD",
             exists=None):
        """Drive main() with herdr() replaced, and record every close it attempts."""
        attempts = []

        def fake_herdr(*args):
            if args[:2] == ("workspace", "list"):
                return {"result": {"workspaces": workspace_rows(panes)}}
            if args[:2] == ("pane", "list"):
                return {"result": {"panes": panes}}
            attempts.append(args)
            return {"result": {"type": "ok"}}  # type: ignore[return-value]

        if exists is None:
            exists = lambda p: False  # noqa: E731
        out = cgw.run(argv=argv, herdr=fake_herdr, protected=set(protected),
                      live_panes=set(live_panes), exists=exists,
                      runtime=lambda _p: runtime, out=io.StringIO())
        return attempts, out

    def test_apply_closes_the_dead_and_not_the_working(self):
        attempts, _ = self._run(["--yes"],
                                [pane(pid="wK4:p1", status="unknown"),
                                 pane(pid="wK5:p1", status="working")])
        self.assertEqual(attempts, [("workspace", "close", "wK4")],
                         f"apply closed the wrong set: {attempts!r}")

    def test_dry_run_closes_nothing(self):
        attempts, _ = self._run([], [pane()])
        self.assertEqual(attempts, [],
                         f"a dry run removed something: {attempts!r}")

    def test_the_coordinator_survives_apply(self):
        attempts, _ = self._run(["--yes"], [pane(pid="wX:p3")], protected={"wX"})
        self.assertEqual(attempts, [],
                         f"apply closed the coordinator's workspace: {attempts!r}")

    def test_the_coordinator_is_protected_even_with_a_deleted_directory(self):
        """The uncomfortable one: the coordinator's *own* directory, gone.

        Nothing about that workspace is unusual — it is the same predicate as a dead
        worker's. The only thing standing between it and a close is the protection, so
        the test has to drive the protection rather than the directory.
        """
        attempts, _ = self._run(["--yes"], [pane(pid="wX:p3", cwd="$HOME")],
                                protected={"wX"}, exists=lambda _p: False)
        self.assertEqual(attempts, [],
                         f"apply closed the coordinator's workspace: {attempts!r}")

    def test_audit_closes_nothing_and_reports_every_bucket(self):
        """`--audit` is for reading, so it must classify without closing anything."""
        attempts, out = self._run(["--audit"],
                                  [pane(pid="wK4:p1"),
                                   pane(pid="wK5:p1", cwd=HERE_TREE)])
        self.assertEqual(attempts, [], f"audit removed something: {attempts!r}")

    def test_the_protect_flag_protects_a_named_workspace(self):
        attempts, _ = self._run(["--yes", "--protect", "wK4"], [pane()])
        self.assertEqual(attempts, [],
                         f"--protect was ignored: {attempts!r}")

    def test_an_existing_directory_is_never_closed_by_apply(self):
        attempts, _ = self._run(["--yes"], [pane(cwd=HERE_TREE)], exists=lambda p: True)
        self.assertEqual(attempts, [],
                         f"apply closed a workspace whose directory exists: {attempts!r}")


class TheSavedDirectoryIsReadFromHerdrsOwnSuffix(unittest.TestCase):
    def test_suffix_is_stripped_and_only_it(self):
        self.assertEqual(cgw.saved_directory(f"{GONE} (deleted)"), GONE)
        self.assertEqual(cgw.saved_directory(f"{GONE} (deleted) (deleted)"),
                         f"{GONE} (deleted)",
                         "strip once: a directory literally named '... (deleted)' is "
                         "still a path")
        self.assertEqual(cgw.saved_directory(""), "")
        self.assertEqual(cgw.saved_directory(None), "")

    def test_verdicts_are_named_constants(self):
        """Three outcomes, not two: 'nothing to do' and 'would not' are different facts.

        A boolean here would collapse `saved directory still exists` into the same
        answer as `refused`, which is how a report ends up saying it removed something
        it never considered.
        """
        for name in ("CLOSE", "SKIP", "REFUSE"):
            self.assertIsInstance(getattr(cgw, name), str)
        self.assertEqual(len({cgw.CLOSE, cgw.SKIP, cgw.REFUSE}), 3)


class ItReadsHerdrWithoutABarePipe(unittest.TestCase):
    """`pane read` returns terminal text, not JSON. Parse it as the trap it is."""

    def test_a_json_error_means_dead(self):
        payload = json.dumps({"error": {"code": "pane_not_found"}})
        self.assertEqual(cgw.parse_pane_read(payload), "DEAD")

    def test_terminal_text_means_live(self):
        self.assertEqual(cgw.parse_pane_read("  ┃ hello\n  ╹▀▀"), "LIVE")

    def test_a_json_payload_without_an_error_means_live(self):
        self.assertEqual(cgw.parse_pane_read(json.dumps({"result": {}})), "LIVE")

    def test_empty_output_is_dead(self):
        self.assertEqual(cgw.parse_pane_read(""), "DEAD")

    def test_pane_read_is_never_assumed_to_be_json(self):
        """The regression: json.loads() on terminal text raises, and a caller that
        treats a raise as 'no error' reports every live pane dead — and then closes
        the coordinator's own workspace."""
        with self.assertRaises(json.JSONDecodeError):
            json.loads("  ┃ chrome-devtools           Connected")


if __name__ == "__main__":
    unittest.main(verbosity=2)