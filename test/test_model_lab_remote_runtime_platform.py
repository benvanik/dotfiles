"""Remote model-runtime platform tests."""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model-lab"))

from model_lab.errors import ModelLabError  # noqa: E402
from service_runtime.execution_environment import (  # noqa: E402
    runtime_execution_environment,
    validate_runtime_execution_environment,
)
from service_runtime.layout import RuntimeLayout  # noqa: E402
from service_runtime.platform import ProcessObservation, SystemPlatform  # noqa: E402


OWNERSHIP_ENVIRONMENT = {
    "RUNPOD_SERVICE_ID": "fixture-service",
    "RUNPOD_SERVICE_PROCESS_NONCE": "fixture-nonce",
    "RUNPOD_SERVICE_MANIFEST_SHA256": "a" * 64,
}


def signal_process_group_then_report_absent(
    _: int,
    signal_number: int,
) -> None:
    if signal_number == 0:
        raise ProcessLookupError


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.status = 200
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def read(self, _: int) -> bytes:
        return self.payload


class _Opener:
    def __init__(self, model_payload: bytes) -> None:
        self.model_payload = model_payload
        self.calls = 0

    def open(self, *_: object, **__: object) -> _Response:
        self.calls += 1
        if self.calls == 1:
            return _Response(b"")
        return _Response(self.model_payload)


class SystemPlatformTest(unittest.TestCase):
    def test_runtime_instance_identity_is_session_stable_and_container_distinct(self):
        platform = SystemPlatform()
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            first_session = root / "first-session"
            second_session = root / "second-session"
            workspace = root / "workspace"
            first_session.mkdir(mode=0o700)
            second_session.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            first = RuntimeLayout(
                session_root=first_session,
                workspace_root=workspace,
            )
            second = RuntimeLayout(
                session_root=second_session,
                workspace_root=workspace,
            )
            first_identity = platform.boot_id(layout=first)
            self.assertEqual(
                SystemPlatform().boot_id(layout=first),
                first_identity,
            )
            second_identity = platform.boot_id(layout=second)
            self.assertRegex(first_identity, r"^[0-9a-f]{64}$")
            self.assertRegex(second_identity, r"^[0-9a-f]{64}$")
            self.assertNotEqual(first_identity, second_identity)

    def test_execution_environment_captures_only_validated_runtime_inputs(self):
        environment = runtime_execution_environment(
            {
                "CUDA_VISIBLE_DEVICES": "0",
                "LD_LIBRARY_PATH": "/usr/local/nvidia/lib:/usr/local/nvidia/lib64",
                "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
                "NVIDIA_VISIBLE_DEVICES": "all",
                "HF_TOKEN": "must-not-cross",
                "PYTHONPATH": "/host/injection",
            }
        )

        self.assertEqual(
            validate_runtime_execution_environment(environment.normalized()),
            environment,
        )
        self.assertEqual(environment.values["CUDA_VISIBLE_DEVICES"], "0")
        self.assertNotIn("HF_TOKEN", environment.values)
        self.assertNotIn("PYTHONPATH", environment.values)

    def test_execution_environment_rejects_unsafe_library_path(self):
        with self.assertRaises(ModelLabError):
            runtime_execution_environment(
                {"LD_LIBRARY_PATH": "/usr/local/cuda/lib64:relative/path"}
            )

    def test_probe_disables_proxies_and_rejects_malformed_model_entries(self):
        platform = SystemPlatform()
        opener = _Opener(b'{"object":"list","data":[{"id":"fixture"},null]}')
        with mock.patch.object(
            urllib.request,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            with mock.patch.dict(
                os.environ,
                {"HTTP_PROXY": "http://proxy.invalid:3128"},
            ):
                result = platform.probe(
                    port=8000,
                    expected_service_id="fixture",
                )

        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, urllib.request.ProxyHandler)
        self.assertEqual(handler.proxies, {})
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "model inventory is malformed")

    def test_spawn_rejects_hardlinked_log_before_truncation(self):
        platform = SystemPlatform()
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            target = root / "target"
            log = root / "service.log"
            target.write_bytes(b"must remain")
            target.chmod(0o600)
            os.link(target, log)

            with self.assertRaises(ModelLabError) as caught:
                platform.spawn(
                    argv=("/does/not/matter",),
                    environment_additions=OWNERSHIP_ENVIRONMENT,
                    execution_environment=runtime_execution_environment({}),
                    log_path=log,
                    serving_lease_descriptor=0,
                )

            self.assertEqual(caught.exception.code, "service_start_failed")
            self.assertEqual(target.read_bytes(), b"must remain")

    def test_spawn_reaps_child_that_misses_session_identity(self):
        platform = SystemPlatform()
        process = mock.Mock()
        process.pid = 2468
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="vllm", timeout=10),
            0,
        ]
        with tempfile.TemporaryDirectory() as temporary:
            log = pathlib.Path(temporary) / "service.log"
            with (
                mock.patch.object(
                    subprocess,
                    "Popen",
                    return_value=process,
                ),
                mock.patch.object(
                    platform,
                    "observe_process",
                    return_value=None,
                ),
                mock.patch.object(
                    platform,
                    "list_service_processes",
                    return_value=[],
                ),
                mock.patch.object(
                    os,
                    "killpg",
                    side_effect=signal_process_group_then_report_absent,
                ) as killpg,
            ):
                with self.assertRaises(ModelLabError) as caught:
                    platform.spawn(
                        argv=("/usr/local/bin/vllm",),
                        environment_additions=OWNERSHIP_ENVIRONMENT,
                        execution_environment=runtime_execution_environment({}),
                        log_path=log,
                        serving_lease_descriptor=0,
                    )

        self.assertEqual(caught.exception.code, "service_start_failed")
        self.assertEqual(
            [
                call
                for call in killpg.call_args_list
                if call.args[1] != 0
            ],
            [
                mock.call(2468, signal.SIGTERM),
                mock.call(2468, signal.SIGKILL),
            ],
        )
        self.assertEqual(process.wait.call_count, 2)

    def test_rejected_spawn_reaps_tagged_process_that_escaped_launch_group(self):
        platform = SystemPlatform()
        process = mock.Mock()
        process.pid = 2468
        process.wait.return_value = 0
        escaped = ProcessObservation(
            pid=3579,
            process_group_id=3579,
            session_id=3579,
            start_ticks=12345,
        )
        with (
            mock.patch.object(
                os,
                "killpg",
                side_effect=signal_process_group_then_report_absent,
            ) as killpg,
            mock.patch.object(
                platform,
                "list_service_processes",
                side_effect=[[escaped], [escaped], []],
            ),
            mock.patch.object(platform, "signal_processes") as signal_processes,
            mock.patch.object(
                platform,
                "wait_for_exit",
                return_value=False,
            ),
        ):
            platform._reap_rejected_spawn(
                process,
                service_id="fixture-service",
                process_nonce="fixture-nonce",
                manifest_sha256="a" * 64,
            )

        self.assertEqual(
            [
                call
                for call in killpg.call_args_list
                if call.args[1] != 0
            ],
            [
                mock.call(2468, signal.SIGTERM),
                mock.call(2468, signal.SIGKILL),
            ],
        )
        self.assertEqual(
            signal_processes.call_args_list,
            [
                mock.call(
                    processes=[escaped],
                    signal_number=signal.SIGTERM,
                ),
                mock.call(
                    processes=[escaped],
                    signal_number=signal.SIGKILL,
                ),
            ],
        )

    def test_rejected_spawn_kills_group_after_leader_exits_without_tags(self):
        platform = SystemPlatform()
        process = mock.Mock()
        process.pid = 2468
        process.wait.return_value = 0
        observed_signals: list[int] = []

        def process_group_state(_: int, signal_number: int) -> None:
            if signal_number == 0:
                if signal.SIGKILL in observed_signals:
                    raise ProcessLookupError
                return
            observed_signals.append(signal_number)

        with (
            mock.patch.object(
                os,
                "killpg",
                side_effect=process_group_state,
            ),
            mock.patch.object(
                platform,
                "list_service_processes",
                return_value=[],
            ),
        ):
            platform._reap_rejected_spawn(
                process,
                service_id="fixture-service",
                process_nonce="fixture-nonce",
                manifest_sha256="a" * 64,
            )

        self.assertEqual(
            observed_signals,
            [signal.SIGTERM, signal.SIGKILL],
        )


if __name__ == "__main__":
    unittest.main()
