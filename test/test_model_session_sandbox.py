from __future__ import annotations

import dataclasses
import errno
import json
import os
import pathlib
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from model_session.attachment import publish_inference_attachment
from model_session.errors import ModelSessionError
from model_session.lease import RunLease, acquire_run_from_state
from model_session.profile import load_profile
from model_session.materialization import materialize_new_run
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


REVISION = "a" * 40
OTHER_SESSION_ID = "20260726T120000000001Z-fedcba9876543210"


def _private_directory(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


class SandboxFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ms.", dir="/tmp")
        self.root = pathlib.Path(self.temporary.name)
        self.profile_root = self.root / "profiles" / "profile"
        self.profile_root.mkdir(parents=True)
        self.profile_root.chmod(0o755)
        self.state_root = self.root / "state"
        self.project = _private_directory(self.root / "project")
        self.project.chmod(0o775)
        self.pi_installation = _private_directory(
            self.root / "pi-installation"
        )
        pi_bin = _private_directory(self.pi_installation / "bin")
        pi_executable = pi_bin / "pi"
        pi_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        pi_executable.chmod(0o700)
        pi_node = pi_bin / "node"
        pi_node.write_text("#!/bin/sh\necho v24.11.1\n", encoding="utf-8")
        pi_node.chmod(0o700)

        (self.profile_root / "AGENTS.md").write_text(
            "isolated workspace\n",
            encoding="utf-8",
        )
        (self.profile_root / "SYSTEM.md").write_text(
            "locked system prompt\n",
            encoding="utf-8",
        )
        for name in ("AGENTS.md", "SYSTEM.md"):
            (self.profile_root / name).chmod(0o644)
        (self.profile_root / "profile.toml").write_text(
            f"""schema = "model-session.profile.v1"
profile_id = "profile"
project_id = "project"
state_root = "{self.state_root}"
project_root = "{self.project}"

[model]
repository = "fixture/model"
revision = "{REVISION}"
context_tokens = 65536
max_output_tokens = 8192
kv_cache_dtype = "bf16"
max_sequences = 1
weight_format = "bf16"

[runtime]
provider = "fixture"
model_id = "fixture-model"
reasoning = false
input_modalities = ["text"]

[pi]
installation_root = "{self.pi_installation}"
executable = "bin/pi"
version = "0.82.1"
tools = ["read", "write", "edit", "bash"]
system_prompt_file = "SYSTEM.md"
""",
            encoding="utf-8",
        )
        (self.profile_root / "profile.toml").chmod(0o644)

        profile = load_profile(self.profile_root)
        self.run = materialize_new_run(profile)
        self.session_root = self.run.root
        self.snapshot = self.run.snapshot_root
        self.workspace = self.run.workspace
        self.workspace_authority = self.workspace / ".pi"
        self.pi_sessions = self.run.pi_sessions
        self.report = self.run.report_directory
        self.memory = self.run.memory_directory

        (self.project / "shared.txt").write_text("shared\n", encoding="utf-8")
        reports = self.project / "reports"
        memory = self.project / "memory"
        self.other_report = _private_directory(reports / OTHER_SESSION_ID)
        self.other_memory = _private_directory(memory / OTHER_SESSION_ID)
        (self.other_report / "sealed.txt").write_text(
            "other report\n",
            encoding="utf-8",
        )
        (self.other_memory / "sealed.txt").write_text(
            "other memory\n",
            encoding="utf-8",
        )

        socket_root = _private_directory(self.root / "runtime")
        self.socket_path = socket_root / "inference.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(os.fspath(self.socket_path))
        self.listener.listen(128)
        self.socket_path.chmod(0o600)
        self.attachment_runtime_root = self.root / "attachment-runtime"
        self.attachment = publish_inference_attachment(
            self.run.profile,
            self.socket_path,
            ttl_seconds=3600,
            runtime_root=self.attachment_runtime_root,
        )
        self.lease = acquire_run_from_state(
            self.state_root,
            "profile",
            self.run.session_id,
        )

    def close(self) -> None:
        self.lease.close()
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
                self.fixture.lease,
                command=command,
                attachment_runtime_root=self.fixture.attachment_runtime_root,
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
                "--as-pid-1",
                "--new-session",
                "--die-with-parent",
                "--json-status-fd",
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

            self.assertLess(argv.index("/workspace"), argv.index("/workspace/.pi"))
            self.assertLess(argv.index("/usr"), argv.index("/usr/local"))
            project_bind = argv.index("/project")
            report_bind = argv.index(
                f"/project/reports/{self.fixture.run.session_id}"
            )
            memory_bind = argv.index(
                f"/project/memory/{self.fixture.run.session_id}"
            )
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

            self.assertLess(argv.index("--clearenv"), argv.index("--setenv"))
            rendered = "\0".join(argv)
            for forbidden in (
                "SSH_AUTH_SOCK",
                "DOCKER_HOST",
                "RUNPOD_API_KEY",
                "AWS_SECRET_ACCESS_KEY",
                os.fspath(pathlib.Path.home()),
            ):
                self.assertNotIn(forbidden, rendered)
            for destination in DENIED_COMMAND_DESTINATIONS:
                if (
                    pathlib.Path(destination).is_file()
                    and not pathlib.Path(destination).is_symlink()
                ):
                    self.assertIn(destination, argv)

    def test_accepts_project_owned_by_a_private_writable_group(self) -> None:
        self.assertEqual(stat.S_IMODE(self.fixture.project.stat().st_mode), 0o775)
        with self.build() as plan:
            self.assertIn("/project", plan.argv)

    def test_plan_owns_descriptors_and_the_exclusive_run_lease(self) -> None:
        plan = self.build()
        descriptors = plan.pass_fds
        for descriptor in descriptors:
            os.fstat(descriptor)
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.lease.close()
        self.assertEqual(caught.exception.code, "session_lease_owned")
        with self.assertRaises(ModelSessionError) as caught:
            acquire_run_from_state(
                self.fixture.state_root,
                "profile",
                self.fixture.run.session_id,
            )
        self.assertEqual(caught.exception.code, "session_in_use")
        plan.close()
        self.assertTrue(plan.closed)
        plan.close()
        for descriptor in descriptors:
            with self.assertRaises(OSError) as caught:
                os.fstat(descriptor)
            self.assertEqual(caught.exception.errno, errno.EBADF)
        self.assertTrue(self.fixture.lease.closed)
        with acquire_run_from_state(
            self.fixture.state_root,
            "profile",
            self.fixture.run.session_id,
        ) as replacement:
            self.assertFalse(replacement.closed)

    def test_plan_construction_failure_rolls_back_lease_transfer(self) -> None:
        with mock.patch(
            "model_session.sandbox.SandboxPlan",
            side_effect=MemoryError("injected construction failure"),
        ):
            with self.assertRaises(MemoryError):
                self.build()
        self.assertFalse(self.fixture.lease.closed)
        self.fixture.lease.close()
        with acquire_run_from_state(
            self.fixture.state_root,
            "profile",
            self.fixture.run.session_id,
        ) as replacement:
            self.assertFalse(replacement.closed)

    def test_child_identity_rejects_a_pid_outside_the_live_monitor(
        self,
    ) -> None:
        monitor = subprocess.Popen(
            ("/usr/bin/sleep", "60"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with self.build() as plan:
                owned_before = tuple(plan._owned_descriptors)
                status_write_descriptor = plan._status_write_descriptor
                status = json.dumps({"child-pid": os.getpid()}).encode("ascii")
                os.write(plan._status_write_descriptor, status + b"\n")
                with self.assertRaises(ModelSessionError) as caught:
                    plan.sandbox_child_pid(monitor)
                self.assertEqual(
                    caught.exception.code,
                    "sandbox_launch_failed",
                )
                self.assertIn("direct monitor ancestry", str(caught.exception))
                self.assertEqual(plan._sandbox_child_pid_descriptor, -1)
                self.assertIsNone(plan._sandbox_child_pid)
                self.assertIsNone(plan._sandbox_monitor)
                self.assertEqual(
                    tuple(plan._owned_descriptors),
                    tuple(
                        descriptor
                        for descriptor in owned_before
                        if descriptor != status_write_descriptor
                    ),
                )
        finally:
            monitor.terminate()
            monitor.wait()

    def test_child_identity_rejects_an_exited_monitor(self) -> None:
        monitor = subprocess.Popen(
            ("/usr/bin/true",),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        monitor.wait()
        with self.build() as plan:
            status = json.dumps({"child-pid": os.getpid()}).encode("ascii")
            os.write(plan._status_write_descriptor, status + b"\n")
            with self.assertRaises(ModelSessionError) as caught:
                plan.sandbox_child_pid(monitor)
            self.assertEqual(caught.exception.code, "sandbox_launch_failed")
            self.assertIn("monitor exited", str(caught.exception))

    def test_rejects_an_inert_run_instead_of_a_lease(self) -> None:
        with self.assertRaises(ModelSessionError) as caught:
            build_sandbox_plan(
                self.fixture.run,
                command=("/usr/bin/python3", "-c", "pass"),
                attachment_runtime_root=self.fixture.attachment_runtime_root,
            )
        self.assertEqual(caught.exception.code, "session_lease_required")

    def test_rejects_missing_workspace_authority_mask_target(self) -> None:
        self.fixture.workspace_authority.rmdir()
        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "unsafe_sandbox_source")

    def test_uses_retained_source_after_canonical_path_replacement(self) -> None:
        alternate = _private_directory(self.fixture.root / "alternate-workspace")
        original = self.fixture.session_root / "workspace.original"
        self.fixture.workspace.rename(original)
        self.fixture.workspace.symlink_to(alternate, target_is_directory=True)
        with self.build() as plan:
            workspace_destination = plan.argv.index("/workspace")
            descriptor = int(plan.argv[workspace_destination - 1])
            self.assertEqual(
                (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino),
                (original.stat().st_dev, original.stat().st_ino),
            )

    def test_locked_resources_are_sealed_at_lease_acquisition(self) -> None:
        originals = {
            resource.relative_path: resource.path.read_bytes()
            for resource in self.fixture.run.resources
        }
        first_resource = self.fixture.run.resources[0]
        independent_reader = self.fixture.lease.duplicate_resource(
            first_resource.relative_path
        )
        try:
            self.assertTrue(os.read(independent_reader, 1))
        finally:
            os.close(independent_reader)
        for resource in self.fixture.run.resources:
            resource.path.write_bytes(b"replaced after lease acquisition\n")
            resource.path.chmod(0o600)

        with self.build() as plan:
            for relative_path, expected in originals.items():
                if relative_path.parts[0] == "profile":
                    destination = (
                        "/profile/" + "/".join(relative_path.parts[1:])
                    )
                elif relative_path.parts[0] == "runtime":
                    destination = (
                        "/runtime/" + "/".join(relative_path.parts[1:])
                    )
                else:
                    destination = "/config/models.json"
                destination_index = plan.argv.index(destination)
                self.assertEqual(
                    plan.argv[destination_index - 2],
                    "--ro-bind-data",
                )
                descriptor = int(plan.argv[destination_index - 1])
                self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), 0)
                self.assertEqual(
                    os.read(descriptor, len(expected) + 1),
                    expected,
                )

    def test_rejects_source_metadata_change_after_lease_acquisition(self) -> None:
        self.fixture.workspace.chmod(0o750)
        with self.assertRaises(ModelSessionError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "session_reference_changed")

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

    def test_run_authority_cannot_be_reassigned_or_forged(self) -> None:
        with self.assertRaises(AttributeError):
            self.fixture.lease.run = dataclasses.replace(
                self.fixture.lease.run,
                report_directory=self.fixture.memory,
            )
        with self.assertRaises(ModelSessionError) as caught:
            RunLease(
                run=self.fixture.run,
                root_descriptor=-1,
                receipt_descriptor=-1,
                sources={},
                source_identities={},
                resources={},
                resource_identities={},
                authority=object(),
            )
        self.assertEqual(caught.exception.code, "session_lease_required")

    def test_rejects_unlocked_or_noncanonical_entrypoint(self) -> None:
        for command in (
            ("/usr/bin/sh", "-c", "true"),
            ("/opt/pi/../../usr/bin/python3", "-c", "pass"),
        ):
            with self.subTest(command=command):
                with self.assertRaises(ModelSessionError):
                    self.build(command)

    def test_rejects_inference_socket_reachable_through_mounted_state(
        self,
    ) -> None:
        for root in (self.fixture.project, self.fixture.pi_sessions):
            with self.subTest(root=root):
                nested_socket = root / "nested.sock"
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
                    nested_socket.unlink()

    def test_rejects_socket_beneath_a_renamed_retained_mount(self) -> None:
        moved_project = self.fixture.root / "project.original"
        self.fixture.project.rename(moved_project)
        nested_socket = moved_project / "nested.sock"
        nested_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            nested_listener.bind(os.fspath(nested_socket))
            nested_listener.listen(1)
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
            self.assertIn("retained sandbox mount", str(caught.exception))
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

    def test_rejects_pi_installation_changed_after_lease_acquisition(self) -> None:
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
    def test_run_lock_is_held_for_the_real_child_lifetime(self) -> None:
        if not BWRAP_BINARY.exists():
            self.skipTest(f"{BWRAP_BINARY} is not installed")
        fixture = SandboxFixture()
        process: subprocess.Popen[str] | None = None
        script = (
            "import sys\n"
            "print('ready', flush=True)\n"
            "assert sys.stdin.readline() == 'release\\n'\n"
        )
        try:
            with build_sandbox_plan(
                fixture.lease,
                command=("/usr/bin/python3", "-c", script),
                attachment_runtime_root=fixture.attachment_runtime_root,
            ) as plan:
                process = subprocess.Popen(
                    plan.argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    pass_fds=plan.pass_fds,
                )
                child_pid = plan.sandbox_child_pid(process)
                os.kill(child_pid, 0)
                self.assertEqual(plan.sandbox_child_pid(process), child_pid)
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline(), "ready\n")
                with self.assertRaises(ModelSessionError) as caught:
                    acquire_run_from_state(
                        fixture.state_root,
                        "profile",
                        fixture.run.session_id,
                    )
                self.assertEqual(caught.exception.code, "session_in_use")
                stdout, stderr = process.communicate(input="release\n")
                self.assertEqual(
                    process.returncode,
                    0,
                    msg=f"stdout:\n{stdout}\nstderr:\n{stderr}",
                )
                process = None
            with acquire_run_from_state(
                fixture.state_root,
                "profile",
                fixture.run.session_id,
            ) as replacement:
                self.assertFalse(replacement.closed)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait()
            fixture.close()

    def test_real_boundary_exposes_only_intended_writable_surfaces(self) -> None:
        if not BWRAP_BINARY.exists():
            self.skipTest(f"{BWRAP_BINARY} is not installed")
        fixture = SandboxFixture()
        session_id = fixture.run.session_id
        script = f"""
import json
import os
import pathlib
import socket

assert pathlib.Path.cwd() == pathlib.Path("/workspace")
assert socket.gethostname() == "model-session"
assert pathlib.Path("/workspace/AGENTS.md").read_text() == "isolated workspace\\n"
assert not any(pathlib.Path("/workspace/.pi").iterdir())
assert pathlib.Path("/profile/AGENTS.md").read_text() == "isolated workspace\\n"
assert not pathlib.Path("/profile/lock.json").exists()
models = json.loads(pathlib.Path("/config/models.json").read_text())
assert "fixture" in models["providers"]
assert pathlib.Path("/runtime/relay.py").is_file()
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
    "/project/reports/{session_id}/written",
    "/project/memory/{session_id}/written",
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
    "/project/reports/{OTHER_SESSION_ID}/sealed.txt",
    "/project/memory/{OTHER_SESSION_ID}/sealed.txt",
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
                fixture.lease,
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
            self.assertFalse((fixture.workspace_authority / "written").exists())
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
            fixture.close()


if __name__ == "__main__":
    unittest.main()
