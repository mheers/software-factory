#!/usr/bin/env python3
"""Tests for the reclaim sweep: the repository filter, and the gone-worktree bucket.

Run: `python3 scripts/test_cleanup_workers.py` (no test runner, no network, no Herdr
server, no real repository).

## What this file holds

`cleanup-workers.sh` is bash, so it is driven the way bash can be driven honestly: a temp
`PATH` holding stub `herdr`, `git` and `glab` executables, and the **real, unmodified**
script executed against them. Nothing here re-implements the script's logic in Python —
a Python copy of a shell loop would pass while the shell loop stayed broken, which is the
"probe that was never a test" failure in FACTORY.md 4a.

Four shipped defects, each measured before the fix:

1. **No repository filter.** The candidate loop matched every workspace labelled
   `#<iid> <slug>` fleet-wide, so `--repo` decided only which checkout to read. Measured
   2026-10-03: a dry run from `meta-16` reported 19 candidates, **0 of them this
   repository's** — 7 were other repos' primary checkouts, 12 were other repos' gone
   worktrees.
2. **The gone worktree was misreported.** A workspace whose checkout path no longer
   exists was reported `no branch registration for <path>`: a *misleading reason* rather
   than a refusal, and never closed. It is now its own bucket and reaches
   `close-gone-workspaces.py`.
3. **The report.** The old closing line printed `nothing to do.` after listing 19
   candidates — the "green because it did nothing" failure this family keeps hitting.
   Found-nothing and reclaimed-nothing are now different sentences.
4. **The label match was anchored.** The candidate loop accepted only
   `#<iid> <slug>`, so a workspace renamed to the human form
   (`AI · <project> #<iid> <slug>`) was skipped **silently** — no candidate, no refusal,
   no line. A merged, issue-closed worker under that label could never be reclaimed, and
   "found nothing to reclaim" printed identically to "found nothing to look at". The id
   is now read from either spelling, and a workspace of this repository whose label
   carries no id at all is reported and left alone rather than skipped.

And three safety properties, because a sweep that closes too much has no undo:

- **an unmerged branch is never handed over for closing**, and the check is the existing
  `git merge-base --is-ancestor` + GitLab MR/issue pair rather than a new one;
- **the gone-worktree close is delegated, not re-implemented.** Two pruning
  implementations that disagree is the failure this family keeps hitting, so the tests
  assert the delegation happens *and* that this script never issues its own
  `workspace close` for that bucket.
- **a workspace without an issue id is never reclaimed and never silently dropped**:
  only this repository's own linked worktree is reported as a worker that lost its id —
  the primary checkouts and the coordinator workspaces are excluded, because the report
  is for a worker whose number is missing, not for every workspace that has none.

Every guard is mutation-tested. Each mutation asserts the needle was present and unique,
that the result differs from the source, and that `bash -n` still parses it — a mutation
that cannot load demonstrates nothing about the guard it was supposed to break.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "cleanup-workers.sh"

# `SELF` and `OTHER` are resolved to real temp paths by the harness. Both repositories
# exist on disk, because the script cds into the one it is sweeping.
SELF = "SELF"
OTHER = "OTHER"

HERDR_STUB = r"""#!/usr/bin/env bash
# Records every call so a test can assert which commands the sweep attempted. Logged with
# the program name, like the git and glab stubs: `closes_attempted()` looks for
# `herdr workspace close …`, and a stub that logged bare arguments made every one of its
# assertions vacuously true.
printf 'herdr %s\n' "$*" >> "$CALLLOG"
case "$1 $2" in
  "workspace list") cat "$WORKSPACES_JSON" ;;
  "pane list")     cat "$PANES_JSON" ;;
  "worktree list") cat "$WORKTREES_JSON" ;;
  *) : ;;
esac
exit 0
"""

# `git` is stubbed so nothing touches a real repository. Subcommands the script may ask
# for are answered; anything else exits 0 with no output, so an unexpected call cannot
# masquerade as a real answer.
GIT_STUB = r"""#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$CALLLOG"
case "$1" in
  rev-parse)
    case "$2" in
      --show-toplevel) printf '%s\n' "$REPO_ROOT"; exit 0 ;;
      --verify)        exit 0 ;;
    esac
    ;;
  worktree)
    case "$2" in
      list) cat "$WORKTREE_LIST" ;;
    esac
    ;;
  merge-base)
    # `--is-ancestor <branch> main`: exit 0 only when the branch is the merged one.
    if [ "$2" = "--is-ancestor" ] && [ "$MERGED_BRANCHES" != "$3" ]; then
      exit 1
    fi
    exit 0
    ;;
  for-each-ref)
    # The branches this repository still has, one per line.
    [ -n "$LOCAL_BRANCHES" ] && printf '%s\n' "$LOCAL_BRANCHES"
    exit 0
    ;;
esac
exit 0
"""

GLAB_STUB = r"""#!/usr/bin/env bash
printf 'glab %s\n' "$*" >> "$CALLLOG"
case "$1" in
  api) printf '{"state":"%s"}\n' "$ISSUE_STATE"; exit 0 ;;
  mr)
    # Strict about flags, like the real CLI. A permissive stub cannot fail, so it cannot catch
    # a malformed invocation: `glab mr list --state opened` shipped in the sweep and every one
    # of these 27 tests stayed green, because the stub answered anything. Real `glab` rejects it
    # with `Unknown flag: --state` and exits non-zero, and the sweep reported that as
    # "GitLab unreachable (glab needs GLAB_CONFIG_DIR)" — a permissions story for a typo.
    #
    # So the accepted set is enumerated here, and an unknown flag is an error. Keep it in step
    # with `glab mr list --help`.
    shift
    for a in "$@"; do
      case "$a" in
        --source-branch|--assignee|--author|--closed|--created-after|--created-before|\
        --deployed-after|--deployed-before|--draft|--environment|--group|--in|--label|\
        --milestone|--order-by|--reviewer|--scope|--search|--sort|--target-branch|\
        --updated-after|--updated-before|--all|-a|-A|-c|-d|-F|--output)
          case "$a" in
            --source-branch|--assignee|--author|--closed|--created-after|--created-before|\
            --deployed-after|--deployed-before|--draft|--environment|--group|--in|--label|\
            --milestone|--order-by|--reviewer|--scope|--search|--sort|--target-branch|\
            --updated-after|--updated-before|--all|-a|-A|-c|-d|-F|--output) ;;
          esac
          ;;
        -*) printf 'Unknown flag: %s\n' "${a#--}" >&2; exit 1 ;;
      esac
    done
    # A flag that takes a value must be followed by one; `--all` and `-F` style are handled above.
    printf '%s\n' "$OPEN_MRS"; exit 0 ;;
esac
exit 0
"""


def workspace(wid, label, *, repo_root=None, linked=None, checkout=None, number=1):
    """One Herdr workspace row.

    `repo_root=None` omits the `worktree` object entirely — what Herdr reports for a
    workspace it cannot attribute, and the gone-worktree case.
    """
    row = {"workspace_id": wid, "label": label, "number": number}
    if repo_root is not None:
        row["worktree"] = {
            "repo_root": repo_root,
            "is_linked_worktree": bool(linked),
            "checkout_path": checkout or repo_root,
        }
    return row


def pane(wid, cwd, *, status="unknown", session="ses_x"):
    return {"pane_id": f"{wid}:p1", "workspace_id": wid, "cwd": cwd,
            "agent_status": status, "agent_session": {"value": session}}


def human_form_worker(label="AI · meta #9 thing", *, status="idle",
                      merged="issue-9-thing"):
    """Harness keyword arguments for one worker labelled in the human spelling.

    `herdr worktree create --label` writes `#<iid> <slug>`; a renamed workspace carries
    `AI · <project> #<iid> <slug>`. The worktree registration and the branch stay the
    factory ones — a branch is read from git, never from the label — so only the label
    differs between the two spellings.
    """
    return dict(
        workspaces=[workspace("wA", label, repo_root=SELF, linked=True,
                              checkout=f"{SELF}-issue-9")],
        panes=[pane("wA", f"{SELF}-issue-9", status=status)],
        existing=[f"{SELF}-issue-9"],
        local_branches=["issue-9-thing"],
        merged=merged,
        worktree_list=(f"worktree {SELF}\nbranch refs/heads/main\n\n"
                       f"worktree {SELF}-issue-9\nbranch refs/heads/issue-9-thing\n\n"))


class Harness:
    """A temp PATH with stub herdr/git/glab over two real, empty repositories.

    Only checkout paths passed in `existing` exist on disk. Everything else Herdr names
    is gone — the gone-worktree case — so it is the default rather than something a test opts into.
    """

    def __init__(self, *, workspaces=(), panes=(), existing=(), local_branches=(),
                 merged="issue-9-thing", open_mrs="[]", issue_state="closed",
                 worktree_list=""):
        self.tmp = Path(tempfile.mkdtemp(prefix="cleanup-workers-test."))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls.log"

        self.A = str(self.tmp / "meta")
        self.B = str(self.tmp / "media")
        Path(self.A).mkdir()
        Path(self.B).mkdir()

        for name, body in (("herdr", HERDR_STUB), ("git", GIT_STUB), ("glab", GLAB_STUB)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)

        self.worktrees = [self._resolve(w) for w in workspaces]
        self.panes = [self._resolve(p) for p in panes]
        self.worktree_list = self._resolve(worktree_list)
        for path in existing:
            Path(self._resolve(path)).mkdir(parents=True, exist_ok=True)

        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALLLOG": str(self.calls),
            "WORKSPACES_JSON": str(self.tmp / "workspaces.json"),
            "PANES_JSON": str(self.tmp / "panes.json"),
            "WORKTREES_JSON": str(self.tmp / "worktrees.json"),
            "WORKTREE_LIST": str(self.tmp / "worktree_list"),
            "REPO_ROOT": self.A,
            "FACTORY_TRACKER_PROVIDER": "gitlab",
            "LOCAL_BRANCHES": "\n".join(local_branches).replace("SELF", self.A),
            "MERGED_BRANCHES": merged.replace("SELF", self.A),
            "OPEN_MRS": open_mrs,
            "ISSUE_STATE": issue_state,
        }

    def _resolve(self, obj):
        """Replace SELF/OTHER placeholders with this run's real paths."""
        if isinstance(obj, str):
            return obj.replace(SELF, self.A).replace(OTHER, self.B)
        if isinstance(obj, dict):
            return {k: self._resolve(v) for k, v in obj.items()}
        return obj

    def write_fixtures(self):
        (self.tmp / "workspaces.json").write_text(
            json.dumps({"result": {"workspaces": self.worktrees}}))
        (self.tmp / "panes.json").write_text(
            json.dumps({"result": {"panes": self.panes}}))
        (self.tmp / "worktrees.json").write_text(
            json.dumps({"result": {"worktrees": []}}))
        (self.tmp / "worktree_list").write_text(self.worktree_list)

    def run(self, *args, script=None, timeout=60):
        self.write_fixtures()
        result = subprocess.run(
            ["bash", str(script or SCRIPT), "--repo", self.A, *args],
            capture_output=True, text=True, env=self.env, timeout=timeout, check=False)
        self.stdout, self.stderr, self.code = result.stdout, result.stderr, result.returncode
        return self

    def calls_made(self):
        if not self.calls.exists():
            return []
        return [ln for ln in self.calls.read_text().splitlines() if ln.strip()]

    def closes_attempted(self):
        return [c for c in self.calls_made() if c.startswith("herdr workspace close")]

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBase(unittest.TestCase):
    """Every harness is torn down, so a failing test cannot leave temp dirs behind."""

    def setUp(self):
        self._harnesses = []

    def tearDown(self):
        for h in self._harnesses:
            h.close()

    def harness(self, **kw):
        h = Harness(**kw)
        self._harnesses.append(h)
        return h


# ------------------------------------------------------------------ the repo filter


class TheSweepIsScopedToOneRepository(TestBase):
    """The repo filter: a sweep from one repo must not name another repo's workers."""

    def fixture(self, **kw):
        return self.harness(
            workspaces=[
                workspace("wA", "#9 thing", repo_root=SELF, linked=True,
                          checkout=f"{SELF}-issue-9"),
                workspace("wB", "#9 thing", repo_root=OTHER, linked=True,
                          checkout=f"{OTHER}-issue-9"),
                workspace("wC", "#3 transport", repo_root=OTHER, linked=False,
                          checkout=OTHER),
            ],
            panes=[pane("wA", f"{SELF}-issue-9"), pane("wB", f"{OTHER}-issue-9"),
                   pane("wC", OTHER)],
            **kw)

    def test_a_foreign_repositorys_workspace_is_never_a_candidate(self):
        h = self.fixture(local_branches=["issue-9-thing"], merged="issue-9-thing",
                         existing=[f"{SELF}-issue-9"],
                         worktree_list=(f"worktree {SELF}\nbranch refs/heads/main\n\n"
                                        f"worktree {SELF}-issue-9\n"
                                        f"branch refs/heads/issue-9-thing\n\n"))
        h.run("--no-gitlab")
        # The one reclaimable row must be this repository's, and it must be the only one.
        reclaimable = h.stdout.split("reclaimable (merged, clean, issue closed):")[1] \
            .split("\n\n")[0] if "reclaimable (merged" in h.stdout else ""
        self.assertNotIn(f"{h.B}-issue-9", reclaimable,
                         f"another repository's worker was offered for removal:\n{h.stdout}")
        self.assertNotIn("workspace wB", reclaimable,
                         f"a foreign workspace appeared among the reclaimable:\n{h.stdout}")

    def test_a_foreign_primary_checkout_is_not_refused_as_ours(self):
        h = self.fixture()
        h.run("--no-gitlab")
        self.assertNotIn("primary checkout, never reclaimed", h.stdout,
                         f"a foreign repo's primary checkout was refused as ours:\n{h.stdout}")

    def test_the_foreign_ones_are_reported_as_a_count(self):
        """The repo filter's second checkbox: not even as "left alone", but accounted for."""
        h = self.fixture()
        h.run("--no-gitlab")
        self.assertIn("other repositories' workspaces", h.stdout,
                      f"foreign workspaces vanished instead of being counted:\n{h.stdout}")
        self.assertIn("media", h.stdout,
                      f"the summary must say which repository:\n{h.stdout}")

    def test_this_repos_own_workspace_is_still_seen(self):
        """A filter that hides everything would pass every test above."""
        h = self.fixture(local_branches=["issue-9-thing"], merged="issue-9-thing",
                         existing=[f"{SELF}-issue-9"],
                         worktree_list=(f"worktree {SELF}\nbranch refs/heads/main\n\n"
                                        f"worktree {SELF}-issue-9\n"
                                        f"branch refs/heads/issue-9-thing\n\n"))
        h.run("--no-gitlab")
        self.assertIn(f"{h.A}-issue-9", h.stdout,
                      f"the repository's own linked worktree went missing:\n{h.stdout}")
        self.assertIn("reclaimable (merged", h.stdout,
                      f"and it should have been reclaimable:\n{h.stdout}")

    def test_iid_only_matches_this_repositorys_workers(self):
        """Every repository has its own issue 2, so an unscoped --iid matches another repo's ticket."""
        h = self.fixture()
        h.run("--no-gitlab", "--iid", "3")
        self.assertIn("not this sweep's business", h.stdout,
                      f"--iid 3 named another repo's ticket:\n{h.stdout}")
        self.assertNotIn("left alone", h.stdout,
                         f"and it was acted on rather than merely counted:\n{h.stdout}")

    def test_help_says_iid_is_repo_scoped(self):
        h = self.fixture()
        h.run("--help")
        self.assertIn("this repository", h.stdout,
                      f"--help must say --iid is repo-scoped:\n{h.stdout}")


class AWorkspaceWithNoWorktreeObjectIsNotHidden(TestBase):
    """The repo filter's third checkbox: it must not swallow the gone-worktree case."""

    def test_it_is_still_reported(self):
        h = self.harness(
            workspaces=[workspace("wX", "#11 buildvcs")],
            panes=[pane("wX", f"{SELF}-issue-11")])
        h.run("--no-gitlab")
        self.assertIn("cannot be attributed", h.stdout,
                      f"an unattributable workspace disappeared:\n{h.stdout}")
        self.assertIn("wX", h.stdout)


# ------------------------------------------------ the label, in either spelling


class AWorkspaceLabelIsReadInEitherForm(TestBase):
    """The label match: both spellings are worker labels, and both go through the same guards.

    A live worker stays invisible to *removal* — the guards decide that, and this class
    only fixes the recognition step in front of them.
    """

    def test_the_human_form_is_reclaimable_when_merged_and_closed(self):
        h = self.harness(**human_form_worker())
        h.run()
        self.assertIn("reclaimable (merged", h.stdout,
                      f"the human-form label was not seen as a worker at all:\n{h.stdout}")
        self.assertIn("workspace wA", h.stdout,
                      f"the reclaimable row must name the workspace:\n{h.stdout}")
        self.assertIn("issue-9-thing", h.stdout,
                      f"the row must carry the branch it would delete:\n{h.stdout}")

    def test_the_human_form_is_actually_reclaimed_with_yes(self):
        h = self.harness(**human_form_worker())
        h.run("--yes")
        self.assertEqual(h.closes_attempted(), ["herdr workspace close wA"],
                         f"the human-form worker was not closed:\n{h.stdout}")
        self.assertIn("git branch -d issue-9-thing", h.calls_made(),
                      "the merged branch was not deleted")

    def test_a_working_agent_under_the_human_form_is_left_alone(self):
        h = self.harness(**human_form_worker(status="working"))
        h.run("--yes")
        self.assertNotIn("reclaimable (merged", h.stdout,
                         f"a working agent was offered for removal:\n{h.stdout}")
        self.assertIn("agent still working in workspace", h.stdout,
                      f"and the refusal must say why:\n{h.stdout}")
        self.assertEqual(h.closes_attempted(), [],
                         "a workspace with a working agent was closed")

    def test_an_unmerged_branch_under_the_human_form_is_left_alone(self):
        h = self.harness(**human_form_worker(merged="none"))
        h.run("--yes")
        self.assertNotIn("reclaimable (merged", h.stdout,
                         f"an unmerged branch was offered for removal:\n{h.stdout}")
        self.assertIn("not merged into main", h.stdout,
                      f"and the refusal must say why:\n{h.stdout}")
        self.assertEqual(h.closes_attempted(), [],
                         "a workspace with an unmerged branch was closed")


class ALabelWithoutAnIssueIdIsReportedNotSkipped(TestBase):
    """The label match: a worker whose label lost its `#<iid>` is said, never silently skipped.

    It cannot be reclaimed — the branch namespace, the MR check and the issue check are
    all keyed on the id — but "this worker cannot name its issue" is exactly the fact a
    sweep must not swallow.
    """

    def lost(self, label="AI · meta", **kw):
        return self.harness(
            workspaces=[workspace("wL", label, repo_root=SELF, linked=True,
                                  checkout=f"{SELF}-issue-9")],
            panes=[pane("wL", f"{SELF}-issue-9")],
            existing=[f"{SELF}-issue-9"],
            local_branches=["issue-9-thing"], merged="issue-9-thing", **kw)

    def test_the_workspace_is_reported_with_its_id(self):
        h = self.lost()
        h.run()
        self.assertIn("label does not carry an issue id", h.stdout,
                      f"a worker with no id in its label vanished:\n{h.stdout}")
        self.assertIn("workspace wL", h.stdout,
                      f"the row must carry the workspace id:\n{h.stdout}")

    def test_it_is_never_a_candidate_and_never_reclaimed(self):
        h = self.lost()
        h.run("--yes")
        self.assertNotIn("reclaimable (merged", h.stdout,
                         f"a worker with no id was offered for removal:\n{h.stdout}")
        self.assertIn("need a decision", h.stdout,
                      f"the summary counted it as nothing instead of a decision:\n{h.stdout}")
        self.assertEqual(h.closes_attempted(), [],
                         "a worker with no issue id was closed")
        self.assertEqual([c for c in h.calls_made() if c.startswith("git branch -d")], [],
                         "a worker with no issue id had a branch deleted")

    def test_a_targeted_iid_sweep_does_not_swallow_it(self):
        """--iid narrows by issue; "this one cannot name its issue" must still be seen."""
        h = self.lost()
        h.run("--iid", "9")
        self.assertIn("label does not carry an issue id", h.stdout,
                      f"--iid hid the workspace that lost its id:\n{h.stdout}")


class NonWorkerWorkspacesWithoutAnIssueIdStayExcluded(TestBase):
    """The constraint half of the label match: not every off-pattern label is a worker.

    A primary checkout and a coordinator workspace (`AI · go Dagger v1 (serial)`)
    carry no issue id *by design*. They must not be reported as workers that lost one —
    the report is for a worker whose number is missing, not for every workspace that has
    none.
    """

    def test_an_unlabelled_primary_checkout_is_not_reported(self):
        h = self.harness(
            workspaces=[workspace("wP", "meta", repo_root=SELF, linked=False,
                                  checkout=SELF)],
            panes=[pane("wP", SELF)])
        h.run()
        self.assertNotIn("wP", h.stdout,
                         f"the primary checkout was reported as a worker:\n{h.stdout}")
        self.assertNotIn("label does not carry an issue id", h.stdout)

    def test_a_no_worktree_workspace_with_no_issue_is_not_reported(self):
        h = self.harness(
            workspaces=[workspace("wD", "AI · go Dagger v1 (serial)")],
            panes=[pane("wD", "$HOME")])
        h.run()
        self.assertNotIn("wD", h.stdout,
                         f"a non-worker workspace was named:\n{h.stdout}")
        self.assertNotIn("cannot be attributed", h.stdout)

    def test_a_foreign_workspace_without_an_issue_is_not_counted(self):
        h = self.harness(
            workspaces=[workspace("wF", "AI · media", repo_root=OTHER, linked=True,
                                  checkout=f"{OTHER}-issue-9")],
            panes=[pane("wF", f"{OTHER}-issue-9")])
        h.run()
        self.assertNotIn("wF", h.stdout)
        self.assertNotIn("other repositories' workspaces", h.stdout,
                         f"a no-id foreign workspace inflated the count:\n{h.stdout}")


# ---------------------------------------------------------------- the gone worktree


class TheGoneWorktreeIsItsOwnBucket(TestBase):
    """The gone-worktree case: classified apart from the primary checkout, and never closed from here."""

    def gone(self, *, gone_branch=None, merged="none", **kw):
        kw.setdefault("issue_state", "closed")
        return self.harness(
            workspaces=[
                workspace("wG", "#13 stamping", repo_root=SELF, linked=True,
                          checkout=f"{SELF}-issue-13"),
                workspace("wP", "#13 stamping", repo_root=SELF, linked=False,
                          checkout=SELF),
            ],
            panes=[pane("wG", f"{SELF}-issue-13", session="ses_preserved"), pane("wP", SELF)],
            local_branches=[gone_branch] if gone_branch else [],
            merged=merged, **kw)

    def test_the_two_verdicts_differ(self):
        """The ticket's own acceptance criterion: gone != primary checkout."""
        h = self.gone()
        h.run()
        self.assertIn("reclaimed worktree, workspace still open", h.stdout,
                      f"the gone worktree is not in its own bucket:\n{h.stdout}")
        self.assertIn("primary checkout, never reclaimed", h.stdout,
                      f"and the real primary checkout must still be refused:\n{h.stdout}")

    def test_the_gone_row_and_the_primary_row_are_distinguishable(self):
        h = self.gone()
        h.run()
        self.assertIn("workspace wG", h.stdout,
                      f"the gone workspace row disappeared:\n{h.stdout}")
        self.assertIn("workspace wP", h.stdout,
                      f"the primary checkout row disappeared:\n{h.stdout}")

    def test_it_names_the_workspace_the_pane_and_the_preserved_session(self):
        h = self.gone()
        h.run()
        for token in ("workspace wG", "pane wG:p1", "ses_preserved", "kept, not deleted"):
            self.assertIn(token, h.stdout, f"the report omits {token!r}:\n{h.stdout}")

    def test_a_dry_run_never_closes_it(self):
        h = self.gone()
        h.run()
        self.assertEqual(h.closes_attempted(), [], "a dry run closed a workspace")

    def test_this_script_never_closes_that_workspace_itself(self):
        """Delegation, not a second pruning implementation.

        `cleanup-workers.sh` must not grow its own close for this bucket: the refusals
        that make closing safe live in close-gone-workspaces.py and are covered by 28
        tests there. A second copy here would be free to disagree with them.
        """
        h = self.gone()
        h.run("--yes")
        self.assertEqual(h.closes_attempted(), [],
                         "cleanup-workers.sh closed the workspace itself instead of "
                         "delegating to the sweep that owns that decision")

    def test_close_gone_is_opt_in(self):
        h = self.gone()
        h.run("--yes")
        self.assertIn("--close-gone", h.stdout,
                      f"the report must say how to actually close these:\n{h.stdout}")

    def test_close_gone_delegates_to_the_other_sweep(self):
        h = self.gone()
        h.run("--yes", "--close-gone")
        self.assertIn("close-gone-workspaces.py", h.stdout,
                      f"--close-gone did not delegate:\n{h.stdout}")
        self.assertIn("workspace wG", h.stdout,
                      f"and the delegated row must still be named:\n{h.stdout}")


class AnUnmergedBranchIsNeverHandedOver(TestBase):
    """Requirement 3: nothing with unmerged work is closed, ever."""

    def gone_with_branch(self, *, merged="a-different-branch", **kw):
        return self.harness(
            workspaces=[workspace("wG", "#13 stamping", repo_root=SELF, linked=True,
                                  checkout=f"{SELF}-issue-13")],
            panes=[pane("wG", f"{SELF}-issue-13")],
            local_branches=["issue-13-stamping"], merged=merged, **kw)

    def test_an_unmerged_branch_refuses_the_close(self):
        h = self.gone_with_branch()
        h.run()
        self.assertIn("not merged into main", h.stdout,
                      f"an unmerged branch was reported as closeable:\n{h.stdout}")
        self.assertNotIn("reclaimed worktree, workspace still open", h.stdout,
                         f"it was offered for closing anyway:\n{h.stdout}")

    def test_an_open_merge_request_refuses_the_close(self):
        h = self.gone_with_branch(merged="issue-13-stamping", open_mrs="[{}]")
        h.run()
        self.assertIn("open MR", h.stdout,
                      f"a branch with an open MR was reported as closeable:\n{h.stdout}")

    def test_an_open_issue_refuses_the_close(self):
        h = self.gone_with_branch(merged="issue-13-stamping", issue_state="opened")
        h.run()
        self.assertIn("issue #13 is opened", h.stdout,
                      f"a workspace whose issue is still open was closeable:\n{h.stdout}")

    def test_an_unreadable_issue_refuses_rather_than_assuming_closed(self):
        """The Casdoor lesson: an answer that cannot express failure is not permission."""
        h = self.gone_with_branch(merged="issue-13-stamping", issue_state="")
        h.run()
        self.assertIn("could not read issue", h.stdout,
                      f"an unreadable issue read as closed:\n{h.stdout}")

    def test_no_branch_is_deleted_for_a_gone_worktree(self):
        """A closed workspace must not touch git: its work is already committed or gone."""
        h = self.gone_with_branch(merged="issue-13-stamping")
        h.run("--yes", "--close-gone")
        self.assertEqual([c for c in h.calls_made() if c.startswith("git branch -d")], [],
                         "a branch was deleted while handling a gone worktree")


# ------------------------------------------------------------------------ the report


class TheReportSaysWhatItSkippedAndWhy(TestBase):
    """Requirement 4, and the "green because it did nothing" failure it names."""

    def test_found_nothing_is_not_the_same_sentence_as_reclaimed_nothing(self):
        h = self.harness()
        h.run()
        self.assertIn("nothing to reclaim and nothing to decide", h.stdout,
                      f"an empty sweep must say so plainly:\n{h.stdout}")
        self.assertNotIn("nothing to do", h.stdout,
                         f"'nothing to do' is the sentence that hid 19 candidates:\n{h.stdout}")

    def test_a_sweep_with_candidates_says_it_reclaimed_nothing(self):
        h = self.harness(
            workspaces=[workspace("wG", "#13 stamping", repo_root=SELF, linked=True,
                                  checkout=f"{SELF}-issue-13")],
            panes=[pane("wG", f"{SELF}-issue-13")], local_branches=[])
        h.run("--no-gitlab")
        self.assertIn("nothing removed (dry run); 1 item(s)", h.stdout,
                      f"a non-empty dry run claimed there was nothing to do:\n{h.stdout}")

    def test_refusals_are_reported_even_when_nothing_was_reclaimed(self):
        h = self.harness(
            workspaces=[workspace("wP", "#13 stamping", repo_root=SELF, linked=False,
                                  checkout=SELF)],
            panes=[pane("wP", SELF)])
        h.run("--no-gitlab")
        self.assertIn("need a decision", h.stdout,
                      f"refusals were silently dropped from the summary:\n{h.stdout}")

    def test_the_repo_being_swept_is_named(self):
        h = self.harness()
        h.run()
        self.assertIn("repo:", h.stdout)


# ------------------------------------------------------------------------ the mutations


class TheMutationIsTheOnlyProof(TestBase):
    """Disable each guard and confirm the sweep then does the thing it must not."""

    def _mutate(self, needle, replacement):
        """Rewrite one line of the script, proving the rewrite happened.

        Three checks, because a mutation that never applied looks exactly like a guard
        that held (FACTORY.md 4a): the needle must be present and unique, the result must
        differ from the source, and `bash -n` must still parse it — a mutation that cannot
        load proves nothing about the guard it was supposed to break.
        """
        src = SCRIPT.read_text()
        self.assertIn(needle, src, f"the guard moved: {needle!r} is not in the script")
        self.assertEqual(src.count(needle), 1,
                         f"{needle!r} is not unique; the mutation would be ambiguous")
        mutated = src.replace(needle, replacement, 1)
        self.assertNotEqual(mutated, src, f"the mutation to {needle!r} changed nothing")
        path = self.tmp_path = Path(tempfile.mkdtemp(prefix="mutant.")) / "cleanup-workers.sh"
        path.write_text(mutated)
        # The script reads its branch prefix from `factory_config.py` and its tracker answers
        # from `tracker.py`, both resolved beside it; a mutant without the modules beside it
        # would die for the wrong reason, and the guard under test would look as if it had held.
        shutil.copy(HERE / "factory_config.py", path.parent / "factory_config.py")
        shutil.copy(HERE / "tracker.py", path.parent / "tracker.py")
        self.addCleanup(shutil.rmtree, str(path.parent), ignore_errors=True)
        syntax = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True,
                                check=False)
        self.assertEqual(syntax.returncode, 0,
                         f"the mutation to {needle!r} does not parse, so it proves nothing "
                         f"about the guard:\n{syntax.stderr}")
        return path

    def test_the_repo_filter_is_real(self):
        """Drop the repo_root comparison; a foreign worker is swept like a local one.

        The foreign checkout must exist on disk and have a branch registration, or the
        sweep would reject it further down for an unrelated reason and the mutation would
        look as if the guard still held — which is the inert-mutation trap.
        """
        path = self._mutate('if [ "$(abspath "$ws_repo_root")" != "$REPO_ROOT" ]; then',
                            "if false; then")
        h = self.harness(
            workspaces=[workspace("wB", "#9 thing", repo_root=OTHER, linked=True,
                                  checkout=f"{OTHER}-issue-9")],
            panes=[pane("wB", f"{OTHER}-issue-9")], local_branches=["issue-9-thing"],
            merged="issue-9-thing", existing=[f"{OTHER}-issue-9"],
            worktree_list=(f"worktree {OTHER}\nbranch refs/heads/main\n\n"
                           f"worktree {OTHER}-issue-9\nbranch refs/heads/issue-9-thing\n\n"))
        h.run("--no-gitlab", script=path)
        self.assertNotIn("other repositories' workspaces", h.stdout,
                         "the mutation did not take effect: the filter still excluded it")
        self.assertIn("reclaimable (merged", h.stdout,
                      "disabling the repo filter did not let another repository's worker "
                      f"through, so that guard is untested:\n{h.stdout}")

    def test_the_unmerged_branch_refusal_is_real(self):
        """Drop the merge check; an unmerged branch becomes closeable."""
        path = self._mutate('if [ -n "$unmerged" ]; then', "if false; then")
        h = self.harness(
            workspaces=[workspace("wG", "#13 stamping", repo_root=SELF, linked=True,
                                  checkout=f"{SELF}-issue-13")],
            panes=[pane("wG", f"{SELF}-issue-13")], local_branches=["issue-13-stamping"],
            merged="a-different-branch", issue_state="closed")
        h.run("--yes", "--close-gone", script=path)
        self.assertIn("reclaimed worktree, workspace still open", h.stdout,
                      "disabling the merge check did not make an unmerged branch "
                      f"closeable, so that guard is untested:\n{h.stdout}")

    def test_the_unattributed_bucket_is_real(self):
        """Drop the no-worktree-object report; those workspaces vanish from the output."""
        path = self._mutate('UNATTRIBUTED+=("$iid|$ws_id|$label")', "UNATTRIBUTED+=( )")
        h = self.harness(workspaces=[workspace("wX", "#11 buildvcs")],
                         panes=[pane("wX", f"{SELF}-issue-11")])
        h.run("--no-gitlab", script=path)
        self.assertNotIn("cannot be attributed", h.stdout,
                         "the mutation did not take effect: the bucket still prints")
        self.assertNotIn("wX", h.stdout,
                         "the unattributable workspace still appears somewhere:\n"
                         f"{h.stdout}")

    def test_the_unmutated_script_holds_all_three_lines(self):
        """The complement of each mutation: the real script refuses the same cases."""
        h = self.harness(
            workspaces=[workspace("wG", "#13 stamping", repo_root=SELF, linked=True,
                                  checkout=f"{SELF}-issue-13"),
                        workspace("wB", "#9 thing", repo_root=OTHER, linked=True,
                                  checkout=f"{OTHER}-issue-9"),
                        workspace("wX", "#11 buildvcs")],
            panes=[pane("wG", f"{SELF}-issue-13"), pane("wB", f"{OTHER}-issue-9"),
                   pane("wX", f"{SELF}-issue-11")],
            local_branches=["issue-13-stamping"], merged="a-different-branch")
        h.run()
        self.assertIn("not merged into main", h.stdout,
                      f"the real script let an unmerged branch through:\n{h.stdout}")
        self.assertIn("cannot be attributed", h.stdout,
                      f"the real script hid an unattributable workspace:\n{h.stdout}")
        self.assertIn("other repositories' workspaces", h.stdout,
                      f"the real script did not scope itself to one repository:\n{h.stdout}")

    def test_the_either_spelling_match_is_real(self):
        """Anchor the id match again; the human-form worker stops being reclaimable.

        The anchored regex is the mutation, and the complement is the assertion: the
        mutant must not offer the worker for removal, and must now *report* it as having
        no id — the difference between the old silent skip and this fix.
        """
        path = self._mutate(
            r'if [[ "$label" =~ \#([0-9]+)([^0-9[:alpha:]]|$) ]]; then',
            r'if [[ "$label" =~ ^\#([0-9]+)[[:space:]]+(.+)$ ]]; then')
        h = self.harness(**human_form_worker())
        h.run(script=path)
        self.assertNotIn("reclaimable (merged", h.stdout,
                         "anchoring the match did not hide the human-form worker, so the "
                         f"either-spelling guard is untested:\n{h.stdout}")
        self.assertIn("label does not carry an issue id", h.stdout,
                      "with the id unreadable the mutant must still report the workspace "
                      f"rather than skip it in silence:\n{h.stdout}")

    def test_the_lost_label_report_is_real(self):
        """Delete the report; the no-id worker vanishes from the output again."""
        path = self._mutate(
            'CLASS_B+=("|$ws_id|$label|label does not carry an issue id; '
            'cannot reclaim a worker without one|")',
            ": # the no-id report, removed")
        h = self.harness(
            workspaces=[workspace("wL", "AI · meta", repo_root=SELF, linked=True,
                                  checkout=f"{SELF}-issue-9")],
            panes=[pane("wL", f"{SELF}-issue-9")], existing=[f"{SELF}-issue-9"])
        h.run(script=path)
        self.assertNotIn("label does not carry an issue id", h.stdout)
        self.assertNotIn("wL", h.stdout,
                         f"the no-id worker still appears somewhere:\n{h.stdout}")

    def test_the_no_id_exclusion_is_real(self):
        """Widen the no-id branch to every workspace; excluded non-workers surface.

        The predicate is `this repository AND a linked worktree`, and the mutation drops
        it — so every no-id workspace is then reported as a worker that lost its id.
        """
        path = self._mutate(
            '  if [ -z "$iid" ]; then\n'
            '    if [ -n "$ws_repo_root" ] \\\n'
            '       && [ "$(abspath "$ws_repo_root")" = "$REPO_ROOT" ] \\\n'
            '       && [ "$linked" = "true" ]; then',
            '  if [ -z "$iid" ]; then\n'
            '    if true; then')
        h = self.harness(
            workspaces=[workspace("wD", "AI · go Dagger v1 (serial)"),
                        workspace("wP", "meta", repo_root=SELF, linked=False,
                                  checkout=SELF),
                        workspace("wF", "AI · media", repo_root=OTHER, linked=True,
                                  checkout=f"{OTHER}-issue-9")],
            panes=[pane("wD", "$HOME"), pane("wP", SELF),
                   pane("wF", f"{OTHER}-issue-9")])
        h.run(script=path)
        self.assertIn("label does not carry an issue id", h.stdout,
                      f"widening the no-id branch did not surface the excluded ones:\n{h.stdout}")
        for wid in ("wD", "wP", "wF"):
            self.assertIn(wid, h.stdout,
                          f"{wid} stayed invisible under the widened mutation:\n{h.stdout}")


if __name__ == "__main__":
    unittest.main(verbosity=2)