from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
import urllib.request
import subprocess
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runpod"))

from runpod_local.errors import RunpodLocalError  # noqa: E402
from service_runtime.platform import SystemPlatform  # noqa: E402


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

            with self.assertRaises(RunpodLocalError) as caught:
                platform.spawn(
                    argv=("/does/not/matter",),
                    environment_additions={},
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
            ):
                with self.assertRaises(RunpodLocalError) as caught:
                    platform.spawn(
                        argv=("/usr/local/bin/vllm",),
                        environment_additions={},
                        log_path=log,
                        serving_lease_descriptor=0,
                    )

        self.assertEqual(caught.exception.code, "service_start_failed")
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
