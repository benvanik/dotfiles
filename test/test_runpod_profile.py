from __future__ import annotations

import pathlib
import tempfile
import unittest

from runpod_local.errors import RunpodLocalError
from runpod_local.profile import (
    DEFAULT_CACHE_ENVIRONMENT,
    ProfileStore,
    create_profile,
)
from runpod_local.state import StateStore
from runpod_local.timeutil import parse_duration


def profile(**overrides):
    arguments = {
        "name": "nvidia-dev",
        "gpu_names": ["pro6000", "h200", "b200", "b300"],
        "max_hourly_usd": 8.0,
        "default_ttl_seconds": 4 * 60 * 60,
        "template_id": "template123",
        "network_volume_id": "volume123",
    }
    arguments.update(overrides)
    return create_profile(**arguments)


class ProfileTest(unittest.TestCase):
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
        self.assertEqual(value["limits"]["max_hourly_usd"], 8.0)
        self.assertEqual(value["lease"]["expiry_action"], "terminate")

    def test_literal_secret_is_rejected(self):
        with self.assertRaises(RunpodLocalError) as caught:
            profile(environment={"HF_TOKEN": "literal-fixture-value"})
        self.assertEqual(caught.exception.code, "literal_secret_rejected")

    def test_non_secret_name_containing_key_letters_is_allowed(self):
        value = profile(environment={"MONKEY": "banana"})
        self.assertEqual(value["pod"]["environment"]["MONKEY"], "banana")

    def test_runpod_secret_reference_is_retained(self):
        value = profile(
            environment={"HF_TOKEN": "{{ RUNPOD_SECRET_huggingface }}"}
        )
        self.assertEqual(
            value["pod"]["environment"]["HF_TOKEN"],
            "{{ RUNPOD_SECRET_huggingface }}",
        )

    def test_storage_choice_must_be_explicit(self):
        with self.assertRaises(RunpodLocalError):
            profile(network_volume_id=None)
        ephemeral = profile(network_volume_id=None, ephemeral=True)
        self.assertEqual(ephemeral["pod"]["storage_mode"], "ephemeral")

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

    def test_duration_parser_supports_composition_and_caps_lifetime(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        with self.assertRaises(RunpodLocalError):
            parse_duration("1.5h")
        with self.assertRaises(RunpodLocalError) as caught:
            parse_duration("31d")
        self.assertEqual(caught.exception.code, "duration_too_long")


if __name__ == "__main__":
    unittest.main()
