from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import unittest

from runpod_local.claim_cli import add_claim_parser, run_claim_command
from runpod_local.claims import ClaimReleaseResult, HostClaim
from runpod_local.timeutil import utc_timestamp


NOW = datetime.datetime(
    2026,
    7,
    28,
    20,
    0,
    tzinfo=datetime.timezone.utc,
)


def host_claim(*, generation: int = 1) -> HostClaim:
    return HostClaim(
        host_name="dev96",
        operation_id="12345678-1234-4234-8234-123456789abc",
        provider_resource_id="pod-123",
        profile_name="pro6000-is1",
        profile_sha256="1" * 64,
        hard_expires_at=utc_timestamp(
            NOW + datetime.timedelta(hours=2)
        ),
        claim_id="claim-" + "2" * 32,
        generation=generation,
        mode="shared",
        remote_root="/root/runpod-session/claims/claim-" + "2" * 32,
        endpoints={"openai": 18000},
        allocation={"gpu_memory_gb": 96.0},
        renewal_deadline=utc_timestamp(
            NOW + datetime.timedelta(minutes=2)
        ),
    )


class FakeControl:
    def __init__(self) -> None:
        self.claim = host_claim()
        self.calls = []

    def list(self, host_name=None):
        self.calls.append(("list", host_name))
        return [self.claim]

    def get(self, host_name, claim_id):
        self.calls.append(("get", host_name, claim_id))
        return self.claim

    def acquire(self, request):
        self.calls.append(("acquire", request))
        return self.claim

    def renew(
        self,
        host_name,
        claim_id,
        expected_generation,
        renewal_ttl_seconds,
    ):
        self.calls.append(
            (
                "renew",
                host_name,
                claim_id,
                expected_generation,
                renewal_ttl_seconds,
            )
        )
        return host_claim(generation=expected_generation + 1)

    def release(
        self,
        host_name,
        claim_id,
        expected_generation,
        *,
        now=False,
    ):
        self.calls.append(
            (
                "release",
                host_name,
                claim_id,
                expected_generation,
                now,
            )
        )
        return ClaimReleaseResult(
            host_name=host_name,
            claim_id=claim_id,
            released_generation=expected_generation,
            remaining_claim_count=0,
            retention="while-claimed",
            empty_since=utc_timestamp(NOW),
            retire_at=utc_timestamp(NOW) if now else utc_timestamp(
                NOW + datetime.timedelta(minutes=5)
            ),
            retirement_due=now,
        )

    def enforce_retirement(self, *, execute):
        self.calls.append(("enforce", execute))
        return {
            "schema_version": "runpod.host-retirement.v1",
            "executed": execute,
            "actions": [],
        }


class ClaimCliTest(unittest.TestCase):
    def setUp(self) -> None:
        parser = argparse.ArgumentParser(prog="runpod")
        add_claim_parser(parser.add_subparsers(dest="command"))
        self.parser = parser
        self.control = FakeControl()

    def invoke(self, arguments):
        parsed = self.parser.parse_args(["claim", *arguments])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = run_claim_command(parsed, control=self.control)
        return status, output.getvalue()

    def test_list_and_show_are_machine_readable(self):
        status, output = self.invoke(["list", "dev96", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output)["claims"][0]["claim_id"],
            self.control.claim.claim_id,
        )
        status, output = self.invoke(
            [
                "show",
                "dev96",
                self.control.claim.claim_id,
                "--json",
            ]
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output)["provider_resource_id"],
            "pod-123",
        )

    def test_acquire_plan_is_read_only_and_execute_is_normalized(self):
        arguments = [
            "acquire",
            "--owner-system",
            "model-lab",
            "--owner-instance",
            "fixture",
            "--operation-id",
            "model-lab-operation-1",
            "--profile",
            "pro6000-is1",
            "--mode",
            "gpu-exclusive",
            "--gpu-device",
            "0",
            "--gpu-memory-gib",
            "24",
            "--cpu-count",
            "4",
            "--memory-gib",
            "16",
            "--ephemeral-disk-gib",
            "10",
            "--endpoint",
            "openai",
            "--json",
        ]
        _, output = self.invoke(arguments)
        plan = json.loads(output)
        self.assertFalse(plan["executed"])
        self.assertEqual(
            plan["target"]["acquisition_timeout_seconds"],
            300,
        )
        self.assertEqual(self.control.calls, [])

        _, output = self.invoke([*arguments, "--execute"])
        result = json.loads(output)
        self.assertEqual(result["claim_id"], self.control.claim.claim_id)
        request = self.control.calls[-1][1]
        self.assertEqual(request.gpu_devices, (0,))
        self.assertEqual(request.gpu_memory_gb, 24.0)
        self.assertEqual(request.endpoint_names, ("openai",))
        self.assertEqual(request.acquisition_timeout_seconds, 300)

    def test_renew_and_release_require_explicit_execution(self):
        claim_id = self.control.claim.claim_id
        _, output = self.invoke(
            [
                "renew",
                "dev96",
                claim_id,
                "--generation",
                "1",
                "--ttl",
                "3m",
                "--json",
            ]
        )
        self.assertFalse(json.loads(output)["executed"])
        self.assertEqual(self.control.calls, [])

        self.invoke(
            [
                "renew",
                "dev96",
                claim_id,
                "--generation",
                "1",
                "--ttl",
                "3m",
                "--execute",
                "--json",
            ]
        )
        self.assertEqual(self.control.calls[-1][-1], 180)

        _, output = self.invoke(
            [
                "release",
                "dev96",
                claim_id,
                "--generation",
                "2",
                "--now",
                "--json",
            ]
        )
        self.assertTrue(
            json.loads(output)["target"]["retire_now"]
        )

        self.invoke(
            [
                "release",
                "dev96",
                claim_id,
                "--generation",
                "2",
                "--now",
                "--execute",
                "--json",
            ]
        )
        self.assertEqual(self.control.calls[-1][-1], True)

    def test_enforce_defaults_to_plan_and_agents_docs_are_available(self):
        _, output = self.invoke(["enforce", "--json"])
        self.assertFalse(json.loads(output)["executed"])
        self.assertEqual(self.control.calls[-1], ("enforce", False))

        _, output = self.invoke(["--agents-md"])
        self.assertIn("runpod claim acquire", output)


if __name__ == "__main__":
    unittest.main()
