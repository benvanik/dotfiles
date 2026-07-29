from __future__ import annotations

import contextlib
import datetime
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from runpod_local.cli import build_parser
from runpod_local.errors import RunpodLocalError
from runpod_local.lifecycle_cli import (
    _print,
    _resolve_idle_timeout_seconds,
    _resolve_launch_ttl_seconds,
    _run_down,
    _run_ttl,
    _run_ttl_watch_cycle,
    _run_up,
)
from runpod_local.state import HOST_CONTROLLER_LOCK_SCOPE
from runpod_local.state import StateStore


class RecordingState:
    def __init__(self):
        self.lock_scopes = []
        self.in_controller_lock = False

    @staticmethod
    def scan(namespace):
        if namespace != "hostclaimops":
            raise AssertionError(f"unexpected state scan: {namespace}")
        return []

    @contextlib.contextmanager
    def locked(self, scope):
        self.lock_scopes.append(scope)
        self.in_controller_lock = True
        try:
            yield
        finally:
            self.in_controller_lock = False


class LifecycleCliTest(unittest.TestCase):
    def state_with_malformed_claim(self) -> StateStore:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = StateStore(pathlib.Path(temporary.name) / "state")
        state.write(
            "hostclaims",
            "broken",
            {"schema_version": "not-a-host-claim-ledger"},
        )
        return state

    def test_public_lifecycle_json_redacts_saved_docker_arguments(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print(
                {
                    "schema_version": "runpod.launch-result.v1",
                    "instance": {
                        "docker_entrypoint": ["/bin/bash", "-c"],
                        "docker_start_cmd": [
                            "PROVIDER_SECRET=must-not-escape\n"
                        ],
                    },
                },
                as_json=True,
            )

        emitted = output.getvalue()
        self.assertNotIn("must-not-escape", emitted)
        self.assertIn('"argument_count": 1', emitted)
        self.assertIn('"sha256":', emitted)

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

    def test_execute_launch_holds_the_host_controller_lock(self):
        args = build_parser().parse_args(
            [
                "up",
                "compiler",
                "--profile",
                "generic-host",
                "--execute",
                "--json",
            ]
        )
        state = RecordingState()
        manager = mock.Mock()

        def launch(*_args, **_kwargs):
            self.assertTrue(state.in_controller_lock)
            return {
                "name": "compiler",
                "phase": "active",
                "pod_id": "pod123",
            }

        manager.launch.side_effect = launch
        output = io.StringIO()
        with mock.patch(
            "runpod_local.lifecycle_cli._state",
            return_value=state,
        ), mock.patch(
            "runpod_local.lifecycle_cli.ProfileStore.load",
            return_value={
                "lease": {"default_ttl_seconds": 1800},
                "pod": {"gpu_type_ids": ["NVIDIA RTX PRO 6000"]},
            },
        ), mock.patch(
            "runpod_local.lifecycle_cli._api",
            return_value=object(),
        ), mock.patch(
            "runpod_local.lifecycle_cli.LifecycleManager",
            return_value=manager,
        ), contextlib.redirect_stdout(output):
            self.assertEqual(_run_up(args), 0)

        self.assertEqual(state.lock_scopes, [HOST_CONTROLLER_LOCK_SCOPE])
        manager.launch.assert_called_once()

    def test_execute_down_refuses_an_active_claim_before_termination(self):
        args = build_parser().parse_args(
            ["down", "compiler", "--execute", "--json"]
        )
        state = RecordingState()
        manager = mock.Mock()
        with mock.patch(
            "runpod_local.lifecycle_cli._state",
            return_value=state,
        ), mock.patch(
            "runpod_local.lifecycle_cli._api",
            return_value=object(),
        ), mock.patch(
            "runpod_local.lifecycle_cli.LifecycleManager",
            return_value=manager,
        ), mock.patch(
            "runpod_local.lifecycle_cli._active_claim_host_names",
            return_value={"compiler"},
        ):
            with self.assertRaises(RunpodLocalError) as caught:
                _run_down(args)

        self.assertEqual(caught.exception.code, "host_has_active_claims")
        self.assertEqual(
            state.lock_scopes,
            [HOST_CONTROLLER_LOCK_SCOPE, HOST_CONTROLLER_LOCK_SCOPE],
        )
        manager.terminate.assert_not_called()

    def test_execute_down_isolates_unrelated_malformed_claim_state(self):
        state = self.state_with_malformed_claim()
        manager = mock.Mock()
        manager.terminate.return_value = {
            "schema_version": "runpod.termination-plan.v1",
            "executed": True,
        }
        healthy_args = build_parser().parse_args(
            ["down", "healthy", "--execute", "--json"]
        )
        output = io.StringIO()
        with mock.patch(
            "runpod_local.lifecycle_cli._state",
            return_value=state,
        ), mock.patch(
            "runpod_local.lifecycle_cli._api",
            return_value=object(),
        ), mock.patch(
            "runpod_local.lifecycle_cli.LifecycleManager",
            return_value=manager,
        ), contextlib.redirect_stdout(output):
            self.assertEqual(_run_down(healthy_args), 0)

        manager.terminate.assert_called_once_with(
            "healthy",
            execute=True,
            reason="operator_request",
        )

        broken_args = build_parser().parse_args(
            ["down", "broken", "--execute", "--json"]
        )
        with mock.patch(
            "runpod_local.lifecycle_cli._state",
            return_value=state,
        ), mock.patch(
            "runpod_local.lifecycle_cli._api",
            return_value=object(),
        ), mock.patch(
            "runpod_local.lifecycle_cli.LifecycleManager",
            return_value=manager,
        ):
            with self.assertRaises(RunpodLocalError) as caught:
                _run_down(broken_args)

        self.assertEqual(
            caught.exception.code,
            "host_claim_state_ambiguous",
        )
        self.assertEqual(manager.terminate.call_count, 1)

    def test_execute_ttl_enforcement_protects_claimed_hosts_under_lock(self):
        args = build_parser().parse_args(
            ["ttl", "enforce", "--execute", "--json"]
        )
        state = RecordingState()
        manager = mock.Mock()

        def enforce_ttl(*, execute, protected_instance_names):
            self.assertTrue(state.in_controller_lock)
            self.assertTrue(execute)
            self.assertEqual(protected_instance_names, {"compiler"})
            return {
                "schema_version": "runpod.ttl-enforcement.v1",
                "actions": [],
            }

        manager.enforce_ttl.side_effect = enforce_ttl
        output = io.StringIO()
        with mock.patch(
            "runpod_local.lifecycle_cli._state",
            return_value=state,
        ), mock.patch(
            "runpod_local.lifecycle_cli._api",
            return_value=object(),
        ), mock.patch(
            "runpod_local.lifecycle_cli.LifecycleManager",
            return_value=manager,
        ), mock.patch(
            "runpod_local.lifecycle_cli._active_claim_host_names",
            return_value={"compiler"},
        ), contextlib.redirect_stdout(output):
            self.assertEqual(_run_ttl(args), 0)

        self.assertEqual(
            state.lock_scopes,
            [HOST_CONTROLLER_LOCK_SCOPE, HOST_CONTROLLER_LOCK_SCOPE],
        )
        manager.enforce_ttl.assert_called_once()

    def test_ttl_enforcement_reports_claim_scan_error_and_continues(self):
        args = build_parser().parse_args(
            ["ttl", "enforce", "--execute", "--json"]
        )
        state = self.state_with_malformed_claim()
        manager = mock.Mock()
        manager.enforce_ttl.return_value = {
            "schema_version": "runpod.ttl-enforcement.v1",
            "evaluated_at": "2026-07-28T20:00:00Z",
            "executed": True,
            "actions": [
                {
                    "instance_name": "healthy",
                    "phase": "terminated",
                    "reasons": ["hard_ttl"],
                    "executed": True,
                    "blocked_by_active_claims": False,
                }
            ],
        }
        output = io.StringIO()
        with mock.patch(
            "runpod_local.lifecycle_cli._state",
            return_value=state,
        ), mock.patch(
            "runpod_local.lifecycle_cli._api",
            return_value=object(),
        ), mock.patch(
            "runpod_local.lifecycle_cli.LifecycleManager",
            return_value=manager,
        ), contextlib.redirect_stdout(output):
            self.assertEqual(_run_ttl(args), 1)

        manager.enforce_ttl.assert_called_once_with(
            execute=True,
            protected_instance_names={"broken"},
        )
        actions = {
            action["instance_name"]: action
            for action in json.loads(output.getvalue())["actions"]
        }
        self.assertTrue(actions["healthy"]["executed"])
        self.assertEqual(
            actions["broken"]["error"],
            {
                "cause_code": "invalid_host_claim_record",
                "code": "host_claim_state_ambiguous",
                "message": (
                    "claim state for instance broken is ambiguous: "
                    "host claim ledger has an unsupported schema"
                ),
            },
        )

    def test_ttl_watch_reports_claim_scan_error_and_continues(self):
        state = self.state_with_malformed_claim()
        hosts = mock.Mock()
        hosts.enforce_retirement.return_value = {
            "schema_version": "runpod.host-retirement.v1",
            "actions": [],
        }
        lifecycle = mock.Mock()
        lifecycle.enforce_ttl.return_value = {
            "schema_version": "runpod.ttl-enforcement.v1",
            "evaluated_at": "2026-07-28T20:00:00Z",
            "executed": True,
            "actions": [
                {
                    "instance_name": "healthy",
                    "phase": "terminated",
                    "reasons": ["hard_ttl"],
                    "executed": True,
                    "blocked_by_active_claims": False,
                }
            ],
        }
        now = datetime.datetime(
            2026,
            7,
            28,
            20,
            tzinfo=datetime.timezone.utc,
        )

        cycle = _run_ttl_watch_cycle(
            state=state,
            lifecycle=lifecycle,
            hosts=hosts,
            now=now,
        )

        lifecycle.enforce_ttl.assert_called_once_with(
            execute=True,
            protected_instance_names={"broken"},
        )
        host_ttl_actions = {
            item["action"]["instance_name"]: item["action"]
            for item in cycle["actions"]
            if item["controller"] == "host-ttl"
        }
        self.assertTrue(host_ttl_actions["healthy"]["executed"])
        self.assertEqual(
            host_ttl_actions["broken"]["error"]["code"],
            "host_claim_state_ambiguous",
        )


if __name__ == "__main__":
    unittest.main()
