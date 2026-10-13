#!/usr/bin/env python3
"""The glab profile loader must find a credential, not merely a directory named like one.

`scripts/glab-env.sh` is sourced by every caller that talks to GitLab — the coordinator, every
worker brief, and cron. Two defects in it were found by using it rather than reading it, and
both fail in the same direction: **they look like success.**

1. It walked up and took the first directory named `.gitlab-cli`. `glab` creates one of those
   with a bare `config.yml` the first time it runs with `GLAB_CONFIG_DIR` unset, so a checkout
   where somebody called `glab` directly grows a decoy. Measured 2026-10-05: `meta`
   had exactly that, the real profile was at the workspace root, and the script exported
   `GITLAB_TOKEN=""` and returned 0. Every caller then got `403 insufficient_granular_scope` and
   reported it as *"GitLab was not read at all"* — the same failure as the PATH bug this file
   already documents, one layer over.

2. It ran `set -euo pipefail` in a **sourced** file, which changes the caller's shell rather
   than the sourced one. Measured: sourcing it from a plain `bash` turned `nounset` on in the
   caller, and the caller's next reference to any unset variable died. `errexit` leaked the
   same way — it only *looked* contained because the caller happened to source inside a `||`
   list, where bash ignores errexit for that command.

So this test asserts three things, and the third is the one a token-length check would miss:
the right profile is found, a refusal is a **refusal** rather than an empty export, and
**the caller's shell options are the same afterwards.**

    python3 scripts/test_glab_env.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOADER = REPO_ROOT / "scripts" / "glab-env.sh"
FAKE_TOKEN = "not-a-real-token-0123456789"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f"\n         {detail}" if detail else ""))


def run_sourced(probe: str, workspace: Path) -> tuple[int, str]:
    """Source the loader from <workspace>/repo/scripts and run `probe` in that shell.

    A subprocess, not an import: the defects are about what a *sourced* file does to the
    shell that sourced it, and a Python test cannot observe that at all.

    The child's environment is **scrubbed of the two variables this loader exports**, because
    the refusal case asserts that nothing was exported and a probe inherits whatever the
    caller's shell already had. Measured 2026-10-06: with the workspace profile sourced — which
    is how the documented gate block is run, its first command is `check-agents-md.sh` — this
    test reported `8 passed, 1 failed`, and the failure was `rc=0 reported RC='1' tokenlen='62'`:
    the loader refused correctly and the assertion read the *caller's* token length. A test
    whose expectation can be satisfied only by the shell it was launched from is a test about
    the harness, not about the loader.
    """
    env = {**os.environ}
    for inherited in ("GLAB_CONFIG_DIR", "GITLAB_TOKEN"):
        env.pop(inherited, None)
    proc = subprocess.run(
        ["bash", "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(workspace / "repo"),
        env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


PROBE = r"""
source scripts/glab-env.sh >/dev/null 2>&1
echo "PROFILE=$GLAB_CONFIG_DIR"
echo "TOKENLEN=${#GITLAB_TOKEN}"
echo "NOUNSET=$(set -o | grep -o 'nounset.*')"
echo "ERREXIT=$(set -o | grep -o 'errexit.*')"
echo "AFTER=${DEFINITELY_UNSET_VARIABLE:-readable}"
"""

REFUSAL_PROBE = r"""
source scripts/glab-env.sh >/dev/null 2>&1
rc=$?
echo "RC=$rc"
echo "TOKENLEN=${#GITLAB_TOKEN}"
echo "NOUNSET=$(set -o | grep -o 'nounset.*')"
echo "AFTER=${DEFINITELY_UNSET_VARIABLE:-readable}"
"""


def build(workspace: Path, *, decoy: bool, token: str | None, empty_token: bool = False) -> None:
    """A workspace with <ws>/.gitlab-cli (the real profile) and <ws>/repo/scripts/glab-env.sh."""
    (workspace / "repo" / "scripts").mkdir(parents=True)
    shutil.copy2(LOADER, workspace / "repo" / "scripts" / "glab-env.sh")

    if token is not None:
        profile = workspace / ".gitlab-cli"
        profile.mkdir()
        (profile / "config.yml").write_text("host: gitlab.com\nuser: example\n")
        (profile / "token").write_text(("" if empty_token else token) + "\n")

    if decoy:
        # Exactly what `glab` leaves behind: a directory of the right name, a config, no token.
        d = workspace / "repo" / ".gitlab-cli"
        d.mkdir()
        (d / "config.yml").write_text("host: gitlab.com\nuser: example\n")
        (d / "aliases.yml").write_text("{}\n")


def parse(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    return fields


def is_the_real_profile(path: str, workspace: Path) -> bool:
    """True when `path` is the profile this test built, not merely something ending in a name.

    Written as a path comparison rather than a suffix: an earlier version matched
    `endswith("ws/.gitlab-cli")`, and `tempfile.TemporaryDirectory()` hands out `/tmp/tmpXXXX`
    rather than a `ws` directory — so the assertion could never hold and it read as a loader
    defect. **A test whose expectation cannot be satisfied fails for a reason that has nothing
    to do with the thing under test**, which is the same trap as an inert mutation.
    """
    return Path(path).resolve() == (workspace / ".gitlab-cli").resolve()


def main() -> int:
    print("glab-env: the loader must find a credential, not a directory named like one\n")

    if not LOADER.is_file():
        print(f"  FAIL the loader is not at {LOADER}")
        return 1

    # ---------------------------------------------------------------- the real shape
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        build(ws, decoy=False, token=FAKE_TOKEN)
        rc, out = run_sourced(PROBE, ws)
        f = parse(out)
        check(
            "a profile with a token is found and exported",
            f.get("TOKENLEN") == str(len(FAKE_TOKEN))
            and is_the_real_profile(f.get("PROFILE", ""), ws),
            f"rc={rc} profile={f.get('PROFILE')!r} tokenlen={f.get('TOKENLEN')!r}",
        )

    # ------------------------------------------------- the decoy: a directory is not a profile
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        build(ws, decoy=True, token=FAKE_TOKEN)
        rc, out = run_sourced(PROBE, ws)
        f = parse(out)
        check(
            "an empty .gitlab-cli decoy below the real profile is stepped over",
            f.get("TOKENLEN") == str(len(FAKE_TOKEN))
            and is_the_real_profile(f.get("PROFILE", ""), ws),
            f"rc={rc} took={f.get('PROFILE')!r} tokenlen={f.get('TOKENLEN')!r}",
        )

    # ------------------------------------------- no profile anywhere: refuse, do not export ""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        build(ws, decoy=False, token=None)
        rc, out = run_sourced(REFUSAL_PROBE, ws)
        f = parse(out)
        check(
            "no profile at all is a refusal, not an empty credential",
            f.get("RC") == "1" and f.get("TOKENLEN") == "0",
            f"rc={rc} reported RC={f.get('RC')!r} tokenlen={f.get('TOKENLEN')!r}",
        )

    # ------------------------------------------- a token file that exists and is empty
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        build(ws, decoy=False, token=FAKE_TOKEN, empty_token=True)
        rc, out = run_sourced(REFUSAL_PROBE, ws)
        f = parse(out)
        check(
            "a token file that exists but is empty is refused (-s alone would pass it)",
            f.get("RC") == "1" and f.get("TOKENLEN") == "0",
            f"rc={rc} reported RC={f.get('RC')!r} tokenlen={f.get('TOKENLEN')!r}",
        )

    # ------------------------------------------------------ the caller's shell is unchanged
    # This is the assertion a token check cannot make, and the one that bit hardest: the loader
    # is sourced, and a `set` in a sourced file changes the caller.
    for label, decoy, token, empty in (
        ("after a successful load", False, FAKE_TOKEN, False),
        ("after a refusal", False, None, False),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            build(ws, decoy=decoy, token=token, empty_token=empty)
            probe = REFUSAL_PROBE if token is None else PROBE
            _, out = run_sourced(probe, ws)
            f = parse(out)
            check(
                f"nounset is not left on in the caller {label}",
                f.get("NOUNSET", "").startswith("nounset") and "off" in f.get("NOUNSET", ""),
                f"NOUNSET={f.get('NOUNSET')!r}",
            )
            check(
                f"an unset variable stays readable in the caller {label}",
                f.get("AFTER") == "readable",
                f"AFTER={f.get('AFTER')!r} — the caller's shell was left with nounset on",
            )

    # ------------------------------------------------------- strictness survives INSIDE
    # Restoring the caller's options must not have removed this file's own. Unset-variable
    # expansion inside the loader has to fail loudly rather than produce an empty credential.
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        build(ws, decoy=False, token=FAKE_TOKEN)
        text = (ws / "repo" / "scripts" / "glab-env.sh").read_text()
        check(
            "the loader still uses nounset internally, and restores it explicitly",
            "set -eu -o pipefail" in text and "_glab_env_saved_opts" in text,
            "strictness was dropped rather than contained",
        )

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("\nfailing checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())