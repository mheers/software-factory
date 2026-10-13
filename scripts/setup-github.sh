#!/usr/bin/env bash
# Set up the tracker repository: the scoped label taxonomy, the milestones and the issue forms.
#
# GitHub has no group-level labels, so this operates on ONE repository: the current one, or the
# one named with `-R owner/repo`. Idempotent — safe to re-run. It only ever creates or updates
# labels, only creates missing milestones, and never overwrites an issue form that exists; it
# never removes a label and never touches an existing issue's label set.
#
# Usage:  ./scripts/setup-github.sh [-R owner/repo]
set -euo pipefail

REPO=()
while [ $# -gt 0 ]; do
  case "$1" in
    -R|--repo) [ $# -ge 2 ] || { echo "setup-github: -R needs owner/repo" >&2; exit 1; }
               REPO=(-R "$2"); shift ;;
    -R=*|--repo=*) REPO=(-R "${1#*=}") ;;
    -h|--help) echo "usage: $0 [-R owner/repo]"; exit 0 ;;
    *) echo "setup-github: unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

command -v gh >/dev/null 2>&1 || { echo "gh not found on PATH" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated — run: gh auth login" >&2; exit 1; }

# `gh` flags come after the subcommand; this keeps `-R` out of every call site.
gh_repo() { gh "$@" ${REPO[@]+"${REPO[@]}"}; }

# `gh repo view` takes the repository as a POSITIONAL argument — it has no `-R` (the env var
# `GH_REPO` is the other way); every other subcommand used here takes `-R`.
if [ ${#REPO[@]} -gt 0 ]; then
  NAME_WITH_OWNER="$(gh repo view "${REPO[1]}" --json nameWithOwner -q .nameWithOwner)"
else
  NAME_WITH_OWNER="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
fi
echo "labels, milestones and forms for $NAME_WITH_OWNER"

say() { printf '\n=== %s ===\n' "$1"; }

# --- scoped label taxonomy: one value per scope -------------------------------
# `--force` is the idempotency guarantee: it creates a missing label and updates an existing one
# in place. The taxonomy is the contract in AGENTS.md; the colours are the GitLab flavor's, so
# the two trackers read the same way.
label() { # name colour description
  if gh_repo label create "$1" --color "${2#\#}" --description "$3" --force >/dev/null 2>&1; then
    echo "  = $1"
  else
    echo "  ! $1 (could not be created)"
  fi
}

say "scoped labels"
# type:: the nature of the work
label "type::feature"   "#0e7490" "New capability"
label "type::bug"       "#b91c1c" "Something is broken"
label "type::task"      "#475569" "Work with no new behaviour"
label "type::docs"      "#7c3aed" "Documentation only"
label "type::decision"  "#a16207" "A decision to be recorded"
label "type::risk"      "#b45309" "Something that could hurt us"
label "type::spike"     "#6b7280" "Throwaway investigation"
# area:: where it lives
label "area::api"       "#0f766e" "HTTP API / OpenAPI"
label "area::store"     "#166534" "Persistence"
label "area::web"       "#1d4ed8" "Frontend"
label "area::cli"       "#4b5563" "Command line"
label "area::worker"    "#92400e" "Background work"
label "area::docs"      "#7c3aed" "Documentation"
label "area::infra"     "#334155" "Deployment, containers, gateway"
label "area::security"  "#991b1b" "Authentication, authorization, data protection"
# status:: the workflow
label "status::todo"    "#64748b" "Ready to pick up"
label "status::doing"   "#0284c7" "In progress"
label "status::blocked" "#b91c1c" "Waiting on something"
# sev:: on bugs and risks only
label "sev::high"       "#b91c1c" "High severity"
label "sev::medium"     "#c2410c" "Medium severity"
label "sev::low"        "#65a30d" "Low severity"

# --- milestones ---------------------------------------------------------------
# List first, then create: GitHub answers a duplicate with 422 and no useful message, and the
# list also names the milestone a re-run found. `state=all` so an archived title still counts.
milestone() { # title description
  local title="$1" description="$2"
  if gh api "repos/$NAME_WITH_OWNER/milestones?state=all" --paginate --jq '.[].title' \
       | grep -Fxq -- "$title"; then
    echo "  = $title (exists)"
    return 0
  fi
  if gh api "repos/$NAME_WITH_OWNER/milestones" --method POST \
       -f "title=$title" -f "description=$description" >/dev/null 2>&1; then
    echo "  + $title"
  else
    echo "  ! $title (could not be created)"
  fi
}

say "milestones"
milestone "M1 — Foundations"  "The repositories and the shared platform. Nothing else can start until these land."
milestone "M2 — Core product" "The main user-facing capability of the workspace."
milestone "M3 — Hardening"    "The remaining applications, polish and operational work."

# --- issue forms --------------------------------------------------------------
# GitHub forms are committed files, unlike GitLab's API-side setup: the factory's forms live in
# `templates/issue_templates/github/` and a repository carries its own copy in
# `.github/ISSUE_TEMPLATE/`. This copies the missing ones into the repository it runs in and
# never overwrites one that exists — a form is a file people edit.
TPL_DIR=".github/ISSUE_TEMPLATE"
say "issue forms ($TPL_DIR)"
TPL_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/templates/issue_templates/github"
if [ ! -d "$TPL_SRC" ]; then
  echo "  ! no forms at $TPL_SRC — nothing copied"
else
  mkdir -p "$TPL_DIR"
  for form in "$TPL_SRC"/*.yml; do
    [ -e "$form" ] || continue
    base="$(basename "$form")"
    if [ -e "$TPL_DIR/$base" ]; then
      echo "  = $base (exists, left alone)"
    else
      cp "$form" "$TPL_DIR/$base"
      echo "  + $base"
    fi
  done
fi

echo
echo "Done. A member repository runs this in its own clone, so its labels, milestones and forms"
echo "match the workspace contract in AGENTS.md."
