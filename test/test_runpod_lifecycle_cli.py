from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from runpod_local.cli import build_parser
from runpod_local.errors import RunpodLocalError
from runpod_local.lifecycle_cli import (
    _resolve_idle_timeout_seconds,
    _resolve_launch_ttl_seconds,
    _run_up,
)


class LifecycleCliTest(unittest.TestCase):
    def test_implicit_launch_ttl_caps_stale_profile_defaults(self):
        self.assertEqual(_resolve_launch_ttl_seconds(None, 4 * 60 * 60), 1800)

    def test_implicit_launch_ttl_keeps_stricter_profile_default(self):
        self.assertEqual(_resolve_launch_ttl_seconds(None, 10 * 60), 600)

    def test_explicit_launch_ttl_bypasses_the_implicit_cap(self):
        self.assertEqual(_resolve_launch_ttl_seconds("4h", 1800), 14400)

    def test_explicit_empty_launch_ttl_is_invalid(self):
        with self.assertRaises(RunpodLocalError) as caught:
            _resolve_launch_ttl_seconds("", 1800)
        self.assertEqual(caught.exception.code, "invalid_duration")

    def test_explicit_empty_idle_ttl_is_invalid(self):
        with self.assertRaises(RunpodLocalError) as caught:
            _resolve_idle_timeout_seconds("")
        self.assertEqual(caught.exception.code, "invalid_duration")

    def test_launch_plan_applies_the_implicit_cap_to_a_stale_profile(self):
        args = build_parser().parse_args(
            [
                "up",
                "compiler",
                "--profile",
                "stale-four-hour-default",
                "--json",
            ]
        )
        manager = mock.Mock()
        manager.plan_launch.return_value = {
            "schema_version": "runpod.launch-plan.v1",
            "executed": False,
        }
        output = io.StringIO()
        with mock.patch(
            "runpod_local.lifecycle_cli.ProfileStore.load",
            return_value={"lease": {"default_ttl_seconds": 4 * 60 * 60}},
        ), mock.patch(
            "runpod_local.lifecycle_cli._model_placement",
            return_value=(None, None),
        ), mock.patch(
            "runpod_local.lifecycle_cli._api",
            return_value=object(),
        ), mock.patch(
            "runpod_local.lifecycle_cli.LifecycleManager",
            return_value=manager,
        ), contextlib.redirect_stdout(output):
            self.assertEqual(_run_up(args), 0)

        self.assertEqual(
            manager.plan_launch.call_args.kwargs["ttl_seconds"],
            1800,
        )


if __name__ == "__main__":
    unittest.main()
