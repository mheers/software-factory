#!/usr/bin/env python3
"""The configuration seam: three steps, and the two hard stops.

The value of this module is that a script can be pointed at a workspace without being edited,
so what these tests hold is the *resolution order* (environment, then `factory.json`, then the
shipped defaults) and the refusal (`SystemExit(2)`) when a configuration the operator **named**
cannot be read. The refusal is tested in a subprocess as well — in-process `SystemExit` proves
the exception, not the exit code, and the exit code is the contract cron reads.

Run: `python3 scripts/test_factory_config.py`. No network, no fixtures beyond one temp file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import factory_config as fc  # noqa: E402

ENV_KEYS = (
    "FACTORY_CONFIG",
    "FACTORY_META_REPO",
    "FACTORY_WORKSPACE",
    "FACTORY_BRANCH_PREFIX",
    "FACTORY_TRACKER_PROVIDER",
    "FACTORY_TRACKER_OWNER",
    "FLEET_GITLAB_GROUP",
    "FLEET_APPS_JSON",
)


class ConfigTestCase(unittest.TestCase):
    """Strip every override for the duration of one test, and restore it after."""

    def setUp(self) -> None:
        self.saved = {key: os.environ.pop(key, None) for key in ENV_KEYS}
        fc.reset()

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        fc.reset()

    def write_config(self, directory: Path, data: dict) -> Path:
        path = directory / "factory.json"
        path.write_text(json.dumps(data))
        os.environ["FACTORY_CONFIG"] = str(path)
        fc.reset()
        return path


class TheShippedDefaults(ConfigTestCase):
    """Assert the shipped defaults through an EMPTY named config.

    A workspace's own `factory.json` exists precisely to override these values, so reading
    them through the workspace's file makes this red in every configured workspace — measured
    on 2026-10-14, when the first workspace bootstrapped from the template ran its gate and
    found exactly that. An empty config is the state the shipped defaults describe.
    """

    def setUp(self) -> None:
        super().setUp()
        self._empty_config_dir = tempfile.TemporaryDirectory()
        empty = Path(self._empty_config_dir.name) / "factory.json"
        empty.write_text("{}")
        os.environ["FACTORY_CONFIG"] = str(empty)
        fc.reset()

    def tearDown(self) -> None:
        self._empty_config_dir.cleanup()
        super().tearDown()

    def test_the_meta_root_is_the_repository_this_module_lives_in(self):
        self.assertEqual(fc.meta_root(), HERE.parent)
        self.assertTrue((fc.meta_root() / "scripts" / "factory_config.py").is_file())

    def test_the_workspace_is_the_meta_repositorys_parent(self):
        self.assertEqual(fc.workspace_root(), fc.meta_root().parent)

    def test_the_template_defaults_are_the_documented_layout(self):
        self.assertEqual(fc.meta_repo(), "meta")
        self.assertEqual(fc.registry_path(), fc.meta_root() / "apps.json")
        self.assertEqual(fc.state_dir(), fc.meta_root() / ".fleet-state")
        self.assertEqual(fc.branch_prefix(), "issue-")
        self.assertEqual(fc.tracker_provider(), "gitlab")
        self.assertEqual(fc.tracker_owner(), "example.com")
        self.assertEqual(fc.repo_full_name("portal"), "example.com/portal")
        self.assertEqual(fc.label("todo"), "status::todo")
        self.assertEqual(fc.label("doing"), "status::doing")
        self.assertEqual(fc.label("blocked"), "status::blocked")

    def test_the_state_files_live_one_directory(self):
        for key, name in sorted(fc.STATE_FILES.items()):
            self.assertEqual(fc.path(key), fc.state_dir() / name, key)

    def test_an_unknown_label_or_path_is_a_caller_error(self):
        with self.assertRaises(SystemExit) as caught:
            fc.label("whenever")
        self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit) as caught:
            fc.path("no-such-path")
        self.assertEqual(caught.exception.code, 2)


class TheEnvironmentBeatsTheFile(ConfigTestCase):
    def test_each_override_wins(self):
        os.environ.update({
            "FACTORY_META_REPO": "env-meta",
            "FACTORY_BRANCH_PREFIX": "ticket-",
            "FACTORY_TRACKER_PROVIDER": "github",
            "FACTORY_TRACKER_OWNER": "acme",
            "FACTORY_WORKSPACE": "/synthetic/workspace",
            "FLEET_APPS_JSON": "/synthetic/registry.json",
        })
        fc.reset()
        self.assertEqual(fc.meta_repo(), "env-meta")
        self.assertEqual(fc.branch_prefix(), "ticket-")
        self.assertEqual(fc.tracker_provider(), "github")
        self.assertEqual(fc.tracker_owner(), "acme")
        self.assertEqual(fc.workspace_root(), Path("/synthetic/workspace"))
        self.assertEqual(fc.registry_path(), Path("/synthetic/registry.json"))

    def test_the_legacy_gitlab_group_variable_still_names_the_owner(self):
        os.environ["FLEET_GITLAB_GROUP"] = "legacy.example.com/group"
        fc.reset()
        self.assertEqual(fc.tracker_owner(), "legacy.example.com/group")

    def test_an_environment_named_config_wins_over_the_meta_roots_own(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.write_config(Path(td), {"meta_repo": "from-file"})
            os.environ["FACTORY_META_REPO"] = "from-env"
            fc.reset()
            self.assertEqual(fc.meta_repo(), "from-env")
            self.assertEqual(path.name, "factory.json")


class TheConfigFile(ConfigTestCase):
    def test_the_file_supplies_every_key(self):
        with tempfile.TemporaryDirectory() as td:
            self.write_config(Path(td), {
                "meta_repo": "factory-meta",
                "workspace": "/synthetic/ws",
                "registry": "<meta>/registry.json",
                "branch_prefix": "ticket-",
                "tracker": {"provider": "github", "owner": "acme/group"},
                "paths": {"state_dir": "<meta>/state",
                          "fleet_manifest": "<meta>/state/custom-fleet.json"},
                "labels": {"todo": "t::todo", "doing": "t::doing", "blocked": "t::blocked"},
            })
            self.assertEqual(fc.meta_repo(), "factory-meta")
            self.assertEqual(fc.workspace_root(), Path("/synthetic/ws"))
            self.assertEqual(fc.registry_path(), fc.meta_root() / "registry.json")
            self.assertEqual(fc.branch_prefix(), "ticket-")
            self.assertEqual(fc.tracker_provider(), "github")
            self.assertEqual(fc.tracker_owner(), "acme/group")
            self.assertEqual(fc.state_dir(), fc.meta_root() / "state")
            self.assertEqual(fc.path("fleet_manifest"), fc.meta_root() / "state" / "custom-fleet.json")
            # A state file the config does not name follows the configured state directory.
            self.assertEqual(fc.path("notify_state"), fc.meta_root() / "state" / "notify.json")
            self.assertEqual(fc.label("todo"), "t::todo")

    def test_an_unknown_provider_refuses_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as td:
            self.write_config(Path(td), {"tracker": {"provider": "mercurial"}})
            with self.assertRaises(SystemExit) as caught:
                fc.tracker_provider()
            self.assertEqual(caught.exception.code, 2)


class TheHardStops(ConfigTestCase):
    def test_a_named_config_that_is_missing_exits_2(self):
        os.environ["FACTORY_CONFIG"] = "/synthetic/does-not-exist/factory.json"
        fc.reset()
        with self.assertRaises(SystemExit) as caught:
            fc.workspace_root()
        self.assertEqual(caught.exception.code, 2)

    def test_a_malformed_config_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "factory.json"
            path.write_text("{not json")
            os.environ["FACTORY_CONFIG"] = str(path)
            fc.reset()
            with self.assertRaises(SystemExit) as caught:
                fc.workspace_root()
            self.assertEqual(caught.exception.code, 2)

    def test_the_exit_code_is_2_in_a_real_process(self):
        """In-process SystemExit proves the exception; cron reads the exit code, not the class."""
        program = ("import sys; sys.path.insert(0, %r); import factory_config as fc; "
                   "fc.workspace_root()" % str(HERE))
        env = {**os.environ, "FACTORY_CONFIG": "/synthetic/does-not-exist/factory.json"}
        run = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                             env=env)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertIn("not a readable file", run.stderr)
        self.assertIn("factory_config:", run.stderr)


class TheShellCLI(ConfigTestCase):
    """The shell side must read the same seam — `cleanup-workers.sh` gets its branch prefix here."""

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "factory_config.py"), *args],
                              capture_output=True, text=True, env=dict(os.environ))

    def test_one_value_per_call(self):
        os.environ["FACTORY_BRANCH_PREFIX"] = "ticket-"
        fc.reset()
        run = self.run_cli("branch-prefix")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ticket-")

    def test_a_path_key_resolves(self):
        run = self.run_cli("path", "notify_state")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), str(fc.path("notify_state")))

    def test_a_named_config_that_is_missing_exits_2(self):
        os.environ["FACTORY_CONFIG"] = "/synthetic/does-not-exist/factory.json"
        run = self.run_cli("branch-prefix")
        self.assertEqual(run.returncode, 2)
        self.assertIn("factory_config:", run.stderr)

    def test_no_command_is_a_usage_error(self):
        run = self.run_cli()
        self.assertEqual(run.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
