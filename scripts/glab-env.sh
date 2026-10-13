#!/usr/bin/env bash
# Load the workspace-local glab profile for the workspace.
#
# The GitLab group is configured per workspace (`example.com` in the shipped defaults). The token
# is a personal access token scoped to that group; the canonical copy lives in the workspace
# root's .gitlab-cli/
# directory (git-ignored, 0600), and this script finds it by walking up from its own
# location. That keeps it working wherever this script is checked out.
#
# Note on scope: the token is bound to the group, so group-level reads answer
# 403 insufficient_granular_scope for anything above it (for example the parent group) and
# for member listings. Project and issue calls work, including creating projects, but
# project creation must go through POST /projects with an explicit namespace_id —
# POST /groups/:id/projects answers 404 for a subgroup-bound token.
#
# Usage:  source scripts/glab-env.sh
#
# `set -eu -o pipefail` is saved and RESTORED, because this file is meant to be sourced and a
# sourced `set` is not local to the sourced file — it changes the caller's shell. Measured:
# sourcing this from a plain `bash` turned `nounset` on in the caller, and the next reference
# to any unset variable in that script died. `errexit` leaked the same way; it appeared not to
# only because the caller happened to invoke the source inside a `||` list, where bash ignores
# errexit for that command — so the leak was there and merely masked.
#
# Strictness is still wanted *inside* this file: an unset variable here would produce an empty
# credential rather than an error, which is the bug this file exists to prevent.
_glab_env_saved_opts="$(set +o)"
set -eu -o pipefail

# zsh does not set BASH_SOURCE; fall back to ${(%):-%x} so this works when sourced in either.
if [ -n "${BASH_SOURCE:-}" ]; then
  _glab_env_src="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
  _glab_env_src="${(%):-%x}"
else
  _glab_env_src="$0"
fi

_glab_env_dir="$(cd "$(dirname "$_glab_env_src")" && pwd)"
_glab_env_profile=""
while [ "$_glab_env_dir" != "/" ]; do
  # A directory named `.gitlab-cli` is NOT a profile. `glab` creates one with its own bare
  # `config.yml` the first time it runs with `GLAB_CONFIG_DIR` unset, and a checkout that
  # nobody sourced this script from is exactly how that happens — so the walk keeps going
  # past a directory with no `token` in it and takes the nearest one that actually carries
  # a credential.
  #
  # It used to break on the first directory it saw. Measured 2026-10-05: `meta`
  # had its own empty `.gitlab-cli/` (created 23:41 by a `glab` call from that directory),
  # the real profile was at the workspace root, and this script exported
  # `GITLAB_TOKEN=""` and returned 0. Every caller then got `403
  # insufficient_granular_scope` and reported it as "GitLab was not read at all" — which is
  # the same failure as the PATH bug below, one layer over: **a script that exports a
  # credential it does not have, because it found the right shape instead of the right
  # file.** A silent empty export is worse than a refusal, because it looks like success.
  if [ -s "$_glab_env_dir/.gitlab-cli/token" ]; then
    _glab_env_profile="$_glab_env_dir/.gitlab-cli"
    break
  fi
  _glab_env_dir="$(dirname "$_glab_env_dir")"
done

if [ -z "$_glab_env_profile" ]; then
  echo "glab-env: no .gitlab-cli/token found above $_glab_env_src" >&2
  echo "glab-env: a .gitlab-cli directory without a token is not a profile — glab creates one" >&2
  echo "glab-env: those by itself. Do not create one here; unset GLAB_CONFIG_DIR if that is what" >&2
  echo "glab-env: you meant to do. Refusing rather than exporting an empty credential." >&2
  unset _glab_env_src _glab_env_dir
  eval "$_glab_env_saved_opts"          # a refusal must not leave the caller's shell changed
  unset _glab_env_saved_opts
  return 1 2>/dev/null || exit 1
fi

export GLAB_CONFIG_DIR="$_glab_env_profile"
export GITLAB_TOKEN="$(cat "${GLAB_CONFIG_DIR}/token")"

# `-s` above proved the file exists and is not empty; prove the *value* is usable, because a
# file containing a newline is non-empty and exports an empty string. A caller cannot tell
# "no token" from "a token of zero length" at the far end — both arrive as a 403 — so the
# check belongs here, where the answer can still be an error.
if [ -z "$GITLAB_TOKEN" ]; then
  echo "glab-env: ${GLAB_CONFIG_DIR}/token is empty" >&2
  unset _glab_env_src _glab_env_dir GITLAB_TOKEN GLAB_CONFIG_DIR
  eval "$_glab_env_saved_opts"
  unset _glab_env_saved_opts
  return 1 2>/dev/null || exit 1
fi

# The exports above are already done, and they are **independent of whether the binary is on
# PATH**: the token and the config directory are facts about a file on disk, and `glab` being
# installed somewhere is a fact about PATH.
#
# Returning 1 here used to look harmless — the caller usually has glab — and it was not. Under
# cron, PATH is `/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin` and `glab` is installed by
# `go install` into `~/go/bin`, so `command -v glab` fails, this returns 1 under `set -e`, and the
# exports are discarded with the shell. Every caller then ran with no token and got `403`, which
# they reported as `UNKNOWN — GitLab was not read at all`. Two layers of the same mistake: a
# script that refuses to export a credential it has, because a binary it does not need is
# missing.
#
# So: warn, export anyway, and succeed. A caller that wants the binary missing to be fatal can
# check `command -v glab` itself — it is one command, and it is the one that knows what it needs.
if ! command -v glab >/dev/null 2>&1; then
  echo "glab-env: glab is not on PATH; the token and config dir are still exported" >&2
  for _glab_env_guess in "$HOME/go/bin" "$HOME/.local/bin"; do
    if [ -x "$_glab_env_guess/glab" ]; then
      echo "glab-env: found it at $_glab_env_guess/glab — add that to PATH" >&2
      break
    fi
  done
  unset _glab_env_guess
fi

echo "glab profile: ${GLAB_CONFIG_DIR} (gitlab.com)"
unset _glab_env_src _glab_env_dir _glab_env_profile

# Hand the caller its own shell options back. Every `return`/`exit` above this line must have
# restored them too — a refusal that leaves the caller's `nounset` on is a second failure inside
# the handler for the first one, which is the worst place to have one.
eval "$_glab_env_saved_opts"
unset _glab_env_saved_opts
