#!/usr/bin/env bash
# Set up the tracker group: labels, milestones and issue templates.
# Configuration: GITLAB_GROUP and GITLAB_GROUP_ID (see docs/TRACKER.md).
# Idempotent — safe to re-run. Group management needs an Owner/Maintainer token.
#
# Usage:  source scripts/glab-env.sh && ./scripts/setup-gitlab.sh
set -euo pipefail

GROUP="${GITLAB_GROUP:?set GITLAB_GROUP to the tracker group, e.g. example.com}"
GROUP_ID="${GITLAB_GROUP_ID:?set GITLAB_GROUP_ID to the numeric group id}"

if ! command -v glab >/dev/null 2>&1; then
  echo "glab not found on PATH" >&2
  exit 1
fi
if [[ -z "${GITLAB_TOKEN:-}" ]]; then
  echo "GITLAB_TOKEN is not set — run: source scripts/glab-env.sh" >&2
  exit 1
fi

say() { printf '\n=== %s ===\n' "$1"; }

# --- scoped label taxonomy: one value per scope -------------------------------
# Refusing to create a label that already exists is the idempotency guarantee;
# GitLab returns 409 for a duplicate, so we treat that as success.
label() { # name colour description
  if glab api "groups/$GROUP_ID/labels" --method POST \
       -f "name=$1" -f "color=$2" -f "description=$3" >/dev/null 2>&1; then
    echo "  + $1"
  else
    echo "  = $1 (exists)"
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
milestone() { # title description
  if glab api "groups/$GROUP_ID/milestones" --method POST \
       -f "title=$1" -f "description=$2" >/dev/null 2>&1; then
    echo "  + $1"
  else
    echo "  = $1 (exists)"
  fi
}

say "milestones"
milestone "M1 — Foundations"  "The repositories and the shared platform. Nothing else can start until these land."
milestone "M2 — Core product" "The main user-facing capability of the workspace."
milestone "M3 — Hardening"    "The remaining applications, polish and operational work."

# --- issue templates ----------------------------------------------------------
TPL_DIR=".gitlab/issue_templates"
say "issue templates ($TPL_DIR)"
mkdir -p "$TPL_DIR"

cat > "$TPL_DIR/Task.md" <<'EOF'
## What
<one sentence>

## Why
<the problem, not the solution>

## Done means
- [ ] <observable outcome>
- [ ] test fails first, then passes
- [ ] evidence pasted below

## Notes
<constraints, links, prior decisions>
EOF

cat > "$TPL_DIR/Bug.md" <<'EOF'
## What broke
<symptom>

## How to reproduce
1. <step>
2. <step>

## Expected vs actual
<expected>

## Evidence
<test output, logs, screenshot>
EOF

cat > "$TPL_DIR/Decision.md" <<'EOF'
## The question
<the decision to be made>

## Options
- **A** — <option> — pros / cons
- **B** — <option> — pros / cons

## Decision
<chosen option, and why>

## Consequences
<what this makes easy, and what it makes hard>
EOF

echo "  templates written to $TPL_DIR"
echo
echo "Done. To add a new app: copy PROJECT_TEMPLATE.md into it as PROJECT.md, then register the"
echo "app in apps.json — that registry edit is what puts it in the app switcher."
