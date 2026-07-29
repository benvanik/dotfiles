from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from runpod_local.errors import RunpodLocalError
from runpod_local.paths import (
    config_root,
    credentials_file,
    profile_root,
    runpod_config_file,
    runpod_root,
    runtime_root,
    state_root,
    volume_root,
)


class RunpodPathsTest(unittest.TestCase):
    def test_defaults_split_authored_state_runtime_and_secrets(self):
        environment = {"XDG_RUNTIME_DIR": "/run/user/123"}
        with mock.patch.dict("os.environ", environment, clear=True), mock.patch(
            "pathlib.Path.home",
            return_value=pathlib.Path("/home/fixture"),
        ):
            self.assertEqual(runpod_root(), pathlib.Path("/mnt/dev/runpod"))
            self.assertEqual(
                runpod_config_file(),
                pathlib.Path("/mnt/dev/runpod/runpod.toml"),
            )
            self.assertEqual(
                profile_root(),
                pathlib.Path("/mnt/dev/runpod/profiles"),
            )
            self.assertEqual(
                volume_root(),
                pathlib.Path("/mnt/dev/runpod/volumes"),
            )
            self.assertEqual(
                state_root(),
                pathlib.Path("/home/fixture/.local/state/runpod"),
            )
            self.assertEqual(
                runtime_root(),
                pathlib.Path("/run/user/123/runpod"),
            )
            self.assertEqual(
                config_root(),
                pathlib.Path("/home/fixture/.config/runpod-local"),
            )
            self.assertEqual(
                credentials_file(),
                pathlib.Path(
                    "/home/fixture/.config/runpod-local/api-key"
                ),
            )

    def test_explicit_environment_roots_are_independent(self):
        environment = {
            "RUNPOD_ROOT": "/portable/runpod",
            "RUNPOD_STATE_HOME": "/state/runpod",
            "RUNPOD_HOME": "/obsolete/combined-root",
            "XDG_RUNTIME_DIR": "/runtime/user",
            "XDG_CONFIG_HOME": "/config",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            self.assertEqual(
                runpod_root(),
                pathlib.Path("/portable/runpod"),
            )
            self.assertEqual(
                runpod_config_file(),
                pathlib.Path("/portable/runpod/runpod.toml"),
            )
            self.assertEqual(
                profile_root(),
                pathlib.Path("/portable/runpod/profiles"),
            )
            self.assertEqual(
                volume_root(),
                pathlib.Path("/portable/runpod/volumes"),
            )
            self.assertEqual(
                state_root(),
                pathlib.Path("/state/runpod"),
            )
            self.assertEqual(
                runtime_root(),
                pathlib.Path("/runtime/user/runpod"),
            )
            self.assertEqual(
                credentials_file(),
                pathlib.Path("/config/runpod-local/api-key"),
            )

    def test_xdg_state_home_is_used_only_for_machine_state(self):
        environment = {
            "XDG_STATE_HOME": "/xdg/state",
            "XDG_RUNTIME_DIR": "/runtime/user",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            self.assertEqual(
                state_root(),
                pathlib.Path("/xdg/state/runpod"),
            )
            self.assertEqual(runpod_root(), pathlib.Path("/mnt/dev/runpod"))

    def test_runtime_root_requires_boot_local_xdg_directory(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RunpodLocalError) as caught:
                runtime_root()
        self.assertEqual(
            caught.exception.code,
            "runtime_directory_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
