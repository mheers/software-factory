#!/usr/bin/env bash
#
# cleanup-workers.sh — reclaim the workspace footprint of merged worker tickets.
#
# For every worker workspace of THIS repository whose branch is merged and whose worktree
# is clean it removes: the git worktree checkout, the local branch, the remote branch and
# the Herdr workspace (which ends the pane and its agent process).
#
# It never deletes an opencode session. The transcript lives in opencode.db
# independently of the pane and stays exportable and recallable; only the
# workspace that hosted the finished worker goes away.
#
# Dry-run by default. Pass --yes to actually remove anything.
#
# Coordinator only. A worker never runs this — it is running inside the very
# worktree this script deletes.

set -euo pipefail

ASSUME_YES=0
REPO=""
ONLY_IID=""
NO_GITLAB=0
TRUST_REPOSITORY=0
CLOSE_GONE=0

PROG="${0##*/}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GONE_SWEEP="$SCRIPT_DIR/close-gone-workspaces.py"

# The branch prefix is configuration (factory_config: environment, then factory.json, then the
# shipped default). A shell that cannot read it must not proceed on a guess — the same exit-2
# contract as the Python side.
BRANCH_PREFIX="$(python3 "$SCRIPT_DIR/factory_config.py" branch-prefix)" || {
  echo "$PROG: cannot read the branch prefix (factory_config.py failed) — refusing" >&2
  exit 2
}
TRACKER_PROVIDER="$(python3 "$SCRIPT_DIR/factory_config.py" tracker-provider)" || {
  echo "$PROG: cannot read the tracker provider (factory_config.py failed) — refusing" >&2
  exit 2
}

usage() {
  cat <<EOF
Usage: $PROG [options]

Reclaims merged worker tickets: worktree checkout, local + remote branch,
Herdr workspace and pane. Opencode sessions are always kept.

Options:
  --yes                Perform the removals (default: dry-run, list only)
  --repo <path>        Repo to operate on (default: current directory)
  --iid <n>            Only consider this repository's worker workspace for issue <n>
                       (scoped to --repo: every repository has its own issue 2, so an unscoped
                       --iid would match another repository's ticket)
  --no-gitlab          Skip the GitLab checks (MR + issue state)
  --trust-repository   Pass --trust-repository to herdr worktree remove
  --close-gone         Also hand workspaces whose worktree is already gone to
                       $GONE_SWEEP
                       (needs --yes to act; they are always reported)
  -h, --help           This help

A worker workspace is recognised by the issue id in its label, in either spelling in
use: `#<iid> <slug>` (`herdr worktree create --label`) or `AI · <project> #<iid> <slug>`
(a renamed workspace). A workspace of this repository whose label carries no issue id is
reported and left alone — never reclaimed, never skipped in silence.

A worker workspace is only reclaimed when all of these hold:
  * its worktree repo_root is THIS repository — another repository's workers are
    never listed at all, neither reclaimable nor "left alone"
  * it is a linked worktree workspace, never the primary repo checkout
  * the worktree has no uncommitted changes
  * the branch is fully merged into main
  * no open merge request exists for the branch
  * the issue is closed on GitLab
  * no agent in the workspace is still working

A workspace whose worktree is ALREADY GONE is a separate bucket, not a refusal:
the directory no longer exists, so there is nothing to reclaim from it and the
workspace holds a pane and a dead agent in the workspace list forever.
Nothing in git is touched for those — no branch is deleted — and the close itself
is done by close-gone-workspaces.py, which owns that decision. It is refused here
only when a branch named issue-<iid>-* still exists and is not merged into main.
EOF
}

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }
warn() { printf '%s: %s\n' "$PROG" "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --repo) [ $# -ge 2 ] || die "--repo needs a path"; REPO="$2"; shift ;;
    --repo=*) REPO="${1#*=}" ;;
    --iid) [ $# -ge 2 ] || die "--iid needs a number"; ONLY_IID="$2"; shift ;;
    --iid=*) ONLY_IID="${1#*=}" ;;
    --no-tracker|--no-gitlab) NO_GITLAB=1 ;;
    --trust-repository) TRUST_REPOSITORY=1 ;;
    --close-gone) CLOSE_GONE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

# ---------------------------------------------------------------- preconditions

command -v herdr >/dev/null 2>&1 || die "herdr not found"
command -v jq    >/dev/null 2>&1 || die "jq not found"
command -v git   >/dev/null 2>&1 || die "git not found"
if [ "$NO_GITLAB" -eq 0 ]; then
  # The provider decides what "the tracker CLI" is (GitHub: `gh`, GitLab: `glab`); `tracker.py`
  # reads it from configuration. `--no-tracker` skips both.
  if [ "$TRACKER_PROVIDER" = "gitlab" ]; then
    command -v glab >/dev/null 2>&1 || die "glab not found (use --no-tracker to skip the tracker checks)"
  else
    command -v gh >/dev/null 2>&1 || die "gh not found (use --no-tracker to skip the tracker checks)"
  fi
  # Source the workspace glab profile ONCE, here, in the sweep's own shell (GitLab flavor).
  #
  # It has to be here and not inside a helper: `issue_state` and `open_mr_count` are called
  # through command substitution, which runs in a subshell, so an export made inside them dies
  # with the subshell. A coordinator running the sweep from a fresh shell — the normal case
  # after a session boundary — then has no GLAB_CONFIG_DIR, every lookup fails, and the sweep
  # reports "tracker unreachable (the CLI is missing or unauthenticated)" for a workspace it
  # would otherwise reclaim. The refusal is safe; the reason given sends you looking for a
  # credential problem that does not exist.
  if [ "$TRACKER_PROVIDER" = "gitlab" ] && [ -z "${GLAB_CONFIG_DIR:-}" ] && [ -z "${GITLAB_TOKEN:-}" ]; then
    for _glab_env in \
      "$(dirname "${BASH_SOURCE[0]}")/glab-env.sh" \
      "${REPO_ROOT:-/nonexistent}/scripts/glab-env.sh"; do
      if [ -f "$_glab_env" ]; then
        # shellcheck disable=SC1090
        . "$_glab_env" >/dev/null 2>&1 && break
      fi
    done
    unset _glab_env
    if [ -z "${GLAB_CONFIG_DIR:-}" ]; then
      printf '%s\n' "note: no glab profile found — GitLab checks may report unreachable." \
                    "Source scripts/glab-env.sh, or pass --no-tracker to skip them deliberately."
    fi
  fi
fi

if [ -n "$REPO" ]; then
  [ -d "$REPO" ] || die "no such directory: $REPO"
  cd "$REPO"
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || die "not inside a git work tree (run from a repo root, or pass --repo)"
cd "$REPO_ROOT"
REPO_NAME="$(basename "$REPO_ROOT")"

# Herdr reports repo_root as an absolute path and git reports the toplevel as it
# resolved it. If one side went through a symlink the string comparison that
# attributes a workspace to a repository would silently match nothing, and a filter
# that matches nothing is indistinguishable from a repository with no workers.
abspath() {
  if command -v realpath >/dev/null 2>&1; then
    realpath -m -- "$1" 2>/dev/null || printf '%s' "$1"
  else
    printf '%s' "$1"
  fi
}
REPO_ROOT="$(abspath "$REPO_ROOT")"

git rev-parse --verify --quiet main >/dev/null \
  || die "no 'main' branch in $REPO_ROOT"

HERDR_ENV="${HERDR_ENV:-1}" export HERDR_ENV

HERDR_WS_JSON="$(herdr workspace list 2>/dev/null)" \
  || die "herdr workspace list failed (is a Herdr server running?)"
HERDR_PANES_JSON="$(herdr pane list 2>/dev/null)" || HERDR_PANES_JSON='{"result":{"panes":[]}}'

# ------------------------------------------------------- worktree -> branch map
#
# The branch is taken from git's own registration, never guessed from the label.
declare -A WT_BRANCH=()
while IFS= read -r line; do
  case "$line" in
    "worktree "*) current_path="${line#worktree }" ;;
    "branch "*)  WT_BRANCH["$current_path"]="${line#branch refs/heads/}" ;;
  esac
done < <(git worktree list --porcelain)

# ------------------------------------------------------------------- gitlab bit

issue_state() {
  # echoes the state, or "unknown" when the tracker is unreachable. Which CLI answers is
  # `tracker.py`'s decision, read from configuration — not this shell's.
  local iid="$1" out
  if out="$(python3 "$SCRIPT_DIR/tracker.py" state --iid "$iid" 2>/dev/null)" && [ -n "$out" ]; then
    printf '%s' "$out"; return 0
  fi
  printf 'unknown'
}

open_mr_count() {
  local branch="$1" out
  # The workspace glab profile is exported by glab-env.sh in the *caller's* shell. A sweep run
  # from a fresh shell — which is how a coordinator runs it after a session boundary — has no
  # GLAB_CONFIG_DIR, so every MR lookup returns "unknown" and the sweep refuses to reclaim
  # anything. Source the profile here rather than failing.
  if [ "$TRACKER_PROVIDER" = "gitlab" ] && [ -z "${GLAB_CONFIG_DIR:-}" ] && [ -z "${GITLAB_TOKEN:-}" ]; then
    local env_sh
    for env_sh in \
      "${REPO_ROOT:-}/scripts/glab-env.sh" \
      "$(dirname "$(dirname "${BASH_SOURCE[0]}")")/scripts/glab-env.sh"; do
      if [ -f "$env_sh" ]; then
        # shellcheck disable=SC1090
        . "$env_sh" >/dev/null 2>&1 && break
      fi
    done
  fi
  # `--state opened` is NOT a flag on `glab mr list`; opened is the default. Passing it makes
  # glab exit non-zero with `Unknown flag: --state`, which this function reported as
  # "GitLab unreachable" — a permissions story for a malformed command line. The refusal to
  # reclaim was correct, the reason given was wrong, and a coordinator reading it would go
  # looking for a credential problem that does not exist.
  if out="$(python3 "$SCRIPT_DIR/tracker.py" mr-count --branch "$branch" 2>/dev/null)" && [ -n "$out" ]; then
    printf '%s' "$out"; return 0
  fi
  printf 'unknown'
}

# Branches that could belong to this worker, from git's own ref namespace.
#
# A gone worktree has no registration to read a branch from, so the only evidence left
# is the naming convention (`issue-<iid>-…`). Guessing one exact name and trusting it
# would be the wrong way round: an unmerged branch whose slug differs would be reported
# as merged, which is how work gets destroyed. So every candidate is listed, and the
# caller refuses on ANY unmerged one.
worker_branches() {
  local iid="$1"
  git for-each-ref --format='%(refname:short)' "refs/heads/$BRANCH_PREFIX$iid-*" 2>/dev/null || true
}

# The first candidate branch that is NOT merged into main, or empty.
#
# Refuses rather than passes by default: an unreadable ref namespace, or a branch that
# exists but whose merge-base cannot be computed, must not read as "merged".
unmerged_branch() {
  local iid="$1" b
  while IFS= read -r b; do
    [ -n "$b" ] || continue
    if ! git merge-base --is-ancestor "$b" main 2>/dev/null; then
      printf '%s' "$b"
      return 0
    fi
  done < <(worker_branches "$iid")
  printf ''
}

# Every candidate branch, joined, for the report. Informational only.
worker_branch_list() {
  local iid="$1" b out=""
  while IFS= read -r b; do
    [ -n "$b" ] || continue
    out="${out:+$out, }$b"
  done < <(worker_branches "$iid")
  printf '%s' "${out:-none found}"
}

# ------------------------------------------------------------------- candidates
#
# CLASS_A  reclaimable: worktree + branches + workspace, all guards passed
# CLASS_B  left alone, with the reason. Nothing is ever removed on a guess.
# CLASS_C  stale worktree registrations (path gone, no workspace involved)
# CLASS_D  the worktree is already gone and the workspace is still open.
#          Reported separately from CLASS_B because "we cannot reclaim this" and
#          "this is reclaimable, only not by removing a worktree" are different facts,
#          and merging them is how the leak survived being reported.
# FOREIGN  workspaces of other repositories. Counted, never listed: they are not
#          this sweep's business, and printing them makes a sweep from one repo name
#          another repo's running workers as candidates for removal.

CLASS_A=(); CLASS_B=(); CLASS_C=(); CLASS_D=()
FOREIGN=(); UNATTRIBUTED=()
declare -A FOREIGN_REPO_COUNT=()

while IFS= read -r ws; do
  [ -n "$ws" ] || continue

  ws_id="$(printf '%s' "$ws" | jq -r '.workspace_id')"
  label="$(printf '%s' "$ws" | jq -r '.label // ""')"

  # The issue id, read from either label spelling in use.
  #
  # `herdr worktree create --label` writes `#<iid> <slug>`; a renamed workspace carries the
  # human form `AI · <project> #<iid> <slug>`. Anchoring the match at `^` recognised only
  # the first and skipped the second *silently*: a merged, issue-closed worker renamed to
  # the human form could never be reclaimed and no line said so. The trailing
  # boundary group keeps an issue number from matching inside a longer token — a label like
  # `brand #2c3e50` says nothing about an issue.
  iid=""
  if [[ "$label" =~ \#([0-9]+)([^0-9[:alpha:]]|$) ]]; then
    iid="${BASH_REMATCH[1]}"
  fi

  # --iid narrows by issue. A workspace whose label carries no id cannot match one — and is
  # deliberately not filtered out here: "this worker cannot name its issue" is exactly the
  # fact a targeted sweep must not swallow, and the row it produces is never reclaimable.
  if [ -n "$ONLY_IID" ] && [ -n "$iid" ] && [ "$iid" != "$ONLY_IID" ]; then
    continue
  fi

  ws_repo_root="$(printf '%s' "$ws" | jq -r 'if .worktree == null then "" else (.worktree.repo_root // "") end')"
  linked="$(printf '%s' "$ws" | jq -r '.worktree.is_linked_worktree // false')"

  # This repository's own linked worktree whose label carries no issue id: a worker that
  # has lost its number. Every guard below is keyed on the id — the branch namespace, the
  # MR and the issue check — so nothing can be verified and nothing is reclaimed. It is
  # *said*, not skipped: a silent `continue` here is how a merged worker becomes a
  # permanent ghost, the same rule the no-worktree-object case below states.
  #
  # Every OTHER workspace without an id is excluded silently and on purpose: primary
  # checkouts (`meta`, …), `AI · go Dagger v1 (serial)`, and other
  # repositories' workspaces are not workers, and naming them on every sweep is noise.
  if [ -z "$iid" ]; then
    if [ -n "$ws_repo_root" ] \
       && [ "$(abspath "$ws_repo_root")" = "$REPO_ROOT" ] \
       && [ "$linked" = "true" ]; then
      CLASS_B+=("|$ws_id|$label|label does not carry an issue id; cannot reclaim a worker without one|")
    fi
    continue
  fi

  # Guard 0: must belong to THIS repository.
  #
  # The candidate loop used to filter on the label alone, so `--repo` only decided which
  # checkout to read and never which workspaces were in scope. A dry run from
  # media named identity's and press's *running* workers.
  if [ -z "$ws_repo_root" ]; then
    # No repo_root at all. Report it, never filter it out silently: a workspace the
    # sweep cannot attribute is exactly the gone-worktree case, and hiding it here would
    # trade a false report for an invisible one.
    UNATTRIBUTED+=("$iid|$ws_id|$label")
    continue
  fi
  if [ "$(abspath "$ws_repo_root")" != "$REPO_ROOT" ]; then
    foreign_root="$(abspath "$ws_repo_root")"
    FOREIGN+=("$iid|$ws_id|$label|$foreign_root")
    FOREIGN_REPO_COUNT["$foreign_root"]=$((${FOREIGN_REPO_COUNT[$foreign_root]:-0} + 1))
    continue
  fi

  pane_id="$(printf '%s' "$HERDR_PANES_JSON" \
    | jq -r --arg w "$ws_id" '[.result.panes[]? | select(.workspace_id==$w)][0].pane_id // "-"')"
  session_id="$(printf '%s' "$HERDR_PANES_JSON" \
    | jq -r --arg w "$ws_id" '[.result.panes[]? | select(.workspace_id==$w)][0].agent_session.value // "-"')"

  # Guard 1: must be a linked worktree workspace, never the primary checkout.
  # `linked` was read above with the id check: it decides both whether a no-id workspace
  # is a lost-label worker and whether an id-carrying one is a primary checkout.
  if [ "$linked" != "true" ]; then
    CLASS_B+=("$iid|$ws_id|$label|primary checkout, never reclaimed|")
    continue
  fi

  checkout="$(printf '%s' "$ws" | jq -r '.worktree.checkout_path // ""')"

  # The worktree is already gone. This is the gone-worktree case, and it is NOT the primary checkout.
  #
  # Measured on 2026-10-03 the sweep reported these as `no branch registration for …`,
  # which is a misleading reason rather than a refusal: the checkout path is absent from
  # disk, the workspace holds a pane and a dead agent in the list forever, and no amount
  # of re-running reaches them. They are classified here and handed to
  # close-gone-workspaces.py, which owns the close and every refusal that protects it.
  if [ -n "$checkout" ] && [ ! -d "$checkout" ]; then
    gone_branches="$(worker_branch_list "$iid")"
    unmerged="$(unmerged_branch "$iid")"
    if [ -n "$unmerged" ]; then
      CLASS_B+=("$iid|$ws_id|$label|branch $unmerged not merged into main; worktree gone, workspace left open|")
      continue
    fi
    if [ "$NO_GITLAB" -eq 0 ]; then
      # Only a branch git can name is worth an MR lookup. The merge check above is the
      # one that protects the work; this one reports whether a merge is still in flight.
      mr_block=""
      while IFS= read -r b; do
        [ -n "$b" ] || continue
        mrs="$(open_mr_count "$b")"
        if [ "$mrs" = "unknown" ]; then
          mr_block="tracker unreachable (the CLI is missing or unauthenticated)"
          break
        fi
        if [ "$mrs" != "0" ]; then
          mr_block="$mrs open MR(s) for $b"
          break
        fi
      done < <(worker_branches "$iid")
      if [ -n "$mr_block" ]; then
        CLASS_B+=("$iid|$ws_id|$label|$mr_block; workspace left open|")
        continue
      fi
      state="$(issue_state "$iid")"
      if [ "$state" = "unknown" ]; then
        CLASS_B+=("$iid|$ws_id|$label|could not read issue #$iid; workspace left open|")
        continue
      fi
      if [ "$state" != "closed" ]; then
        CLASS_B+=("$iid|$ws_id|$label|issue #$iid is $state; workspace left open|")
        continue
      fi
    fi
    CLASS_D+=("$iid|$ws_id|$label|$checkout|$gone_branches|$pane_id|$session_id|")
    continue
  fi

  branch="${WT_BRANCH[$checkout]:-}"

  if [ -z "$branch" ]; then
    CLASS_B+=("$iid|$ws_id|$label|no branch registration for $checkout|")
    continue
  fi

  # Guard 2: no uncommitted work.
  if [ -n "$(git -C "$checkout" status --porcelain 2>/dev/null)" ]; then
    CLASS_B+=("$iid|$ws_id|$label|uncommitted changes in $checkout|")
    continue
  fi

  # Guard 3: branch fully merged into main.
  if ! git merge-base --is-ancestor "$branch" main 2>/dev/null; then
    CLASS_B+=("$iid|$ws_id|$label|branch $branch not merged into main|")
    continue
  fi

  # Guard 4: no open merge request.
  if [ "$NO_GITLAB" -eq 0 ]; then
    mrs="$(open_mr_count "$branch")"
    if [ "$mrs" = "unknown" ]; then
      CLASS_B+=("$iid|$ws_id|$label|tracker unreachable (the CLI is missing or unauthenticated)|")
      continue
    fi
    if [ "$mrs" != "0" ]; then
      CLASS_B+=("$iid|$ws_id|$label|$mrs open MR(s) for $branch|")
      continue
    fi

    # Guard 5: issue closed.
    state="$(issue_state "$iid")"
    if [ "$state" = "unknown" ]; then
      CLASS_B+=("$iid|$ws_id|$label|could not read issue #$iid|")
      continue
    fi
    if [ "$state" != "closed" ]; then
      CLASS_B+=("$iid|$ws_id|$label|issue #$iid is $state|")
      continue
    fi
  fi

  # Guard 6: nobody still working in that workspace.
  busy="$(printf '%s' "$HERDR_PANES_JSON" \
    | jq -r --arg w "$ws_id" '[.result.panes[]? | select(.workspace_id==$w and .agent_status=="working")] | length')"
  if [ "${busy:-0}" != "0" ]; then
    CLASS_B+=("$iid|$ws_id|$label|agent still working in workspace|")
    continue
  fi

  CLASS_A+=("$iid|$ws_id|$label|$checkout|$branch|$pane_id|$session_id|")
done < <(printf '%s' "$HERDR_WS_JSON" | jq -c '.result.workspaces[]?')

# ----------------------------------------------- class C: stale registrations

if [ "${#WT_BRANCH[@]}" -gt 0 ]; then
  for path in "${!WT_BRANCH[@]}"; do
    if [ "$path" != "$REPO_ROOT" ] && [ ! -d "$path" ]; then
      CLASS_C+=("$path")
    fi
  done
fi

# ---------------------------------------------------------------------- report

if [ "$ASSUME_YES" -eq 1 ]; then
  echo "cleanup-workers.sh — APPLYING (repo: $REPO_NAME)"
else
  echo "cleanup-workers.sh — DRY RUN (repo: $REPO_NAME; re-run with --yes to apply)"
fi
echo

if [ "${#CLASS_A[@]}" -gt 0 ]; then
  echo "reclaimable (merged, clean, issue closed):"
  for rec in "${CLASS_A[@]}"; do
    IFS='|' read -r iid ws_id label checkout branch pane_id session_id _ <<<"$rec"
    printf '  #%-4s %-28s %s\n' "$iid" "$branch" "$label"
    printf '         workspace %s  pane %s\n' "$ws_id" "$pane_id"
    printf '         worktree  %s\n' "$checkout"
    printf '         session   %s (kept, not deleted)\n' "$session_id"
  done
  echo
else
  echo "reclaimable: none"
  echo
fi

if [ "${#CLASS_B[@]}" -gt 0 ]; then
  echo "left alone (need a decision; ${#CLASS_B[@]}):"
  for rec in "${CLASS_B[@]}"; do
    IFS='|' read -r iid ws_id label reason _ <<<"$rec"
    # The workspace id is printed because "left alone" without it cannot be acted on:
    # the reader has nothing to run `herdr workspace close` against.
    if [ -z "$iid" ]; then
      # A worker whose label lost its issue id: there is no number for the `#` column.
      printf '  %-28s workspace %s\n' "$label" "$ws_id"
    else
      printf '  #%-4s %-28s workspace %s\n' "$iid" "$label" "$ws_id"
    fi
    printf '         %s\n' "$reason"
  done
  echo
fi

if [ "${#CLASS_C[@]}" -gt 0 ]; then
  echo "stale worktree registrations (${#CLASS_C[@]}; path already gone):"
  for path in "${CLASS_C[@]}"; do
    printf '  %s\n' "$path"
  done
  echo
fi

if [ "${#CLASS_D[@]}" -gt 0 ]; then
  echo "reclaimed worktree, workspace still open (${#CLASS_D[@]}; gone worktree):"
  for rec in "${CLASS_D[@]}"; do
    IFS='|' read -r iid ws_id label checkout branches pane_id session_id _ <<<"$rec"
    printf '  #%-4s workspace %s  pane %s\n' "$iid" "$ws_id" "$pane_id"
    printf '         branch(es) git can still name: %s\n' "$branches"
    printf '         worktree  %s (gone from disk)\n' "$checkout"
    printf '         session   %s (kept, not deleted)\n' "$session_id"
    printf '         no branch is deleted for these; only the workspace goes\n'
  done
  echo
fi

if [ "${#FOREIGN[@]}" -gt 0 ]; then
  # Listed as a count and one example per repository, never as candidates. Naming
  # another repository's running workers in a sweep is what the repo filter is about.
  echo "other repositories' workspaces, not this sweep's business (${#FOREIGN[@]}):"
  # One line per repository rather than one per workspace: the count is the fact, the
  # example says which repo, and a table of other repos' workers is what the repo filter removed.
  declare -A FOREIGN_SEEN=()
  shown=0
  for rec in "${FOREIGN[@]}"; do
    IFS='|' read -r iid ws_id label ws_repo <<<"$rec"
    [ -n "${FOREIGN_SEEN[$ws_repo]:-}" ] && continue
    FOREIGN_SEEN["$ws_repo"]=1
    printf '  %-24s %s workspace(s), e.g. #%s %s\n' \
      "$(basename "$ws_repo")" "${FOREIGN_REPO_COUNT[$ws_repo]:-?}" "$iid" "$label"
    shown=$(( shown + 1 ))
    [ "$shown" -ge 10 ] && break
  done
  echo "  reported as a count so a sweep from one repo never names another repo's workers"
  echo
fi

if [ "${#UNATTRIBUTED[@]}" -gt 0 ]; then
  echo "no worktree object, cannot be attributed to a repository (${#UNATTRIBUTED[@]}):"
  for rec in "${UNATTRIBUTED[@]}"; do
    IFS='|' read -r iid ws_id label _ <<<"$rec"
    printf '  #%-4s %-28s workspace %s\n' "$iid" "$label" "$ws_id"
  done
  printf '  close-gone-workspaces.py decides what happens to these; it needs no repo\n'
  echo
fi

# ----------------------------------------------------------------------- apply

if [ "$ASSUME_YES" -eq 0 ]; then
  # "I cleaned nothing" and "I found nothing" are different facts, and collapsing them is
  # how a report claims it removed work it never considered.
  pending=$(( ${#CLASS_A[@]} + ${#CLASS_C[@]} + ${#CLASS_D[@]} ))
  if [ "$pending" -eq 0 ]; then
    if [ "${#CLASS_B[@]}" -gt 0 ]; then
      printf 'nothing to reclaim; %d workspace(s) need a decision (each says why above).\n' \
        "${#CLASS_B[@]}"
    else
      echo "nothing to reclaim and nothing to decide: this repository has no worker workspaces left."
    fi
  else
    printf 'nothing removed (dry run); %d item(s) would be acted on. Re-run with --yes.\n' "$pending"
  fi
  exit 0
fi

if [ "${#CLASS_C[@]}" -gt 0 ]; then
  echo "pruning stale worktree registrations..."
  git worktree prune
  echo
fi

# ------------------------------------------------------------------ class D
#
# The worktree is already gone, so there is no checkout to remove and no branch to
# delete: what remains is a workspace holding a pane and a dead agent. The close itself
# belongs to close-gone-workspaces.py, which owns that decision and every refusal that
# protects it (working agent, live pane runtime, the coordinator's own workspace, a pane
# in the live fleet manifest). Delegating rather than re-implementing is deliberate: two
# pruning implementations that disagree is the failure this family keeps hitting, and a
# second copy here would be free to disagree with the one that has 28 tests behind it.
#
# Delegation is opt-in. The default is to report, because this sweep deletes branches and
# the other one closes workspaces; both acting unattended in one command is a bigger
# claim than either makes on its own.
if [ "${#CLASS_D[@]}" -gt 0 ]; then
  gone_ids=()
  for rec in "${CLASS_D[@]}"; do
    IFS='|' read -r iid ws_id label checkout branches pane_id session_id _ <<<"$rec"
    gone_ids+=("--only" "$ws_id")
  done
  if [ "$CLOSE_GONE" -eq 1 ] && [ -f "$GONE_SWEEP" ]; then
    echo "closing ${#CLASS_D[@]} workspace(s) whose worktree is gone via close-gone-workspaces.py…"
    python3 "$GONE_SWEEP" --yes "${gone_ids[@]}"
    echo
  else
    echo "not closing them: re-run with --close-gone --yes (or close them fleet-wide with"
    echo "  python3 $(basename "$GONE_SWEEP") --audit)."
    echo
  fi
fi

for rec in "${CLASS_A[@]}"; do
  IFS='|' read -r iid ws_id label checkout branch pane_id session_id _ <<<"$rec"
  echo "reclaiming #$iid ($branch)..."

  remove_args=(worktree remove --workspace "$ws_id" --force)
  if [ "$TRUST_REPOSITORY" -eq 1 ]; then
    remove_args+=(--trust-repository)
  fi
  if ! herdr "${remove_args[@]}" >/dev/null 2>&1; then
    warn "#$iid: herdr worktree remove failed, falling back to git"
    git worktree remove --force "$checkout" 2>/dev/null || warn "#$iid: git worktree remove failed too"
  fi
  git worktree prune

  # git branch -d refuses to delete an unmerged branch — that is the safety net.
  if git branch -d "$branch" 2>/dev/null; then
    echo "  local branch $branch deleted"
  else
    warn "#$iid: local branch $branch kept (not merged / still checked out)"
  fi

  if git push origin --delete "$branch" >/dev/null 2>&1; then
    echo "  remote branch $branch deleted"
  else
    warn "#$iid: remote branch $branch not deleted (no remote, or already gone)"
  fi

  if herdr workspace close "$ws_id" >/dev/null 2>&1; then
    echo "  workspace $ws_id closed (pane $pane_id)"
  else
    warn "#$iid: workspace $ws_id not closed (try: herdr workspace close $ws_id)"
  fi

  echo "  session $session_id kept"
  echo
done

echo "done."