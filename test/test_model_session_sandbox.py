from __future__ import annotations

import dataclasses
import errno
import os
import pathlib
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from model_session.attachment import publish_inference_attachment
from model_session.errors import ModelSessionError
from model_session.pi_runtime import fingerprint_pi_installation
from model_session.profile import (
    ModelContract,
    PiContract,
    ProfileContract,
    RuntimeContract,
)
from model_session.runs import LockedResource, SessionRun
from model_session.sandbox import (
    BWRAP_BINARY,
    DENIED_COMMAND_DESTINATIONS,
    INFERENCE_SOCKET_DESTINATION,
    PRIVATE_CONFIG_BYTES,
    PRIVATE_HOME_BYTES,
    PRIVATE_SHM_BYTES,
    PRIVATE_TMP_BYTES,
    build_sandbox_plan,
    validate_bwrap,
)


SESSION_ID = "20260726T120000000000Z-0123456789abcdef"


def _private_directory(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


class SandboxFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ms.",
            dir="/tmp",
        )
        self.root = pathlib.Path(self.temporary.name)
        self.state_root = _private_directory(self.root / "state")
        self.session_root = _private_directory(
            self.state_root / "sessions" / "profile" / SESSION_ID
        )
        self.snapshot = _private_directory(self.session_root / "snapshot")
        self.workspace = _private_directory(self.session_root / "workspace")
        self.workspace_authority = _private_directory(self.workspace / ".pi")
        (self.workspace_authority / "settings.json").write_text(
            '{"extensions":["hostile.js"]}\n',
            encoding="utf-8",
        )
        (self.workspace / "AGENTS.md").write_text(
            "isolated workspace\n",
            encoding="utf-8",
        )
        snapshot_profile = _private_directory(self.snapshot / "profile")
        (snapshot_profile / "locked.txt").write_text(
            "locked profile\n",
            encoding="utf-8",
        )
        (self.snapshot / "lock.json").write_text(
            '{"host_path":"/must/not/be/visible"}\n',
            encoding="utf-8",
        )
        snapshot_pi = _private_directory(self.snapshot / "pi")
        models_path = snapshot_pi / "models.json"
        models_path.write_text('{"providers":{}}\n', encoding="utf-8")
        models_path.chmod(0o600)
        snapshot_runtime = _private_directory(self.snapshot / "runtime")
        relay_path = snapshot_runtime / "relay.py"
        relay_path.write_text("# locked relay\n", encoding="utf-8")
        relay_path.chmod(0o600)
        policy_path = snapshot_runtime / "session-policy.js"
        policy_path.write_text("// locked policy\n", encoding="utf-8")
        policy_path.chmod(0o600)
        self.resources = (
            LockedResource(
                relative_path=pathlib.PurePosixPath("pi/models.json"),
                roles=("pi_models",),
                path=models_path,
                sha256="0" * 64,
                size=models_path.stat().st_size,
            ),
            LockedResource(
                relative_path=pathlib.PurePosixPath("runtime/relay.py"),
                roles=("inference_relay",),
                path=relay_path,
                sha256="0" * 64,
                size=relay_path.stat().st_size,
            ),
            LockedResource(
                relative_path=pathlib.PurePosixPath(
                    "runtime/session-policy.js"
                ),
                roles=("session_policy",),
                path=policy_path,
                sha256="0" * 64,
                size=policy_path.stat().st_size,
            ),
        )

        pi_root = _private_directory(self.session_root / "pi")
        self.pi_sessions = _private_directory(pi_root / "sessions")

        self.project = _private_directory(self.root / "project")
        reports = _private_directory(self.project / "reports")
        memory = _private_directory(self.project / "memory")
        self.report = _private_directory(reports / SESSION_ID)
        self.memory = _private_directory(memory / SESSION_ID)
        (self.project / "shared.txt").write_text("shared\n", encoding="utf-8")
        other_session_id = (
            "20260726T120000000001Z-fedcba9876543210"
        )
        self.other_report = _private_directory(reports / other_session_id)
        self.other_memory = _private_directory(memory / other_session_id)
        (self.other_report / "sealed.txt").write_text(
            "other report\n",
            encoding="utf-8",
        )
        (self.other_memory / "sealed.txt").write_text(
            "other memory\n",
            encoding="utf-8",
        )

        self.pi_installation = _private_directory(self.root / "pi-installation")
        pi_bin = _private_directory(self.pi_installation / "bin")
        pi_executable = pi_bin / "pi"
        pi_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        pi_executable.chmod(0o700)
        pi_node = pi_bin / "node"
        pi_node.write_text("#!/bin/sh\necho v24.11.1\n", encoding="utf-8")
        pi_node.chmod(0o700)

        socket_root = _private_directory(self.root / "runtime")
        self.socket_path = socket_root / "inference.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(os.fspath(self.socket_path))
        self.listener.listen(128)
        self.socket_path.chmod(0o600)

        profile = ProfileContract(
            schema="model-session.profile.v1",
            profile_id="profile",
            project_id="project",
            profile_root=self.root / "profile",
            state_root=self.state_root,
            project_root=self.project,
            model=ModelContract(
                repository="fixture/model",
                revision="a" * 40,
                context_tokens=65536,
                max_output_tokens=8192,
                kv_cache_dtype="bf16",
                max_sequences=1,
                weight_format="bf16",
            ),
            runtime=RuntimeContract(
                provider="fixture",
                model_id="fixture-model",
                reasoning=False,
                input_modalities=("text",),
            ),
            pi=PiContract(
                installation_root=self.pi_installation,
                executable=pathlib.PurePosixPath("bin/pi"),
                version="0.82.1",
                tools=("read", "write", "edit", "bash"),
                system_prompt_file=None,
                append_system_prompt_file=None,
            ),
        )
        self.run = SessionRun(
            session_id=SESSION_ID,
            created_at="2026-07-26T12:00:00.000000Z",
            root=self.session_root,
            profile=profile,
            snapshot_root=self.snapshot,
            workspace=self.workspace,
            pi_sessions=self.pi_sessions,
            report_directory=self.report,
            memory_directory=self.memory,
            resources=self.resources,
            pi_installation=fingerprint_pi_installation(profile),
        )
        self.attachment_runtime_root = self.root / "attachment-runtime"
        self.attachment = publish_inference_attachment(
            profile,
            self.socket_path,
            ttl_seconds=3600,
            runtime_root=self.attachment_runtime_root,
        )

    def close(self) -> None:
        self.listener.close()
        self.temporary.cleanup()


class SandboxPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = SandboxFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def build(self, command: tuple[str, ...] | None = None):
        if command is None:
            command = ("/usr/bin/python3", "-c", "print('ok')")
        with mock.patch(
            "model_session.sandbox.validate_bwrap",
            return_value=(0, 11, 0),
        ):
            return build_sandbox_plan(
                self.fixture.run,
                command=command,
                attachment_runtime_root=(
                    self.fixture.attachment_runtime_root
                ),
            )

    def test_plan_uses_only_descriptor_backed_host_mounts(self) -> None:
        with self.build() as plan:
            argv = plan.argv
            self.assertEqual(argv[0], "/usr/bin/bwrap")
            for required in (
                "--unshare-all",
                "--unshare-user",
                "--disable-userns",
                "--assert-userns-disabled",
                "--new-session",
                "--die-with-parent",
                "--clearenv",
                "--hostname",
            ):
                self.assertIn(required, argv)
            self.assertNotIn("--share-net", argv)
            self.assertNotIn("--bind", argv)
            self.assertNotIn("--ro-bind", argv)
            self.assertIn("--bind-fd", argv)
            self.assertIn("--ro-bind-fd", argv)
            self.assertEqual(len(plan.pass_fds), len(set(plan.pass_fds)))

            source_paths = (
                self.fixture.workspace,
                self.fixture.snapshot,
                self.fixture.pi_installation,
                self.fixture.project,
                self.fixture.report,
                self.fixture.memory,
                self.fixture.socket_path,
            )
            for source in source_paths:
                self.assertNotIn(os.fspath(source), argv)

            workspace_bind = argv.index("/workspace")
            workspace_authority_mask = argv.index("/workspace/.pi")
            self.assertLess(workspace_bind, workspace_authority_mask)
            usr_bind = argv.index("/usr")
            usr_local_mask = argv.index("/usr/local")
            self.assertLess(usr_bind, usr_local_mask)
            project_bind = argv.index("/project")
            report_bind = argv.index(f"/project/reports/{SESSION_ID}")
            memory_bind = argv.index(f"/project/memory/{SESSION_ID}")
            self.assertLess(project_bind, report_bind)
            self.assertLess(project_bind, memory_bind)
            socket_bind = argv.index(INFERENCE_SOCKET_DESTINATION)
            self.assertGreater(socket_bind, project_bind)
            self.assertEqual(argv[-1], "print('ok')")
            root_read_only = max(
                index
                for index, value in enumerate(argv)
                if value == "--remount-ro" and argv[index + 1] == "/"
            )
            self.assertGreater(root_read_only, socket_bind)

            clear_environment = argv.index("--clearenv")
            first_set_environment = argv.index("--setenv")
            self.assertLess(clear_environment, first_set_environment)
            rendered = "\0".join(argv)
            for forbidden in (
                "SSH_AUTH_SOCK",
                "DOCKER_HOST",
                "RUNPOD_API_KEY",
                "AWS_SECRET_ACCESS_KEY",
                os.fspath(pathlib.Path.home()),
            ):
                self.assertNotIn(forbidden, rendered)

            installed_denied = [
                destination
                for destination in DENIED_COMMAND_DESTINATIONS
                if pathlib.Path(destination).is_file()
                and not pathlib.Path(destination).is_symlink()
            ]
            for destination in installed_denied:
                self.assertIn(destination, argv)

    def test_plan_owns_and_closes_every_mount_descriptor(self) -> None:
        plan = self.build()
        descriptors = plan.pass_fds
        for descriptor in descriptors:
            os.fstat(descriptor)
        plan.close()
        self.assertTrue(plan.closed)
        plan.close()
        for descriptor in descriptors:
            with self.assertRaises(OSError) as caught:
                os.fstat(descriptor)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_rejects_missing_workspace_authority_mask_target(self) -> None:
        (self.fixture.workspace_authority / "settings.json").unlink()
        self.fixture.workspace_authority.rmdir()
        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "unsafe_sandbox_source")

    def test_rejects_noncanonical_locked_runtime_resource(self) -> None:
        models = self.fixture.run.resource_for_role("pi_models")
        replacement = dataclasses.replace(
            models,
            path=self.fixture.root / "outside-models.json",
        )
        self.fixture.run = dataclasses.replace(
            self.fixture.run,
            resources=tuple(
                replacement if resource is models else resource
                for resource in self.fixture.run.resources
            ),
        )

        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertIn("canonical snapshot path", str(caught.exception))

    def test_rejects_symlinked_source(self) -> None:
        alternate = _private_directory(self.fixture.root / "alternate-workspace")
        (self.fixture.workspace_authority / "settings.json").unlink()
        self.fixture.workspace_authority.rmdir()
        (self.fixture.workspace / "AGENTS.md").unlink()
        self.fixture.workspace.rmdir()
        self.fixture.workspace.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "unsafe_sandbox_source")

    def test_rejects_socket_with_shared_permissions_and_closes_partial_fds(
        self,
    ) -> None:
        self.fixture.socket_path.chmod(0o660)
        before = len(tuple(pathlib.Path("/proc/self/fd").iterdir()))
        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        after = len(tuple(pathlib.Path("/proc/self/fd").iterdir()))
        self.assertEqual(
            caught.exception.code,
            "inference_attachment_unavailable",
        )
        self.assertEqual(before, after)

    def test_rejects_noncanonical_project_overlay(self) -> None:
        self.fixture.run = dataclasses.replace(
            self.fixture.run,
            report_directory=self.fixture.memory,
        )
        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertIn("canonical project path", str(caught.exception))

    def test_rejects_unlocked_entrypoint(self) -> None:
        with self.assertRaises(ModelSessionError) as caught:
            self.build(("/usr/bin/sh", "-c", "true"))
        self.assertIn("entrypoint", str(caught.exception))

    def test_rejects_entrypoint_path_traversal(self) -> None:
        with self.assertRaises(ModelSessionError) as caught:
            self.build(("/opt/pi/../../usr/bin/python3", "-c", "pass"))
        self.assertIn("normalized", str(caught.exception))

    def test_rejects_invalid_session_id_before_building_destinations(self) -> None:
        self.fixture.run = dataclasses.replace(
            self.fixture.run,
            session_id="../escape",
        )
        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertIn("session ID", str(caught.exception))

    def test_rejects_inference_socket_reachable_through_project(self) -> None:
        nested_socket = self.fixture.project / "nested.sock"
        nested_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            nested_listener.bind(os.fspath(nested_socket))
            nested_socket.chmod(0o600)
            nested_attachment = dataclasses.replace(
                self.fixture.attachment,
                socket_path=nested_socket,
                socket_device=nested_socket.stat().st_dev,
                socket_inode=nested_socket.stat().st_ino,
            )
            with (
                mock.patch(
                    "model_session.sandbox.validate_bwrap",
                    return_value=(0, 11, 0),
                ),
                mock.patch(
                    "model_session.sandbox.load_inference_attachment",
                    return_value=nested_attachment,
                ),
            ):
                with self.assertRaises(ModelSessionError) as caught:
                    build_sandbox_plan(
                        self.fixture.run,
                        command=("/usr/bin/python3", "-c", "pass"),
                        attachment_runtime_root=(
                            self.fixture.attachment_runtime_root
                        ),
                    )
            self.assertIn("another mount", str(caught.exception))
        finally:
            nested_listener.close()

    def test_rejects_inference_socket_reachable_through_sessions(self) -> None:
        nested_socket = self.fixture.pi_sessions / "nested.sock"
        nested_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            nested_listener.bind(os.fspath(nested_socket))
            nested_socket.chmod(0o600)
            nested_attachment = dataclasses.replace(
                self.fixture.attachment,
                socket_path=nested_socket,
                socket_device=nested_socket.stat().st_dev,
                socket_inode=nested_socket.stat().st_ino,
            )
            with mock.patch(
                "model_session.sandbox.load_inference_attachment",
                return_value=nested_attachment,
            ):
                with self.assertRaises(ModelSessionError) as caught:
                    self.build()
            self.assertIn("another mount", str(caught.exception))
        finally:
            nested_listener.close()

    def test_rejects_attachment_for_another_workload(self) -> None:
        mismatched = dataclasses.replace(
            self.fixture.attachment,
            workload_sha256="f" * 64,
        )
        with mock.patch(
            "model_session.sandbox.load_inference_attachment",
            return_value=mismatched,
        ):
            with self.assertRaises(ModelSessionError) as caught:
                self.build()
        self.assertEqual(caught.exception.code, "inference_attachment_mismatch")

    def test_rejects_socket_replaced_after_attachment_load(self) -> None:
        self.fixture.listener.close()
        self.fixture.socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(os.fspath(self.fixture.socket_path))
        replacement.listen(1)
        self.fixture.socket_path.chmod(0o600)
        self.fixture.listener = replacement

        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertEqual(
            caught.exception.code,
            "inference_attachment_unavailable",
        )

    def test_rejects_pi_installation_changed_after_run_load(self) -> None:
        executable = self.fixture.pi_installation / "bin" / "pi"
        executable.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
        executable.chmod(0o700)

        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "pi_installation_changed")


class BubblewrapProbeTests(unittest.TestCase):
    def test_installed_bwrap_has_required_capabilities(self) -> None:
        if not BWRAP_BINARY.exists():
            self.skipTest(f"{BWRAP_BINARY} is not installed")
        version = validate_bwrap()
        self.assertGreaterEqual(version, (0, 11, 0))

    def test_old_bwrap_is_rejected(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["bwrap", "--version"],
            returncode=0,
            stdout="bubblewrap 0.10.0\n",
            stderr="",
        )
        with mock.patch(
            "model_session.sandbox._probe_bwrap",
            return_value=completed,
        ):
            with self.assertRaises(ModelSessionError) as caught:
                validate_bwrap()
        self.assertEqual(caught.exception.code, "bwrap_version_unsupported")

    def test_missing_bwrap_capability_is_rejected(self) -> None:
        version = subprocess.CompletedProcess(
            args=["bwrap", "--version"],
            returncode=0,
            stdout="bubblewrap 0.11.0\n",
            stderr="",
        )
        help_result = subprocess.CompletedProcess(
            args=["bwrap", "--help"],
            returncode=0,
            stdout="--unshare-all --unshare-user\n",
            stderr="",
        )
        with mock.patch(
            "model_session.sandbox._probe_bwrap",
            side_effect=(version, help_result),
        ):
            with self.assertRaises(ModelSessionError) as caught:
                validate_bwrap()
        self.assertEqual(caught.exception.code, "bwrap_capability_unsupported")


class BubblewrapVisibilitySmokeTests(unittest.TestCase):
    def test_real_boundary_exposes_only_intended_writable_surfaces(self) -> None:
        if not BWRAP_BINARY.exists():
            self.skipTest(f"{BWRAP_BINARY} is not installed")
        fixture = SandboxFixture()
        script = f"""
import os
import pathlib
import socket

assert pathlib.Path.cwd() == pathlib.Path("/workspace")
assert socket.gethostname() == "model-session"
assert pathlib.Path("/workspace/AGENTS.md").read_text() == "isolated workspace\\n"
assert not pathlib.Path("/workspace/.pi/settings.json").exists()
assert pathlib.Path("/profile/locked.txt").read_text() == "locked profile\\n"
assert not pathlib.Path("/profile/lock.json").exists()
assert pathlib.Path("/config/models.json").read_text() == '{{"providers":{{}}}}\\n'
assert pathlib.Path("/runtime/relay.py").read_text() == "# locked relay\\n"
assert pathlib.Path("/project/shared.txt").read_text() == "shared\\n"
assert not pathlib.Path("/home/ben").exists()
assert "AWS_SECRET_ACCESS_KEY" not in os.environ
assert not os.access("/usr/bin/ssh", os.X_OK)
assert not os.access("/usr/bin/docker", os.X_OK)
assert not pathlib.Path("/usr/local/bin/1password-mcp").exists()

for path in (
    "/workspace/written",
    "/home/agent/written",
    "/config/auth.json",
    "/config/models-store.json",
    "/config/settings.json.lock",
    "/dev/shm/written",
    "/sessions/written",
    "/project/reports/{SESSION_ID}/written",
    "/project/memory/{SESSION_ID}/written",
):
    pathlib.Path(path).write_text("ok\\n")

for path in (
    "/workspace/.pi/written",
    "/config/models.json",
    "/profile/written",
    "/opt/pi/written",
    "/project/written",
    "/rootwrite",
    "/home/escape",
    "/run/escape",
    "/dev/escape",
):
    try:
        pathlib.Path(path).write_text("forbidden\\n")
    except OSError:
        pass
    else:
        raise AssertionError(f"unexpected writable path: {{path}}")

for path in (
    "/project/reports/20260726T120000000001Z-fedcba9876543210/sealed.txt",
    "/project/memory/20260726T120000000001Z-fedcba9876543210/sealed.txt",
):
    try:
        pathlib.Path(path).write_text("poisoned\\n")
    except OSError:
        pass
    else:
        raise AssertionError(f"unexpected mutable prior session file: {{path}}")
    try:
        pathlib.Path(path).unlink()
    except OSError:
        pass
    else:
        raise AssertionError(f"unexpected removable prior session file: {{path}}")

interfaces = {{
    line.split(":", 1)[0].strip()
    for line in pathlib.Path("/proc/net/dev").read_text().splitlines()
    if ":" in line
}}
assert interfaces <= {{"lo"}}, interfaces
tmp = os.statvfs("/tmp")
assert tmp.f_blocks * tmp.f_frsize <= {PRIVATE_TMP_BYTES}
home = os.statvfs("/home/agent")
assert home.f_blocks * home.f_frsize <= {PRIVATE_HOME_BYTES}
config = os.statvfs("/config")
assert config.f_blocks * config.f_frsize <= {PRIVATE_CONFIG_BYTES}
shared_memory = os.statvfs("/dev/shm")
assert shared_memory.f_blocks * shared_memory.f_frsize <= {PRIVATE_SHM_BYTES}
assert pathlib.Path("{INFERENCE_SOCKET_DESTINATION}").is_socket()
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.connect("{INFERENCE_SOCKET_DESTINATION}")
client.sendall(b"descriptor-socket")
client.close()
print("sandbox-ok")
"""
        try:
            with build_sandbox_plan(
                fixture.run,
                command=("/usr/bin/python3", "-c", script),
                attachment_runtime_root=fixture.attachment_runtime_root,
            ) as plan:
                environment = os.environ.copy()
                environment["AWS_SECRET_ACCESS_KEY"] = "must-not-cross"
                result = subprocess.run(
                    plan.argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    pass_fds=plan.pass_fds,
                    env=environment,
                )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            received = b""
            fixture.listener.settimeout(2)
            for _ in range(8):
                connection, _ = fixture.listener.accept()
                with connection:
                    received = connection.recv(64)
                if received:
                    break
            self.assertEqual(result.stdout.strip(), "sandbox-ok")
            self.assertEqual(received, b"descriptor-socket")
            self.assertTrue((fixture.workspace / "written").is_file())
            self.assertFalse((fixture.session_root / "pi" / "home").exists())
            self.assertFalse((fixture.snapshot / "pi" / "written").exists())
            self.assertFalse(
                (fixture.snapshot / "pi" / "auth.json").exists()
            )
            self.assertFalse(
                (fixture.workspace_authority / "written").exists()
            )
            self.assertTrue((fixture.report / "written").is_file())
            self.assertFalse((fixture.project / "written").exists())
            self.assertEqual(
                (fixture.other_report / "sealed.txt").read_text(),
                "other report\n",
            )
            self.assertEqual(
                (fixture.other_memory / "sealed.txt").read_text(),
                "other memory\n",
            )
        finally:
            fixture.listener.close()
            fixture.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
