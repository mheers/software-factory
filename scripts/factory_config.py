#!/usr/bin/env python3
"""The workspace configuration — resolved once, in three steps.

Every factory script asks this module for the workspace's layout instead of carrying it as a
literal, so a workspace stands a workspace up by editing one file (`factory.json`) rather than
by editing scripts. Per key:

1. **the environment override** — `FACTORY_*` / `FLEET_*`; kept first because cron jobs and the
   tests set these, and a process that names a value must beat the file;
2. **`factory.json`** — the file named by `FACTORY_CONFIG`, else `<meta root>/factory.json`,
   where the meta root is the repository this module lives in (`scripts/..`);
3. **the shipped template defaults** — the layout `templates/factory.json.example` documents.
   A workspace with no `factory.json` runs on these, and they name no workspace.

A configuration the operator *named* but that cannot be read or parsed is a hard stop —
`SystemExit(2)`, printed to stderr: **"cannot tell" is never a pass**, and a script that guesses
the layout is a script that edits the wrong repository. A *missing* `factory.json` is not a hard
stop; the defaults are documented behavior, not a guess about somebody else's workspace.

No third-party imports. The only side effect is reading the one file, once.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
META_ROOT = HERE.parent  # scripts/ -> the meta repository

DEFAULT_STATE_DIR = ".fleet-state"

# State files that live in the state directory unless `paths` names them. Keys are the names
# callers use; values are the file names under the state directory.
STATE_FILES = {
    "fleet_manifest": "fleet.json",
    "watch_state": "watch.json",
    "keepalive_state": "keepalive.json",
    "keepalive_log": "keepalive.log",
    "notify_state": "notify.json",
    "main_sweep_record": "main-sweep.json",
    "expected_ci": "expected-ci.json",
    "expected_red": "expected-red.json",
}

# Paths that are not state-directory files and carry their own default.
STANDALONE_PATHS = {
    "browser_grant": "/tmp/opencode/browser-grant",
}

DEFAULTS = {
    "meta_repo": "meta",
    "registry": "apps.json",
    "branch_prefix": "issue-",
    "tracker": {"provider": "gitlab", "owner": "example.com"},
    "labels": {"todo": "status::todo", "doing": "status::doing", "blocked": "status::blocked"},
}

TRACKER_PROVIDERS = ("gitlab", "github")

_config_cache: dict | None = None


def die(message: str) -> None:
    """Refuse to guess. Exit 2 — the code the factory reserves for "cannot tell"."""
    print(f"factory_config: {message}", file=sys.stderr)
    raise SystemExit(2)


def reset() -> None:
    """Drop the cached configuration. For tests that rewrite the file or the environment."""
    global _config_cache
    _config_cache = None


def _config_path() -> Path | None:
    named = os.environ.get("FACTORY_CONFIG")
    if named:
        path = Path(named).expanduser()
        if not path.is_file():
            die(f"FACTORY_CONFIG names {path}, which is not a readable file — cannot tell the "
                f"workspace's layout")
        return path
    candidate = META_ROOT / "factory.json"
    return candidate if candidate.is_file() else None


def _config() -> dict:
    global _config_cache
    if _config_cache is None:
        path = _config_path()
        if path is None:
            _config_cache = {}
        else:
            try:
                data = json.loads(path.read_text())
            except Exception as exc:  # noqa: BLE001
                die(f"{path} is not readable JSON ({exc}) — a config that does not parse is "
                    f"exit 2")
            if not isinstance(data, dict):
                die(f"{path} does not hold a JSON object")
            _config_cache = data
    return _config_cache


def _get(*keys: str):
    node: object = _config()
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def meta_root() -> Path:
    """The meta repository this script lives in. The one path that is never configuration."""
    return META_ROOT


def _expand(value: str, *, for_workspace: bool = False) -> Path:
    workspace = "" if for_workspace else str(workspace_root())
    text = value.replace("<meta>", str(META_ROOT)).replace("<workspace>", workspace)
    path = Path(text).expanduser()
    return path if path.is_absolute() else META_ROOT / path


def meta_repo() -> str:
    """The meta repository's logical name (rows, listings) — never a path to find it by."""
    return os.environ.get("FACTORY_META_REPO") or _get("meta_repo") or DEFAULTS["meta_repo"]


def workspace_root() -> Path:
    """The workspace root: the meta repository's parent unless factory.json says otherwise."""
    named = os.environ.get("FACTORY_WORKSPACE")
    if named:
        return Path(named).expanduser()
    configured = _get("workspace")
    if configured:
        return _expand(str(configured), for_workspace=True)
    return META_ROOT.parent


def registry_path() -> Path:
    named = os.environ.get("FLEET_APPS_JSON")
    if named:
        return Path(named).expanduser()
    return _expand(str(_get("registry") or DEFAULTS["registry"]))


def state_dir() -> Path:
    configured = _get("paths", "state_dir")
    if configured:
        return _expand(str(configured))
    return META_ROOT / DEFAULT_STATE_DIR


def path(name: str) -> Path:
    """One named runtime path. `paths` in factory.json overrides the shipped default."""
    configured = _get("paths", name)
    if configured:
        return _expand(str(configured))
    if name in STATE_FILES:
        return state_dir() / STATE_FILES[name]
    if name in STANDALONE_PATHS:
        return Path(STANDALONE_PATHS[name]).expanduser()
    die(f"unknown path key {name!r} — this is a programming error in the caller, not a config one")


def branch_prefix() -> str:
    return (os.environ.get("FACTORY_BRANCH_PREFIX") or _get("branch_prefix")
            or DEFAULTS["branch_prefix"])


def tracker_provider() -> str:
    provider = (os.environ.get("FACTORY_TRACKER_PROVIDER") or _get("tracker", "provider")
                or DEFAULTS["tracker"]["provider"])
    if provider not in TRACKER_PROVIDERS:
        die(f"tracker provider {provider!r} is not one of {', '.join(TRACKER_PROVIDERS)}")
    return provider


def tracker_owner() -> str:
    """The tracker namespace: a GitHub owner, or a GitLab group path. `FLEET_GITLAB_GROUP`
    keeps working because the GitLab flavor shipped first and cron still sets it."""
    return (os.environ.get("FACTORY_TRACKER_OWNER") or os.environ.get("FLEET_GITLAB_GROUP")
            or _get("tracker", "owner") or DEFAULTS["tracker"]["owner"])


def repo_full_name(repo: str) -> str:
    return f"{tracker_owner()}/{repo}"


def label(kind: str) -> str:
    """One workflow label name (`todo`, `doing`, `blocked`)."""
    configured = _get("labels", kind)
    if configured:
        return str(configured)
    if kind in DEFAULTS["labels"]:
        return DEFAULTS["labels"][kind]
    die(f"unknown label kind {kind!r} — expected one of {', '.join(DEFAULTS['labels'])}")


def main(argv: list[str]) -> int:
    """One value per call, for the shell scripts (`cleanup-workers.sh` reads the prefix here).

    Exit 2 without a value when the configuration refuses — the same contract as everywhere else
    in the factory: a shell that cannot read the configuration must not proceed on a guess.
    """
    if not argv:
        print("usage: factory_config.py <branch-prefix|tracker-provider|tracker-owner|meta-repo|"
              "workspace-root|registry-path|state-dir|path <key>>", file=sys.stderr)
        return 2
    if argv[0] == "path":
        if len(argv) != 2:
            print("usage: factory_config.py path <key>", file=sys.stderr)
            return 2
        print(path(argv[1]))
        return 0
    printers = {
        "branch-prefix": branch_prefix,
        "tracker-provider": tracker_provider,
        "tracker-owner": tracker_owner,
        "meta-repo": meta_repo,
        "workspace-root": lambda: str(workspace_root()),
        "registry-path": lambda: str(registry_path()),
        "state-dir": lambda: str(state_dir()),
    }
    if argv[0] not in printers:
        print(f"factory_config.py: unknown command {argv[0]!r}", file=sys.stderr)
        return 2
    print(printers[argv[0]]())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
