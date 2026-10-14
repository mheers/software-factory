#!/usr/bin/env python3
"""The end-to-end smoke: a scratch workspace, real git, the real scripts.

The unit tests hold each behaviour against stubs; this holds the seams where they meet — the
configuration the scripts read, the workspace they walk, the git they run, and the record they
write. Two synthetic repositories, **real** git and **real** `make`:

- `alpha` — a worker branch one commit ahead whose gate is green;
- `beta` — a worker branch one commit ahead whose gate fails, and a failing `main` too.

Then,

1. `fleet-watch.py --json` must report the green worker `MERGEABLE` and the red one `GATE-RED`
   — the two rows the coordinator trusts;
2. `main_sweep.py --dry-run` must write its record for a **complete** run (a dry run still
   records — cron lives on that), and a **partial** run (`--repo alpha`) must leave the record
   byte-identical: a record holding only the repositories it read is a silencer.

The fixture machinery is imported from `test_fleet_watch.py` rather than re-implemented, for
the reason `test_merge_gate.py` already gives: a second copy of the fixture is free to disagree
with the first.

Run: `python3 scripts/test_smoke.py`. Real git/make in a temp directory; no network, no herdr.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_fleet_watch import make_repo_with_a_branch  # noqa: E402

FLEET_WATCH = HERE / "fleet-watch.py"
MAIN_SWEEP = HERE / "main_sweep.py"


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


RED_MAKEFILE = "check:\n\t@echo 'assertion failed: the gate is red' >&2; exit 1\n"


class TheFactoryEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(tempfile.mkdtemp(prefix="smoke-workspace."))
        self.addCleanup(shutil.rmtree, self.ws, True)
        # Deliberately no `shared/` here: a workspace with no shared modules is a legitimate
        # layout (decision 2026-10-14), and `live_panes()` has to sweep it without raising.
        # This used to create the directory because the walk raised without it — which is
        # exactly the failure this arrangement now covers.

        self.alpha = self.ws / "alpha"
        self.beta = self.ws / "beta"
        self.alpha_wt = str(self.ws / "alpha-issue-1")
        self.beta_wt = str(self.ws / "beta-issue-2")
        make_repo_with_a_branch(str(self.alpha), self.alpha_wt, gate_files=True,
                                branch="issue-1-alpha")
        make_repo_with_a_branch(str(self.beta), self.beta_wt, gate_files=True,
                                branch="issue-2-beta")

        # beta is the failing side. Its worker branch's gate fails (GATE-RED), and its main
        # fails too (the RED row the record must carry).
        Path(self.beta_wt, "Makefile").write_text(RED_MAKEFILE)
        git("add", "-A", cwd=Path(self.beta_wt))
        git("commit", "-qm", "refs #2: red gate", cwd=Path(self.beta_wt))
        Path(self.beta, "Makefile").write_text(RED_MAKEFILE)
        git("add", "-A", cwd=self.beta)
        git("commit", "-qm", "refs #2: main is red", cwd=self.beta)
        git("update-ref", "refs/remotes/origin/main", "main", cwd=self.beta)

        self.manifest = self.ws / ".fleet-state" / "fleet.json"
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps({self.alpha_wt: "wSM:p1", self.beta_wt: "wSM:p2"}))
        self.registry = self.ws / "registry.json"
        self.registry.write_text(json.dumps({"apps": [
            {"id": "alpha", "title": "Alpha", "repo": "alpha"},
            {"id": "beta", "title": "Beta", "repo": "beta"},
        ]}))
        self.record = self.ws / ".fleet-state" / "main-sweep.json"

    def env(self) -> dict:
        return {
            **os.environ,
            "FACTORY_WORKSPACE": str(self.ws),
            "FLEET_MANIFEST": str(self.manifest),
            "FLEET_WATCH_STATE": str(self.ws / ".fleet-state" / "watch.json"),
            "FLEET_MAIN_SWEEP": str(self.record),
            "FLEET_APPS_JSON": str(self.registry),
        }

    def test_the_sweep_reports_the_green_worker_and_the_red_one(self):
        run = subprocess.run([sys.executable, str(FLEET_WATCH), "--json"],
                             capture_output=True, text=True, env=self.env(), timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        rows = {r["worktree"]: r for r in json.loads(run.stdout)["panes"]}
        self.assertIn(self.alpha_wt, rows, run.stdout)
        self.assertIn(self.beta_wt, rows, run.stdout)
        self.assertEqual(rows[self.alpha_wt]["verdict"], "MERGEABLE", rows[self.alpha_wt])
        self.assertEqual(rows[self.beta_wt]["verdict"], "GATE-RED", rows[self.beta_wt])

    def test_main_sweep_records_a_complete_run_and_never_a_partial_one(self):
        first = subprocess.run([sys.executable, str(MAIN_SWEEP), "--dry-run"],
                               capture_output=True, text=True, env=self.env(), timeout=300)
        # Exit 1 is the sweep's job: a red main is what cron mails on, even in a dry run.
        self.assertEqual(first.returncode, 1, first.stderr or first.stdout)
        self.assertTrue(self.record.is_file(), first.stdout)
        record = self.record.read_text()
        self.assertIn('"alpha"', record, record[:400])
        self.assertIn('"GREEN"', record, record[:400])
        self.assertIn('"beta"', record, record[:400])
        self.assertIn('"RED"', record, record[:400])

        partial = subprocess.run([sys.executable, str(MAIN_SWEEP), "--dry-run", "--repo", "alpha"],
                                 capture_output=True, text=True, env=self.env(), timeout=300)
        self.assertEqual(partial.returncode, 0, partial.stderr or partial.stdout)
        self.assertEqual(self.record.read_text(), record,
                         "a partial run must leave the complete record byte-identical — a "
                         "record holding only the repositories it read is a silencer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
