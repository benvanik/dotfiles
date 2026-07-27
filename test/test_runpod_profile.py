from __future__ import annotations

import base64
import hashlib
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from runpod_local.errors import RunpodLocalError
from runpod_local.profile import (
    DEFAULT_CACHE_ENVIRONMENT,
    ProfileStore,
    create_profile,
    load_ssh_public_key_file,
    provider_effective_environment_summary,
    validate_profile,
    validate_profile_ssh_files,
    validate_ssh_key_pair,
    validate_ssh_public_key,
)
from runpod_local.runtime_catalog import load_runtime
from runpod_local.state import StateStore
from runpod_local.template import (
    build_private_template_contract,
    environment_summary,
)
from runpod_local.timeutil import parse_duration

SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "fixture@example"
)
IMAGE = (
    "runpod/pytorch@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)
RUNTIME_ID = "vllm-cu129-v0.25.1"
RUNTIME = load_runtime(RUNTIME_ID)


def _ssh_wire_string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


OTHER_SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    + base64.b64encode(
        _ssh_wire_string(b"ssh-ed25519")
        + _ssh_wire_string(b"\x42" * 32)
    ).decode("ascii")
)


def profile(**overrides):
    arguments = {
        "name": "nvidia-dev",
        "gpu_names": ["pro6000", "h200", "b200", "b300"],
        "max_hourly_usd": 8.0,
        "default_ttl_seconds": 4 * 60 * 60,
        "image_name": IMAGE,
        "network_volume_id": "volume123",
        "ssh_public_key": SSH_PUBLIC_KEY,
    }
    arguments.update(overrides)
    return create_profile(**arguments)


def template_profile(**overrides):
    contract = RUNTIME.template_contract(
        name="upstream-runtime",
        template_id="template123",
    )
    return profile(
        image_name=RUNTIME.image,
        template_id="template123",
        template_contract=contract,
        runtime_id=RUNTIME_ID,
        **overrides,
    )


class ProfileTest(unittest.TestCase):
    def test_provider_effective_environment_adds_only_exact_public_key_mirror(
        self,
    ):
        requested = {
            "SSH_PUBLIC_KEY": SSH_PUBLIC_KEY,
            "MODEL_CACHE": "/workspace/models",
        }

        effective = provider_effective_environment_summary(requested)

        self.assertEqual(
            effective,
            environment_summary(
                {
                    **requested,
                    "PUBLIC_KEY": SSH_PUBLIC_KEY,
                }
            ),
        )
        self.assertNotIn("PUBLIC_KEY", requested)
        self.assertIsNone(
            provider_effective_environment_summary(
                {
                    **requested,
                    "PUBLIC_KEY": OTHER_SSH_PUBLIC_KEY,
                }
            )
        )
        self.assertIsNone(
            provider_effective_environment_summary(
                {"MODEL_CACHE": "/workspace/models"}
            )
        )

    def test_profile_pins_private_cache_and_safety_policy(self):
        value = profile()
        pod = value["pod"]
        self.assertEqual(pod["cloud_type"], "SECURE")
        self.assertEqual(pod["ports"], ["22/tcp"])
        self.assertFalse(pod["interruptible"])
        self.assertEqual(pod["storage_mode"], "network_volume")
        self.assertEqual(
            pod["environment"]["HF_HUB_CACHE"],
            DEFAULT_CACHE_ENVIRONMENT["HF_HUB_CACHE"],
        )
        self.assertEqual(
            pod["environment"]["HF_TOKEN_PATH"],
            "/root/runpod-session/secrets/huggingface/token",
        )
        self.assertEqual(
            pod["environment"]["TORCH_HOME"],
            "/root/runpod-session/cache/torch",
        )
        self.assertEqual(
            pod["environment"]["VLLM_CACHE_ROOT"],
            "/root/runpod-session/cache/vllm",
        )
        self.assertEqual(
            pod["environment"]["XDG_CACHE_HOME"],
            "/root/runpod-session/cache",
        )
        self.assertEqual(value["limits"]["max_hourly_usd"], 8.0)
        self.assertEqual(value["lease"]["expiry_action"], "terminate")
        self.assertEqual(
            pod["environment"]["SSH_PUBLIC_KEY"], SSH_PUBLIC_KEY
        )

    def test_literal_secret_is_rejected(self):
        with self.assertRaises(RunpodLocalError) as caught:
            profile(environment={"HF_TOKEN": "literal-fixture-value"})
        self.assertEqual(
            caught.exception.code, "invalid_profile_environment"
        )

    def test_hugging_face_secret_references_are_rejected(self):
        for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
            with self.subTest(name=name):
                with self.assertRaises(RunpodLocalError) as caught:
                    profile(
                        environment={
                            name: "{{ RUNPOD_SECRET_huggingface }}"
                        }
                    )
                self.assertEqual(
                    caught.exception.code, "invalid_profile_environment"
                )

    def test_hugging_face_token_path_cannot_use_persistent_storage(self):
        with self.assertRaises(RunpodLocalError) as caught:
            profile(
                environment={
                    "HF_TOKEN_PATH": "/workspace/.cache/huggingface/token"
                }
            )
        self.assertEqual(
            caught.exception.code, "invalid_profile_environment"
        )

    def test_non_secret_name_containing_key_letters_is_allowed(self):
        value = profile(environment={"MONKEY": "banana"})
        self.assertEqual(value["pod"]["environment"]["MONKEY"], "banana")

    def test_other_runpod_secret_references_are_rejected(self):
        for value in (
            "{{ RUNPOD_SECRET_wandb }}",
            "prefix-{{RUNPOD_SECRET_wandb}}-suffix",
        ):
            with self.subTest(value=value):
                with self.assertRaises(RunpodLocalError) as caught:
                    profile(environment={"WANDB_API_KEY": value})
                self.assertEqual(
                    caught.exception.code, "invalid_profile_environment"
                )

    def test_runpod_startup_shell_metacharacters_are_rejected(self):
        for value in (
            "$(cat /root/runpod-session/secrets/huggingface/token)",
            "`cat /root/runpod-session/secrets/huggingface/token`",
            'escaped"quote',
            "escaped\\quote",
            "line\nbreak",
            "carriage\rreturn",
            "delete\x7fcharacter",
        ):
            with self.subTest(value=value):
                with self.assertRaises(RunpodLocalError) as caught:
                    profile(environment={"SAFE_NAME": value})
                self.assertEqual(
                    caught.exception.code, "invalid_profile_environment"
                )

    def test_simple_tool_paths_remain_valid_profile_environment(self):
        value = profile(
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": "/workspace/tools",
            }
        )
        self.assertEqual(
            value["pod"]["environment"]["PATH"],
            "/usr/local/bin:/usr/bin:/bin",
        )

    def test_remote_shell_startup_environment_is_reserved(self):
        for name in (
            "BASHOPTS",
            "BASH_ENV",
            "ENV",
            "GCONV_PATH",
            "GLIBC_TUNABLES",
            "HOME",
            "LD_AUDIT",
            "LD_CUSTOM_LOADER_CONTROL",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "LOCPATH",
            "PS4",
            "SHELL",
            "SHELLOPTS",
            "ZDOTDIR",
        ):
            with self.subTest(name=name):
                with self.assertRaises(RunpodLocalError) as caught:
                    profile(environment={name: "fixture"})
                self.assertEqual(
                    caught.exception.code, "invalid_profile_environment"
                )

    def test_provider_public_key_environment_is_reserved(self):
        with self.assertRaises(RunpodLocalError) as caught:
            profile(environment={"PUBLIC_KEY": SSH_PUBLIC_KEY})
        self.assertEqual(
            caught.exception.code, "invalid_profile_environment"
        )

    def test_storage_choice_must_be_explicit(self):
        with self.assertRaises(RunpodLocalError):
            profile(network_volume_id=None)
        ephemeral = profile(network_volume_id=None, ephemeral=True)
        self.assertEqual(ephemeral["pod"]["storage_mode"], "ephemeral")

    def test_explicit_images_require_immutable_digests(self):
        with self.assertRaises(RunpodLocalError) as caught:
            profile(
                template_id=None,
                image_name="runpod/pytorch:mutable",
            )
        self.assertEqual(caught.exception.code, "invalid_profile")

        image = (
            "runpod/pytorch@sha256:"
            "1111111111111111111111111111111111111111111111111111111111111111"
        )
        value = profile(template_id=None, image_name=image)
        self.assertEqual(value["pod"]["image_name"], image)

    def test_template_profile_pins_full_provider_runtime_contract(self):
        value = template_profile()

        self.assertEqual(value["pod"]["image_name"], RUNTIME.image)
        self.assertEqual(
            value["pod"]["template_contract"]["docker_entrypoint"],
            ["/bin/bash", "-c"],
        )
        self.assertFalse(
            value["pod"]["template_contract"]["is_serverless"]
        )

    def test_template_profile_requires_matching_contract(self):
        with self.assertRaises(RunpodLocalError) as missing:
            profile(template_id="template123")
        self.assertEqual(missing.exception.code, "invalid_profile")

        contract = build_private_template_contract(
            name="upstream-runtime",
            image="vllm/vllm-openai@sha256:" + "2" * 64,
            docker_entrypoint=["/bin/bash", "-c"],
            docker_start_cmd=["exec /usr/sbin/sshd -D -e\n"],
            template_id="template123",
        )
        with self.assertRaises(RunpodLocalError) as mismatch:
            profile(
                image_name=RUNTIME.image,
                template_id="template123",
                template_contract=contract,
                runtime_id=RUNTIME_ID,
            )
        self.assertEqual(mismatch.exception.code, "invalid_profile")

    def test_template_profile_rejects_template_local_volume(self):
        contract = template_profile()["pod"]["template_contract"]
        contract["volume_in_gb"] = 20

        with self.assertRaises(RunpodLocalError) as caught:
            profile(
                image_name=RUNTIME.image,
                template_id="template123",
                template_contract=contract,
                runtime_id=RUNTIME_ID,
            )

        self.assertEqual(caught.exception.code, "invalid_profile")

    def test_template_contract_tamper_invalidates_stored_profile(self):
        value = template_profile()
        value["pod"]["template_contract"]["image"] = (
            "other/image@sha256:" + "2" * 64
        )

        with self.assertRaises(RunpodLocalError) as caught:
            validate_profile(value)

        self.assertEqual(caught.exception.code, "invalid_profile")

    def test_template_profile_requires_separate_network_volume(self):
        with self.assertRaises(RunpodLocalError) as caught:
            template_profile(
                network_volume_id=None,
                ephemeral=True,
            )
        self.assertEqual(caught.exception.code, "invalid_profile")

    def test_runtime_identity_tamper_invalidates_stored_profile(self):
        value = template_profile()
        value["pod"]["runtime"]["manifest"]["sha256"] = "0" * 64

        with self.assertRaises(RunpodLocalError) as caught:
            validate_profile(value)

        self.assertEqual(caught.exception.code, "invalid_profile")

    def test_profile_store_is_private_and_refuses_implicit_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(pathlib.Path(directory) / "state")
            store = ProfileStore(state)
            value = profile()
            store.save(value)

            record_path = state.record_path("profiles", "nvidia-dev")
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(record_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(store.load("nvidia-dev")["name"], "nvidia-dev")
            with self.assertRaises(RunpodLocalError) as caught:
                store.save(value)
            self.assertEqual(caught.exception.code, "profile_exists")

    def test_state_record_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = StateStore(root / "state")
            profile_dir = state.record_path("profiles", "nvidia-dev").parent
            profile_dir.mkdir(parents=True)
            target = root / "target.json"
            target.write_text("{}")
            target.chmod(0o600)
            state.record_path("profiles", "nvidia-dev").symlink_to(target)
            with self.assertRaises(RunpodLocalError) as caught:
                state.read("profiles", "nvidia-dev")
            self.assertEqual(caught.exception.code, "unsafe_state_record")

    def test_public_key_validation_rejects_non_key_text_and_controls(self):
        self.assertEqual(validate_ssh_public_key(SSH_PUBLIC_KEY), SSH_PUBLIC_KEY)
        for value in (
            "",
            "ssh-dss AAAAB3NzaC1kc3MAAACB",
            "ssh-ed25519 not-base64!",
            "ssh-ed25519 YWJj",
            OTHER_SSH_PUBLIC_KEY.replace("ssh-ed25519", "ssh-rsa", 1),
            (
                "ssh-ed25519 "
                + base64.b64encode(
                    _ssh_wire_string(b"ssh-ed25519")
                    + _ssh_wire_string(b"\x42" * 31)
                ).decode("ascii")
            ),
            f"{OTHER_SSH_PUBLIC_KEY} ",
            f"{OTHER_SSH_PUBLIC_KEY}\x7f",
            f"{SSH_PUBLIC_KEY}\n",
            f"{SSH_PUBLIC_KEY}\tcomment",
        ):
            with self.subTest(value=value):
                with self.assertRaises(RunpodLocalError) as caught:
                    validate_ssh_public_key(value)
                self.assertEqual(
                    caught.exception.code, "invalid_ssh_public_key"
                )

    def test_public_key_file_is_single_owned_non_writable_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            public_key_path = root / "id_ed25519.pub"
            public_key_path.write_text(f"{SSH_PUBLIC_KEY}\n")
            public_key_path.chmod(0o644)

            loaded_path, loaded_key = load_ssh_public_key_file(
                str(public_key_path)
            )
            self.assertEqual(loaded_path, public_key_path)
            self.assertEqual(loaded_key, SSH_PUBLIC_KEY)

            public_key_path.chmod(0o664)
            with self.assertRaises(RunpodLocalError):
                load_ssh_public_key_file(str(public_key_path))

            public_key_path.chmod(0o644)
            public_key_path.write_text(
                f"{SSH_PUBLIC_KEY}\n{OTHER_SSH_PUBLIC_KEY}\n"
            )
            with self.assertRaises(RunpodLocalError):
                load_ssh_public_key_file(str(public_key_path))

            linked_path = root / "linked.pub"
            linked_path.symlink_to(public_key_path)
            with self.assertRaises(RunpodLocalError):
                load_ssh_public_key_file(str(linked_path))

    def test_key_pair_is_derived_noninteractively_and_compared_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            identity_path = pathlib.Path(directory) / "id_ed25519"
            identity_path.write_text("fixture private material")
            identity_path.chmod(0o600)
            completed = subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    " ".join(SSH_PUBLIC_KEY.split(maxsplit=2)[:2]) + "\n"
                ).encode("utf-8"),
                stderr=b"",
            )
            with mock.patch(
                "runpod_local.profile.subprocess.run",
                return_value=completed,
            ) as run:
                validate_ssh_key_pair(str(identity_path), SSH_PUBLIC_KEY)
            self.assertEqual(
                run.call_args.args[0],
                [
                    "ssh-keygen",
                    "-y",
                    "-P",
                    "",
                    "-f",
                    str(identity_path),
                ],
            )
            with mock.patch(
                "runpod_local.profile.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=f"{OTHER_SSH_PUBLIC_KEY}\n".encode("utf-8"),
                    stderr=b"",
                ),
            ):
                with self.assertRaises(RunpodLocalError) as caught:
                    validate_ssh_key_pair(str(identity_path), SSH_PUBLIC_KEY)
            self.assertEqual(caught.exception.code, "ssh_key_mismatch")

    def test_profile_rejects_tampered_public_key_identity(self):
        value = profile()
        value["ssh"]["public_key_sha256"] = "0" * 64
        with self.assertRaises(RunpodLocalError) as caught:
            validate_profile(value)
        self.assertEqual(caught.exception.code, "invalid_profile")

    def test_launch_preflight_rejects_consistent_injected_key_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            identity_path = root / "id_ed25519"
            identity_path.write_text("fixture private material")
            identity_path.chmod(0o600)
            public_key_path = root / "id_ed25519.pub"
            public_key_path.write_text(f"{SSH_PUBLIC_KEY}\n")
            public_key_path.chmod(0o644)
            value = profile(
                identity_file=str(identity_path),
                public_key_file=str(public_key_path),
            )
            value["pod"]["environment"][
                "SSH_PUBLIC_KEY"
            ] = OTHER_SSH_PUBLIC_KEY
            value["ssh"]["public_key_sha256"] = hashlib.sha256(
                OTHER_SSH_PUBLIC_KEY.encode("utf-8")
            ).hexdigest()
            validate_profile(value)

            with self.assertRaises(RunpodLocalError) as caught:
                validate_profile_ssh_files(value)
            self.assertEqual(caught.exception.code, "ssh_key_mismatch")

    def test_malformed_profile_fields_fail_with_typed_errors(self):
        malformed_values = []
        value = profile()
        value["ssh"]["public_key_file"] = 5
        malformed_values.append(value)
        value = profile()
        value["pod"]["environment"] = []
        malformed_values.append(value)
        value = profile()
        del value["created_at"]
        malformed_values.append(value)
        for value in malformed_values:
            with self.subTest(value=value):
                with self.assertRaises(RunpodLocalError) as caught:
                    validate_profile(value)
                self.assertEqual(caught.exception.code, "invalid_profile")

        with self.assertRaises(RunpodLocalError) as caught:
            validate_ssh_public_key("ssh-ed25519 \ud800")
        self.assertEqual(caught.exception.code, "invalid_ssh_public_key")

    def test_duration_parser_supports_composition_and_caps_lifetime(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        with self.assertRaises(RunpodLocalError):
            parse_duration("1.5h")
        with self.assertRaises(RunpodLocalError) as caught:
            parse_duration("31d")
        self.assertEqual(caught.exception.code, "duration_too_long")


if __name__ == "__main__":
    unittest.main()
