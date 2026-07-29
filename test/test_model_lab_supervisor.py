from __future__ import annotations

import datetime
import os
import pathlib
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from model_lab.configuration import parse_lab_toml
from model_lab.controller import ModelLabController, ServiceUse
from model_lab.errors import ModelLabError
from model_lab.lifecycle import (
    DeploymentStore,
    format_timestamp,
    parse_timestamp,
)
from model_lab.profile_binding import ProfileBindingStore
from model_lab.service_definition import parse_service_toml
from model_lab.supervisor import ModelLabSupervisor
from model_lab.supervisor_client import (
    PendingPiUse,
    PiLeaseChannel,
    SupervisorClient,
    _default_launcher,
    subprocess_model_session,
)
from model_lab.supervisor_protocol import (
    PI_PENDING_SCHEMA,
    SESSION_USE_ACCEPTED_SCHEMA,
    SESSION_USE_ADMIT_SCHEMA,
    SUPERVISOR_ERROR_SCHEMA,
    SUPERVISOR_REQUEST_SCHEMA,
    canonical_json_bytes,
    process_start_time,
    receive_document,
    receive_document_with_credentials,
    send_document,
)
from model_session.attachment import (
    ServiceEndpoint,
    ServiceEndpointBinding,
    ServiceWorkload,
)
from model_session.errors import ModelSessionError
from test_model_lab_core import (
    FakeProfile,
    FakeRuntime,
    NotReadyRuntime,
    QuarantinedHosts,
    lab_toml,
    service_toml,
)


FIXTURE_PROFILE_ID = "chat"
FIXTURE_PROJECT_ID = "playground"
FIXTURE_SERVICE_ID = "fixture-chat"
FIXTURE_SERVICE_SHA256 = "b" * 64
FIXTURE_WORKLOAD_SHA256 = "a" * 64
FIXTURE_MODALITIES = ("text",)


def fixture_pi_identity(
    *,
    project_id: str = FIXTURE_PROJECT_ID,
    service_sha256: str = FIXTURE_SERVICE_SHA256,
    workload_sha256: str = FIXTURE_WORKLOAD_SHA256,
    required_input_modalities: tuple[str, ...] = FIXTURE_MODALITIES,
    session_id: str | None = None,
) -> dict:
    return {
        "profile_id": FIXTURE_PROFILE_ID,
        "project_id": project_id,
        "service_id": FIXTURE_SERVICE_ID,
        "service_sha256": service_sha256,
        "workload_sha256": workload_sha256,
        "required_input_modalities": required_input_modalities,
        "session_id": session_id,
    }


class FakeDeployments:
    def __init__(self) -> None:
        self.transfers = []
        self.current = None

    def load(self, _service_id):
        return self.current

    def reconcile_orphaned_uses(self, *, idle_ttl_seconds):
        return ()

    def transfer_use_owner(
        self,
        service_id,
        lease_id,
        *,
        expected_owner_pid,
        expected_owner_start_time,
        owner_pid,
        owner_start_time,
        startup_deadline=None,
        monotonic=None,
    ):
        del startup_deadline, monotonic
        self.transfers.append(
            (
                service_id,
                lease_id,
                expected_owner_pid,
                expected_owner_start_time,
                owner_pid,
                owner_start_time,
            )
        )
        return SimpleNamespace(
            lease_id=lease_id,
            owner_pid=owner_pid,
            owner_start_time=owner_start_time,
        )

    def list(self):
        return ()


class FakeController:
    def __init__(self) -> None:
        self.lab = SimpleNamespace(
            lease=SimpleNamespace(
                service_idle_ttl_seconds=1800,
                renewal_ttl_seconds=120,
                startup_timeout_seconds=300,
            )
        )
        self.deployments = FakeDeployments()
        self.preparations = SimpleNamespace(list=lambda: ())
        self._clock_value = datetime.datetime.now(datetime.timezone.utc)
        self._monotonic_value = 1000.0
        self.startup_deadline_created = threading.Event()
        self.acquire_calls = 0
        self.acquisitions = 0
        self.acquisition_expirations = []
        self.acquisition_deadlines = []
        self.acquisition_profiles = []
        self.stop_on_release_requests = []
        self.releases = []
        self.release_event = threading.Event()
        self.fail_budget_after_acquisition = False
        self.active_mutations = 0
        self.maximum_mutations = 0

    def clock(self):
        return self._clock_value

    def monotonic(self):
        return self._monotonic_value

    def advance_startup_clock(self, seconds):
        self._clock_value += datetime.timedelta(seconds=seconds)
        self._monotonic_value += seconds

    def _startup_timeout_error(self):
        return ModelLabError(
            "service did not become ready within the configured "
            "300-second startup budget",
            code="service_startup_timeout",
        )

    def canonical_startup_expiration(self, expires_at):
        requested = parse_timestamp(
            expires_at,
            "fixture service startup expiration",
        )
        now = self.clock()
        expiration = min(
            requested,
            now + datetime.timedelta(seconds=300),
        )
        if expiration <= now:
            raise self._startup_timeout_error()
        return format_timestamp(expiration)

    def startup_deadline_from_expiration(
        self,
        expires_at,
        *,
        startup_deadline=None,
    ):
        expiration = parse_timestamp(
            expires_at,
            "fixture service startup expiration",
        )
        remaining_seconds = (expiration - self.clock()).total_seconds()
        if remaining_seconds <= 0:
            raise self._startup_timeout_error()
        self.startup_deadline_created.set()
        wall_deadline = self.monotonic() + min(300.0, remaining_seconds)
        if startup_deadline is None:
            return wall_deadline
        return min(startup_deadline, wall_deadline)

    def require_startup_budget(self, deadline):
        if (
            self.fail_budget_after_acquisition
            and self.acquisitions > 0
        ) or self.monotonic() >= deadline:
            raise self._startup_timeout_error()

    def acquire_for_profile(
        self,
        route,
        service,
        *,
        host_name,
        owner_pid,
        owner_start_time,
        startup_expires_at,
        startup_deadline,
        stop_on_release,
    ):
        self.acquire_calls += 1
        expiration = parse_timestamp(
            startup_expires_at,
            "fixture service startup expiration",
        )
        if expiration <= self.clock():
            raise self._startup_timeout_error()
        self.active_mutations += 1
        self.maximum_mutations = max(
            self.maximum_mutations,
            self.active_mutations,
        )
        time.sleep(0.01)
        self.acquisitions += 1
        self.acquisition_profiles.append(route)
        self.acquisition_expirations.append(startup_expires_at)
        self.acquisition_deadlines.append(startup_deadline)
        self.stop_on_release_requests.append(stop_on_release)
        self.active_mutations -= 1
        return ServiceUse(
            deployment=SimpleNamespace(
                service_id=service.service_id,
                deployment_id="deployment-one",
                workload_sha256="a" * 64,
            ),
            endpoint=SimpleNamespace(),
            lease=SimpleNamespace(
                lease_id=f"use-{self.acquisitions}",
                owner_pid=owner_pid,
                owner_start_time=owner_start_time,
            ),
        )

    def release_profile_use(
        self,
        service,
        use,
        *,
        now,
        stop_if_final=False,
    ):
        self.releases.append(
            (
                service.service_id,
                use.lease.lease_id,
                now,
                stop_if_final,
            )
        )
        self.release_event.set()

    def release_expired_pending_uses(self, _service):
        return False

    @staticmethod
    def is_claim_quarantined(error):
        return error.code == "host_claim_quarantined"


class SupervisorLauncherEnvironmentTest(unittest.TestCase):
    def test_default_launcher_passes_only_exact_resolved_paths(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        authored = root / "authored"
        state = root / "model-state"
        runtime = root / "runtime" / "model-lab"
        account_home = root / "account-home"
        runpod_authored = root / "runpod-authored"
        runpod_state = root / "runpod-state"
        runpod_config = root / "runpod-config"
        runpod_credential = runpod_config / "api-key"
        huggingface_token = root / "huggingface" / "token"
        inherited_secret = "must-not-enter-supervisor"
        source_environment = {
            "HOME": str(root / "untrusted-home"),
            "PATH": str(root / "untrusted-bin"),
            "RUNPOD_ROOT": str(runpod_authored),
            "RUNPOD_STATE_HOME": str(runpod_state),
            "RUNPOD_CONFIG_HOME": str(runpod_config),
            "RUNPOD_CREDENTIALS_FILE": str(runpod_credential),
            "HF_TOKEN_PATH": str(huggingface_token),
            "RUNPOD_API_KEY": inherited_secret,
            "HF_TOKEN": inherited_secret,
            "HUGGING_FACE_HUB_TOKEN": inherited_secret,
            "SSH_AUTH_SOCK": str(root / inherited_secret),
            "AWS_SECRET_ACCESS_KEY": inherited_secret,
            "OPENAI_API_KEY": inherited_secret,
        }
        launched: dict[str, object] = {}
        process = SimpleNamespace(poll=lambda: None)

        def popen(arguments, **keywords):
            launched["arguments"] = arguments
            launched["keywords"] = keywords
            log_metadata = os.fstat(keywords["stdout"])
            self.assertTrue(stat.S_ISREG(log_metadata.st_mode))
            self.assertEqual(stat.S_IMODE(log_metadata.st_mode), 0o600)
            self.assertEqual(keywords["stderr"], keywords["stdout"])
            return process

        with (
            mock.patch.dict(os.environ, source_environment, clear=True),
            mock.patch(
                "model_lab.supervisor_client.pwd.getpwuid",
                return_value=SimpleNamespace(pw_dir=str(account_home)),
            ),
            mock.patch(
                "model_lab.supervisor_client.subprocess.Popen",
                side_effect=popen,
            ),
        ):
            result = _default_launcher(authored, state, runtime)

        self.assertIs(result, process)
        self.assertEqual(
            launched["arguments"],
            [
                sys.executable,
                "-I",
                "-B",
                str(
                    pathlib.Path(__file__).resolve().parents[1]
                    / "bin"
                    / "model-lab-supervisor"
                ),
                "--root",
                str(authored),
                "--state-root",
                str(state),
                "--runtime-root",
                str(runtime),
            ],
        )
        keywords = launched["keywords"]
        self.assertEqual(
            keywords["env"],
            {
                "HOME": str(account_home),
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "XDG_RUNTIME_DIR": str(runtime.parent),
                "RUNPOD_ROOT": str(runpod_authored),
                "RUNPOD_STATE_HOME": str(runpod_state),
                "RUNPOD_CONFIG_HOME": str(runpod_config),
                "RUNPOD_CREDENTIALS_FILE": str(runpod_credential),
                "HF_TOKEN_PATH": str(huggingface_token),
            },
        )
        self.assertNotIn(
            inherited_secret,
            "\0".join(keywords["env"].values()),
        )
        self.assertEqual(keywords["stdin"], subprocess.DEVNULL)
        self.assertTrue(keywords["close_fds"])
        self.assertTrue(keywords["start_new_session"])
        self.assertEqual(stat.S_IMODE((state / "supervisor.log").stat().st_mode), 0o600)


class SupervisorClientStartupBudgetTest(unittest.TestCase):
    def test_connect_exhaustion_is_a_service_startup_timeout(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        current = [0.0]

        def monotonic() -> float:
            current[0] += 1.0
            return current[0]

        client = SupervisorClient(
            authored_root=root / "authored",
            state_root=root / "state",
            runtime_root=root / "runtime",
            launcher=lambda *_arguments: None,
            monotonic=monotonic,
        )
        with (
            mock.patch.object(
                client,
                "_connect_once",
                side_effect=FileNotFoundError("controlled absence"),
            ),
            mock.patch("model_lab.supervisor_client.time.sleep"),
            self.assertRaises(ModelLabError) as caught,
        ):
            client.connect(deadline=3.0)

        self.assertEqual(caught.exception.code, "service_startup_timeout")

    def test_connect_translates_transport_timeout_to_startup_timeout(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        client = SupervisorClient(
            authored_root=root / "authored",
            state_root=root / "state",
            runtime_root=root / "runtime",
        )

        with (
            mock.patch.object(
                client,
                "_connect_once",
                side_effect=TimeoutError("controlled transport timeout"),
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            client.connect(deadline=time.monotonic() + 300)

        self.assertEqual(caught.exception.code, "service_startup_timeout")

    def test_slow_byte_stream_cannot_reset_absolute_receive_deadline(
        self,
    ) -> None:
        current = [0.0]

        class SlowSocket:
            def __init__(self) -> None:
                self.payload = bytearray(b'{"schema":"fixture"}\n')
                self.receives = 0

            def settimeout(self, _timeout) -> None:
                return None

            def recv(self, _size) -> bytes:
                self.receives += 1
                current[0] += 1.0
                return bytes([self.payload.pop(0)])

        connection = SlowSocket()

        with self.assertRaises(ModelLabError) as caught:
            receive_document(
                connection,
                deadline=2.5,
                monotonic=lambda: current[0],
                deadline_error_code="service_startup_timeout",
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(connection.receives, 3)

    def test_slow_credential_frame_cannot_reset_absolute_receive_deadline(
        self,
    ) -> None:
        current = [0.0]
        credentials = struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

        class SlowCredentialSocket:
            def __init__(self) -> None:
                self.payload = bytearray(b'{"schema":"fixture"}\n')
                self.receives = 0
                self.timeouts = []

            def settimeout(self, timeout) -> None:
                self.timeouts.append(timeout)

            def recvmsg(self, _size, _ancillary_size):
                self.receives += 1
                current[0] += 1.0
                return (
                    bytes([self.payload.pop(0)]),
                    [
                        (
                            socket.SOL_SOCKET,
                            socket.SCM_CREDENTIALS,
                            credentials,
                        )
                    ],
                    0,
                    None,
                )

        connection = SlowCredentialSocket()

        with self.assertRaises(ModelLabError) as caught:
            receive_document_with_credentials(
                connection,
                deadline=2.5,
                monotonic=lambda: current[0],
                deadline_error_code="service_startup_timeout",
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(connection.receives, 3)
        self.assertEqual(connection.timeouts, [2.5, 1.5, 0.5])

    def test_autostart_uses_one_deadline_and_returns_a_blocking_channel(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(temporary.name)
        client_socket, server_socket = socket.socketpair()
        wall_clock = [
            datetime.datetime(
                2026,
                7,
                29,
                12,
                0,
                tzinfo=datetime.timezone.utc,
            )
        ]
        monotonic_clock = [100.0]
        initial_wall_clock = wall_clock[0]
        timeout_values = []
        launcher_observations = []
        requests = []
        server_errors = []

        class RecordingConnection:
            def __init__(self, connection):
                self.connection = connection

            def settimeout(self, value):
                timeout_values.append(value)
                self.connection.settimeout(value)

            def gettimeout(self):
                return self.connection.gettimeout()

            def __getattr__(self, name):
                return getattr(self.connection, name)

        recording_connection = RecordingConnection(client_socket)

        def clock():
            return wall_clock[0]

        def monotonic():
            return monotonic_clock[0]

        def launcher(*_roots):
            launcher_observations.append(
                (wall_clock[0], monotonic_clock[0])
            )
            wall_clock[0] += datetime.timedelta(seconds=25)
            monotonic_clock[0] += 25
            return None

        def serve_pending_use():
            try:
                request = receive_document(server_socket)
                requests.append(request)
                send_document(
                    server_socket,
                    {
                        "schema": PI_PENDING_SCHEMA,
                        **fixture_pi_identity(),
                        "deployment_id": "deployment-one",
                        "use_lease_id": "use-one",
                    },
                )
            except BaseException as error:
                server_errors.append(error)
            finally:
                server_socket.close()

        server_thread = threading.Thread(
            target=serve_pending_use,
            daemon=True,
        )
        server_thread.start()
        client = SupervisorClient(
            authored_root=root / "authored",
            state_root=root / "state",
            runtime_root=root / "runtime",
            launcher=launcher,
            clock=clock,
            monotonic=monotonic,
        )
        unavailable = ModelLabError(
            "controlled absent supervisor",
            code="supervisor_unavailable",
        )
        channel = None
        try:
            with mock.patch.object(
                client,
                "_connect_once",
                side_effect=[unavailable, recording_connection],
            ):
                channel = client.acquire_pi(
                    **fixture_pi_identity(),
                    host_name=None,
                    stop_on_release=False,
                    startup_timeout_seconds=300,
                )

            self.assertEqual(
                launcher_observations,
                [(initial_wall_clock, 100.0)],
            )
            self.assertGreater(len(timeout_values), 2)
            self.assertTrue(
                all(value == 275.0 for value in timeout_values[:-1])
            )
            self.assertIsNone(timeout_values[-1])
            self.assertIsNone(channel.connection.gettimeout())
            self.assertEqual(
                requests,
                [
                    {
                        "schema": SUPERVISOR_REQUEST_SCHEMA,
                        "operation": "pi-acquire",
                        **{
                            **fixture_pi_identity(),
                            "required_input_modalities": ["text"],
                        },
                        "host_name": None,
                        "stop_on_release": False,
                        "startup_expires_at": (
                            "2026-07-29T12:05:00Z"
                        ),
                        "startup_deadline": 400.0,
                    }
                ],
            )
        finally:
            if channel is not None:
                channel.close()
            else:
                client_socket.close()
            server_thread.join(2)
            server_socket.close()
            temporary.cleanup()
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(server_errors, [])

    def test_pending_grant_must_match_the_complete_requested_identity(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        client_socket, server_socket = socket.socketpair()
        server_errors = []

        def serve_changed_grant() -> None:
            try:
                receive_document(server_socket)
                send_document(
                    server_socket,
                    {
                        "schema": PI_PENDING_SCHEMA,
                        **fixture_pi_identity(
                            service_sha256="c" * 64,
                        ),
                        "deployment_id": "deployment-one",
                        "use_lease_id": "use-one",
                    },
                )
            except BaseException as error:
                server_errors.append(error)
            finally:
                server_socket.close()

        server_thread = threading.Thread(
            target=serve_changed_grant,
            daemon=True,
        )
        server_thread.start()
        client = SupervisorClient(
            authored_root=root / "authored",
            state_root=root / "state",
            runtime_root=root / "runtime",
        )
        with (
            mock.patch.object(
                client,
                "_connect_once",
                return_value=client_socket,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            client.acquire_pi(
                **fixture_pi_identity(),
                host_name=None,
                stop_on_release=False,
            )

        server_thread.join(2)
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(server_errors, [])
        self.assertEqual(caught.exception.code, "invalid_supervisor_protocol")


class SupervisorLeaseChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.authored = root / "authored"
        self.state = root / "state"
        self.runtime = root / "runtime"
        for path in (self.authored, self.state):
            path.mkdir(mode=0o700)
        self.route = SimpleNamespace(
            profile_id="chat",
            project_id="playground",
            service_id="fixture-chat",
            required_input_modalities=("text",),
        )
        self.service = SimpleNamespace(
            service_id="fixture-chat",
            service_sha256=FIXTURE_SERVICE_SHA256,
            workload_sha256="a" * 64,
            endpoint=SimpleNamespace(input_modalities=("text", "image")),
        )
        self.profile = SimpleNamespace(
            contract=SimpleNamespace(
                profile_id=self.route.profile_id,
                project_id=self.route.project_id,
                service_id=self.route.service_id,
                endpoint=SimpleNamespace(
                    required_input_modalities=(
                        self.route.required_input_modalities
                    )
                ),
            ),
        )
        self.controller = FakeController()
        self.supervisor = ModelLabSupervisor(
            controller=self.controller,
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime,
            maintenance_interval_seconds=3600,
        )
        self.supervisor.deployed_services.publish = lambda service: None
        self.patches = (
            mock.patch(
                "model_lab.supervisor.load_profile_route",
                return_value=self.route,
            ),
            mock.patch(
                "model_lab.supervisor.load_service_id",
                return_value=self.service,
            ),
            mock.patch(
                "model_lab.supervisor.load_profile",
                return_value=self.profile,
            ),
        )
        for patch in self.patches:
            patch.start()
        self.thread = threading.Thread(
            target=self.supervisor.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.assertTrue(self.supervisor.ready_event.wait(2))
        self.client = SupervisorClient(
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime,
            launcher=lambda *_: self.fail("running supervisor must be reused"),
            clock=self.controller.clock,
        )

    def _acquire_pi(
        self,
        *,
        profile_id: str = FIXTURE_PROFILE_ID,
        host_name: str | None = None,
        stop_on_release: bool = False,
        startup_timeout_seconds: int = 300,
    ) -> PiLeaseChannel:
        self.assertEqual(profile_id, FIXTURE_PROFILE_ID)
        return self.client.acquire_pi(
            **fixture_pi_identity(),
            host_name=host_name,
            stop_on_release=stop_on_release,
            startup_timeout_seconds=startup_timeout_seconds,
        )

    def tearDown(self) -> None:
        self.supervisor.stop()
        self.thread.join(2)
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def _admit(self, channel: PiLeaseChannel) -> dict:
        send_document(
            channel.connection,
            {
                "schema": SESSION_USE_ADMIT_SCHEMA,
                "profile_id": "chat",
                "service_id": "fixture-chat",
                "pid": os.getpid(),
                "start_time": process_start_time(os.getpid()),
            },
        )
        return receive_document(channel.connection)

    def test_route_drift_is_rejected_before_capacity_acquisition(self) -> None:
        changed = SimpleNamespace(
            profile_id="chat",
            project_id="different-project",
            service_id="fixture-chat",
            required_input_modalities=("text",),
        )
        with (
            mock.patch(
                "model_lab.supervisor.load_profile_route",
                return_value=changed,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self._acquire_pi()

        self.assertEqual(caught.exception.code, "pi_identity_changed")
        self.assertEqual(self.controller.acquire_calls, 0)

    def test_service_drift_is_rejected_before_capacity_acquisition(self) -> None:
        with self.assertRaises(ModelLabError) as caught:
            self.client.acquire_pi(
                **fixture_pi_identity(service_sha256="c" * 64),
                host_name=None,
                stop_on_release=False,
            )

        self.assertEqual(caught.exception.code, "pi_identity_changed")
        self.assertEqual(self.controller.acquire_calls, 0)

    def test_invalid_new_profile_is_rejected_before_capacity_acquisition(
        self,
    ) -> None:
        with (
            mock.patch(
                "model_lab.supervisor.load_profile",
                side_effect=ModelSessionError(
                    "controlled invalid profile",
                    code="invalid_profile",
                ),
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self._acquire_pi()

        self.assertEqual(caught.exception.code, "invalid_profile")
        self.assertEqual(self.controller.acquire_calls, 0)

    def test_resume_revalidation_uses_frozen_modalities(self) -> None:
        resumed = SimpleNamespace(
            session_id="session-one",
            service_id="fixture-chat",
            workload_sha256=FIXTURE_WORKLOAD_SHA256,
            input_modalities=("text", "image"),
        )
        with mock.patch(
            "model_lab.supervisor.resolve_resume_selection",
            return_value=resumed,
        ):
            channel = self.client.acquire_pi(
                **fixture_pi_identity(
                    required_input_modalities=("image", "text"),
                    session_id="session-one",
                ),
                host_name=None,
                stop_on_release=False,
            )

        self.assertEqual(
            self.controller.acquisition_profiles[-1].required_input_modalities,
            ("image", "text"),
        )
        channel.close()
        self.assertTrue(self.controller.release_event.wait(2))

    def test_missing_resume_is_rejected_before_capacity_acquisition(self) -> None:
        with (
            mock.patch(
                "model_lab.supervisor.resolve_resume_selection",
                side_effect=ModelSessionError(
                    "controlled missing session",
                    code="session_not_found",
                ),
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.client.acquire_pi(
                **fixture_pi_identity(session_id="session-one"),
                host_name=None,
                stop_on_release=False,
            )

        self.assertEqual(caught.exception.code, "session_not_found")
        self.assertEqual(self.controller.acquire_calls, 0)

    def test_same_connected_stream_becomes_held_use_lease(self) -> None:
        channel = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        accepted = self._admit(channel)

        self.assertEqual(
            accepted["schema"],
            SESSION_USE_ACCEPTED_SCHEMA,
            accepted,
        )
        self.assertEqual(accepted["use_lease_id"], "use-1")
        self.assertEqual(accepted["session_pid"], os.getpid())
        self.assertEqual(
            accepted["supervisor_pid"],
            self.supervisor._supervisor_pid,
        )
        self.assertEqual(len(self.controller.deployments.transfers), 1)
        self.assertEqual(self.controller.releases, [])

        channel.close()
        deadline = time.monotonic() + 2
        while not self.controller.releases and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(
            self.controller.releases,
            [("fixture-chat", "use-1", False, False)],
        )

    def test_immediate_release_is_bound_during_use_acquisition(self) -> None:
        channel = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=True,
        )
        self._admit(channel)

        self.assertEqual(self.controller.stop_on_release_requests, [True])

        channel.close()
        self.assertTrue(self.controller.release_event.wait(2))
        self.assertEqual(
            self.controller.releases,
            [("fixture-chat", "use-1", True, False)],
        )

    def test_unadmitted_channel_is_final_released_immediately(self) -> None:
        channel = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        channel.close()

        self.assertTrue(self.controller.release_event.wait(2))
        self.assertEqual(
            self.controller.releases,
            [("fixture-chat", "use-1", False, True)],
        )

    def test_failed_acquisition_after_use_creation_is_final_released_now(
        self,
    ) -> None:
        self.controller.fail_budget_after_acquisition = True

        with self.assertRaises(ModelLabError) as caught:
            self._acquire_pi(
                profile_id="chat",
                host_name=None,
                stop_on_release=False,
            )

        self.assertEqual(
            caught.exception.code,
            "service_startup_timeout",
        )
        self.assertTrue(self.controller.release_event.wait(2))
        self.assertEqual(
            self.controller.releases,
            [("fixture-chat", "use-1", False, True)],
        )

    def test_expired_queued_request_never_completes_acquisition(self) -> None:
        connection = self.client.connect()
        startup_expires_at = format_timestamp(
            self.controller.clock() + datetime.timedelta(seconds=1)
        )
        try:
            with self.supervisor._service_mutation("fixture-chat"):
                send_document(
                    connection,
                    {
                        "schema": SUPERVISOR_REQUEST_SCHEMA,
                        "operation": "pi-acquire",
                        **fixture_pi_identity(),
                        "host_name": None,
                        "stop_on_release": False,
                        "startup_expires_at": startup_expires_at,
                        "startup_deadline": (
                            self.controller.monotonic() + 1
                        ),
                    },
                )
                self.assertTrue(
                    self.controller.startup_deadline_created.wait(2)
                )
                self.controller.advance_startup_clock(2)

            response = receive_document(connection)
        finally:
            connection.close()

        self.assertEqual(response["schema"], SUPERVISOR_ERROR_SCHEMA)
        self.assertEqual(response["code"], "service_startup_timeout")
        self.assertEqual(self.controller.acquire_calls, 0)
        self.assertEqual(self.controller.acquisitions, 0)
        self.assertEqual(self.controller.releases, [])

    def test_service_queue_wait_obeys_the_startup_deadline(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def hold_service() -> None:
            with self.supervisor._service_mutation("fixture-chat"):
                entered.set()
                release.wait()

        holder = threading.Thread(target=hold_service)
        holder.start()
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(ModelLabError) as caught:
                with self.supervisor._service_mutation(
                    "fixture-chat",
                    deadline=self.controller.monotonic() + 0.01,
                ):
                    self.fail("expired service mutation must not begin")
        finally:
            release.set()
            holder.join(2)

        self.assertFalse(holder.is_alive())
        self.assertEqual(caught.exception.code, "service_startup_timeout")

    def test_slow_pi_admission_cannot_outlive_startup_deadline(self) -> None:
        credentials = struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

        class SlowAdmissionSocket:
            def __init__(self, controller) -> None:
                self.controller = controller
                self.payload = bytearray(b'{"schema":"never-completes"}\n')
                self.receives = 0
                self.sent = bytearray()
                self.timeouts = []

            def __hash__(self):
                return id(self)

            def setsockopt(self, _level, _kind, _value) -> None:
                return None

            def settimeout(self, timeout) -> None:
                self.timeouts.append(timeout)

            def send(self, payload) -> int:
                self.sent.extend(payload)
                return len(payload)

            def recvmsg(self, _size, _ancillary_size):
                self.receives += 1
                self.controller.advance_startup_clock(101)
                return (
                    bytes([self.payload.pop(0)]),
                    [
                        (
                            socket.SOL_SOCKET,
                            socket.SCM_CREDENTIALS,
                            credentials,
                        )
                    ],
                    0,
                    None,
                )

        connection = SlowAdmissionSocket(self.controller)
        startup_expires_at = format_timestamp(
            self.controller.clock() + datetime.timedelta(seconds=300)
        )

        with self.assertRaises(ModelLabError) as caught:
            self.supervisor._serve_pi(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": "pi-acquire",
                    **fixture_pi_identity(),
                    "host_name": None,
                    "stop_on_release": False,
                    "startup_expires_at": startup_expires_at,
                    "startup_deadline": (
                        self.controller.monotonic() + 300
                    ),
                },
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(connection.receives, 1)
        self.assertEqual(
            connection.timeouts,
            [300.0, 60.0],
        )
        self.assertEqual(self.controller.deployments.transfers, [])
        self.assertEqual(
            self.controller.releases,
            [("fixture-chat", "use-1", False, True)],
        )

    def test_blocked_pending_grant_releases_new_use_at_startup_deadline(
        self,
    ) -> None:
        class BlockedPendingSocket:
            def __init__(self, controller) -> None:
                self.controller = controller
                self.send_calls = 0
                self.timeouts = []

            def __hash__(self):
                return id(self)

            def setsockopt(self, _level, _kind, _value) -> None:
                return None

            def settimeout(self, timeout) -> None:
                self.timeouts.append(timeout)

            def send(self, _payload) -> int:
                self.send_calls += 1
                self.controller.advance_startup_clock(301)
                raise TimeoutError("controlled non-reading peer")

        connection = BlockedPendingSocket(self.controller)
        startup_expires_at = format_timestamp(
            self.controller.clock() + datetime.timedelta(seconds=300)
        )

        with self.assertRaises(ModelLabError) as caught:
            self.supervisor._serve_pi(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": "pi-acquire",
                    **fixture_pi_identity(),
                    "host_name": None,
                    "stop_on_release": False,
                    "startup_expires_at": startup_expires_at,
                    "startup_deadline": (
                        self.controller.monotonic() + 300
                    ),
                },
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(connection.send_calls, 1)
        self.assertEqual(connection.timeouts, [300.0])
        self.assertEqual(self.controller.deployments.transfers, [])
        self.assertEqual(
            self.controller.releases,
            [("fixture-chat", "use-1", False, True)],
        )

    def test_blocked_acceptance_releases_transferred_use_at_deadline(
        self,
    ) -> None:
        session_start_time = process_start_time(os.getpid())
        credentials = struct.pack("3i", os.getpid(), os.getuid(), os.getgid())
        admission = canonical_json_bytes(
            {
                "schema": SESSION_USE_ADMIT_SCHEMA,
                "profile_id": "chat",
                "service_id": "fixture-chat",
                "pid": os.getpid(),
                "start_time": session_start_time,
            }
        )

        class BlockedAcceptanceSocket:
            def __init__(self, controller) -> None:
                self.controller = controller
                self.admission = bytearray(admission)
                self.send_calls = 0

            def __hash__(self):
                return id(self)

            def setsockopt(self, _level, _kind, _value) -> None:
                return None

            def settimeout(self, _timeout) -> None:
                return None

            def send(self, payload) -> int:
                self.send_calls += 1
                if self.send_calls == 2:
                    self.controller.advance_startup_clock(301)
                    raise TimeoutError("controlled non-reading peer")
                return len(payload)

            def recvmsg(self, _size, _ancillary_size):
                return (
                    bytes([self.admission.pop(0)]),
                    [
                        (
                            socket.SOL_SOCKET,
                            socket.SCM_CREDENTIALS,
                            credentials,
                        )
                    ],
                    0,
                    None,
                )

        connection = BlockedAcceptanceSocket(self.controller)
        startup_expires_at = format_timestamp(
            self.controller.clock() + datetime.timedelta(seconds=300)
        )

        with self.assertRaises(ModelLabError) as caught:
            self.supervisor._serve_pi(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": "pi-acquire",
                    **fixture_pi_identity(),
                    "host_name": None,
                    "stop_on_release": False,
                    "startup_expires_at": startup_expires_at,
                    "startup_deadline": (
                        self.controller.monotonic() + 300
                    ),
                },
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(connection.send_calls, 2)
        self.assertEqual(len(self.controller.deployments.transfers), 1)
        self.assertEqual(
            self.controller.releases,
            [("fixture-chat", "use-1", False, True)],
        )

    def test_two_clients_are_serialized_and_each_owns_one_channel(self) -> None:
        channels = []
        barrier = threading.Barrier(3)

        def acquire() -> None:
            barrier.wait()
            channel = self._acquire_pi(
                profile_id="chat",
                host_name=None,
                stop_on_release=False,
            )
            channels.append(channel)

        threads = [threading.Thread(target=acquire) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(channels), 2)
        self.assertEqual(self.controller.acquisitions, 2)
        self.assertEqual(self.controller.maximum_mutations, 1)

        for channel in channels:
            self._admit(channel)
        for channel in channels:
            channel.close()

    def test_up_serializes_a_real_service_endpoint(self) -> None:
        published_at = datetime.datetime(
            2026,
            7,
            28,
            12,
            0,
            tzinfo=datetime.timezone.utc,
        )
        workload = ServiceWorkload(
            repository="fixture/model",
            revision="c" * 40,
            provider="runpod-vllm",
            model_id="fixture-chat",
            context_tokens=32768,
            max_output_tokens=4096,
            weight_format="native",
            kv_cache_dtype="bf16",
            runtime_compatibility="fixture-runtime",
            reasoning=False,
        )
        endpoint = ServiceEndpoint(
            publication_id="d" * 32,
            binding=ServiceEndpointBinding(
                service_id="fixture-chat",
                service_sha256="e" * 64,
                workload=workload,
                workload_sha256="f" * 64,
                input_modalities=("image", "text"),
            ),
            socket_path=self.runtime / "services" / "fixture-chat.sock",
            socket_device=31,
            socket_inode=47,
            published_at=published_at,
            admission_expires_at=published_at + datetime.timedelta(seconds=120),
            receipt_path=self.runtime / "services" / "fixture-chat.json",
        )
        deployment = SimpleNamespace(
            deployment_id="deployment-one",
            phase="ready",
            normalized=lambda: {
                "service_id": "fixture-chat",
                "host_name": "host-one",
                "idle_deadline": "2026-07-28T12:30:00Z",
            }
        )
        self.controller.ensure_ready = lambda *_args, **_kwargs: (
            deployment,
            endpoint,
        )
        self.controller.down = lambda *_args, **_kwargs: deployment

        result = self.client.request_up(
            service_id="fixture-chat",
            host_name=None,
            startup_timeout_seconds=300,
        )

        self.assertEqual(result["deployment"], deployment.normalized())
        self.assertEqual(
            result["endpoint"],
            {
                "publication_id": "d" * 32,
                "binding": {
                    "service_id": "fixture-chat",
                    "service_sha256": "e" * 64,
                    "workload": workload.as_dict(),
                    "workload_sha256": "f" * 64,
                    "input_modalities": ["image", "text"],
                },
                "socket_path": str(self.runtime / "services" / "fixture-chat.sock"),
                "socket_device": 31,
                "socket_inode": 47,
                "published_at": "2026-07-28T12:00:00.000000Z",
                "admission_expires_at": "2026-07-28T12:02:00.000000Z",
                "receipt_path": str(self.runtime / "services" / "fixture-chat.json"),
            },
        )

    def test_down_resolves_snapshot_after_service_mutation_lock(self) -> None:
        old_service = SimpleNamespace(
            service_id="fixture-chat",
            service_sha256="a" * 64,
        )
        replacement_service = SimpleNamespace(
            service_id="fixture-chat",
            service_sha256="b" * 64,
        )
        self.controller.deployments.current = SimpleNamespace(
            service_sha256=old_service.service_sha256,
            phase="ready",
        )
        loaded_snapshots = []

        def load_snapshot(service_id, service_sha256):
            loaded_snapshots.append((service_id, service_sha256))
            return {
                old_service.service_sha256: old_service,
                replacement_service.service_sha256: replacement_service,
            }[service_sha256]

        self.supervisor.deployed_services.load = load_snapshot
        mutation_entered = threading.Barrier(2)
        allow_mutation = threading.Barrier(2)

        class BlockedServiceMutation:
            def __enter__(self):
                mutation_entered.wait(2)
                allow_mutation.wait(2)

            @staticmethod
            def __exit__(_exception_type, _exception, _traceback):
                return False

        cleaned_services = []

        def down(service, *, now):
            cleaned_services.append((service, now))
            return SimpleNamespace(
                normalized=lambda: {
                    "service_id": service.service_id,
                    "service_sha256": service.service_sha256,
                }
            )

        self.controller.down = down

        class AcceptedResultSocket:
            @staticmethod
            def settimeout(_timeout) -> None:
                return None

            @staticmethod
            def send(payload) -> int:
                return len(payload)

        errors = []

        def serve_down() -> None:
            try:
                self.supervisor._serve_down(
                    AcceptedResultSocket(),
                    {
                        "schema": SUPERVISOR_REQUEST_SCHEMA,
                        "operation": "down",
                        "service_id": "fixture-chat",
                        "now": True,
                    },
                )
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(
            self.supervisor,
            "_service_mutation",
            side_effect=lambda _service_id: BlockedServiceMutation(),
        ):
            worker = threading.Thread(target=serve_down)
            worker.start()
            mutation_entered.wait(2)
            self.controller.deployments.current = SimpleNamespace(
                service_sha256=replacement_service.service_sha256,
                phase="ready",
            )
            allow_mutation.wait(2)
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            loaded_snapshots,
            [("fixture-chat", replacement_service.service_sha256)],
        )
        self.assertEqual(cleaned_services, [(replacement_service, True)])

    def test_blocked_up_result_stops_only_its_new_deployment(self) -> None:
        down_modes = []

        def deployment(phase):
            return SimpleNamespace(
                service_id="fixture-chat",
                deployment_id="deployment-one",
                phase=phase,
                use_leases=(),
                normalized=lambda: {
                    "service_id": "fixture-chat",
                    "deployment_id": "deployment-one",
                    "phase": phase,
                },
            )

        endpoint = SimpleNamespace(
            as_dict=lambda: {"publication_id": "publication-one"}
        )

        def ensure_ready(*_args, **_kwargs):
            ready = deployment("ready")
            self.controller.deployments.current = ready
            return ready, endpoint

        def down(
            _service,
            *,
            now,
            cleanup_deadline=None,
            deadline_error_code="service_cleanup_required",
        ):
            del cleanup_deadline, deadline_error_code
            down_modes.append(now)
            current = deployment("released" if now else "idle")
            self.controller.deployments.current = current
            return current

        class BlockedResultSocket:
            def __init__(self, controller) -> None:
                self.controller = controller
                self.timeouts = []

            def settimeout(self, timeout) -> None:
                self.timeouts.append(timeout)

            def send(self, _payload) -> int:
                self.controller.advance_startup_clock(301)
                raise TimeoutError("controlled non-reading peer")

        self.controller.ensure_ready = ensure_ready
        self.controller.down = down
        connection = BlockedResultSocket(self.controller)
        startup_expires_at = format_timestamp(
            self.controller.clock() + datetime.timedelta(seconds=300)
        )

        with self.assertRaises(ModelLabError) as caught:
            self.supervisor._serve_up(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": "up",
                    "service_id": "fixture-chat",
                    "host_name": None,
                    "startup_expires_at": startup_expires_at,
                    "startup_deadline": (
                        self.controller.monotonic() + 300
                    ),
                },
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(connection.timeouts, [300.0])
        self.assertEqual(down_modes, [False, True])
        self.assertEqual(self.controller.deployments.current.phase, "released")

    def test_blocked_up_result_preserves_reused_deployment(self) -> None:
        down_modes = []

        def deployment(phase):
            return SimpleNamespace(
                service_id="fixture-chat",
                deployment_id="deployment-one",
                phase=phase,
                use_leases=(),
                normalized=lambda: {
                    "service_id": "fixture-chat",
                    "deployment_id": "deployment-one",
                    "phase": phase,
                },
            )

        ready = deployment("ready")
        self.controller.deployments.current = ready
        endpoint = SimpleNamespace(
            as_dict=lambda: {"publication_id": "publication-one"}
        )

        def ensure_ready(*_args, **_kwargs):
            return self.controller.deployments.current, endpoint

        def down(
            _service,
            *,
            now,
            cleanup_deadline=None,
            deadline_error_code="service_cleanup_required",
        ):
            del cleanup_deadline, deadline_error_code
            down_modes.append(now)
            current = deployment("released" if now else "idle")
            self.controller.deployments.current = current
            return current

        class BlockedResultSocket:
            def __init__(self, controller) -> None:
                self.controller = controller

            def settimeout(self, _timeout) -> None:
                return None

            def send(self, _payload) -> int:
                self.controller.advance_startup_clock(301)
                raise TimeoutError("controlled non-reading peer")

        self.controller.ensure_ready = ensure_ready
        self.controller.down = down
        connection = BlockedResultSocket(self.controller)
        startup_expires_at = format_timestamp(
            self.controller.clock() + datetime.timedelta(seconds=300)
        )

        with self.assertRaises(ModelLabError) as caught:
            self.supervisor._serve_up(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": "up",
                    "service_id": "fixture-chat",
                    "host_name": None,
                    "startup_expires_at": startup_expires_at,
                    "startup_deadline": (
                        self.controller.monotonic() + 300
                    ),
                },
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(down_modes, [False])
        self.assertEqual(self.controller.deployments.current.phase, "idle")

    def test_failed_first_up_cannot_rollback_a_later_successful_reuse(
        self,
    ) -> None:
        down_modes = []
        first_send_started = threading.Event()
        release_first_send = threading.Event()
        first_errors = []

        def deployment(phase):
            return SimpleNamespace(
                service_id="fixture-chat",
                deployment_id="deployment-one",
                phase=phase,
                use_leases=(),
                normalized=lambda: {
                    "service_id": "fixture-chat",
                    "deployment_id": "deployment-one",
                    "phase": phase,
                },
            )

        endpoint = SimpleNamespace(
            as_dict=lambda: {"publication_id": "publication-one"}
        )

        def ensure_ready(*_args, **_kwargs):
            current = self.controller.deployments.current
            if current is None:
                current = deployment("ready")
                self.controller.deployments.current = current
            return current, endpoint

        def down(
            _service,
            *,
            now,
            cleanup_deadline=None,
            deadline_error_code="service_cleanup_required",
        ):
            del cleanup_deadline, deadline_error_code
            down_modes.append(now)
            current = deployment("released" if now else "idle")
            self.controller.deployments.current = current
            return current

        class BlockedResultSocket:
            def settimeout(self, _timeout) -> None:
                return None

            def send(self, _payload) -> int:
                first_send_started.set()
                if not release_first_send.wait(2):
                    raise AssertionError("second up did not complete")
                raise OSError("controlled first-client disconnect")

        class AcceptedResultSocket:
            def settimeout(self, _timeout) -> None:
                return None

            @staticmethod
            def send(payload) -> int:
                return len(payload)

        self.controller.ensure_ready = ensure_ready
        self.controller.down = down
        startup_expires_at = format_timestamp(
            self.controller.clock() + datetime.timedelta(seconds=300)
        )
        request = {
            "schema": SUPERVISOR_REQUEST_SCHEMA,
            "operation": "up",
            "service_id": "fixture-chat",
            "host_name": None,
            "startup_expires_at": startup_expires_at,
            "startup_deadline": self.controller.monotonic() + 300,
        }

        def serve_first() -> None:
            try:
                self.supervisor._serve_up(BlockedResultSocket(), request)
            except BaseException as error:
                first_errors.append(error)

        first = threading.Thread(target=serve_first)
        first.start()
        self.assertTrue(first_send_started.wait(2))

        self.supervisor._serve_up(AcceptedResultSocket(), request)
        release_first_send.set()
        first.join(2)

        self.assertFalse(first.is_alive())
        self.assertEqual(len(first_errors), 1)
        self.assertIsInstance(first_errors[0], ModelLabError)
        self.assertEqual(
            first_errors[0].code,
            "supervisor_channel_closed",
        )
        self.assertEqual(down_modes, [False, False])
        self.assertEqual(self.controller.deployments.current.phase, "idle")
        self.assertEqual(self.supervisor._pending_up_rollbacks, {})

    def test_hard_expiry_closes_active_pi_channel_before_claim_recovery(self):
        channel = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(channel)
        deployment = SimpleNamespace(
            service_id="fixture-chat",
            service_sha256="b" * 64,
            deployment_id="deployment-one",
            phase="ready",
            use_leases=(SimpleNamespace(lease_id="use-1"),),
        )
        recovered = []
        self.controller.deployments.list = lambda: (deployment,)
        self.controller.deployments.current = deployment
        self.supervisor.deployed_services.load = lambda *_: self.service
        self.controller.renew_deployment_claim = lambda _deployment: (
            _ for _ in ()
        ).throw(
            ModelLabError(
                "controlled provider hard expiry",
                code="host_claim_expired",
            )
        )
        self.controller.is_claim_gone = (
            lambda error: error.code == "host_claim_expired"
        )
        self.controller.reconcile_claim_gone = (
            lambda service, current: recovered.append(
                (service.service_id, current.deployment_id)
            )
        )
        self.controller.hosts = SimpleNamespace(
            enforce_retirement=lambda *, execute: None
        )

        self.supervisor.maintain_once()

        channel.connection.settimeout(1)
        self.assertEqual(channel.connection.recv(1), b"")
        self.assertEqual(
            recovered,
            [("fixture-chat", "deployment-one")],
        )
        channel.close()

    def test_maintenance_reaps_expired_pending_admission(self):
        observed = SimpleNamespace(
            service_id="fixture-chat",
            service_sha256="b" * 64,
            deployment_id="deployment-one",
            phase="ready",
            use_leases=(SimpleNamespace(lease_id="use-pending"),),
        )
        released = SimpleNamespace(
            service_id="fixture-chat",
            service_sha256="b" * 64,
            deployment_id="deployment-one",
            phase="released",
            use_leases=(),
        )
        reaped = []
        self.controller.deployments.list = lambda: (observed,)
        self.controller.deployments.current = observed
        self.supervisor.deployed_services.load = lambda *_: self.service

        def release_expired(service):
            reaped.append(service.service_id)
            self.controller.deployments.current = released
            return True

        self.controller.release_expired_pending_uses = release_expired
        self.controller.hosts = SimpleNamespace(
            enforce_retirement=lambda *, execute: None
        )

        self.supervisor.maintain_once()

        self.assertEqual(reaped, ["fixture-chat"])

    def test_kernel_sender_credentials_reject_a_forged_session_pid(self) -> None:
        channel = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        send_document(
            channel.connection,
            {
                "schema": SESSION_USE_ADMIT_SCHEMA,
                "profile_id": "chat",
                "service_id": "fixture-chat",
                "pid": os.getpid() + 100000,
                "start_time": "1",
            },
        )

        response = receive_document(channel.connection)

        self.assertEqual(response["schema"], SUPERVISOR_ERROR_SCHEMA)
        self.assertEqual(
            response["code"],
            "session_use_admission_mismatch",
        )
        channel.close()

    def test_singleton_remains_held_until_mutating_worker_finishes(self) -> None:
        release_entered = threading.Event()
        allow_release = threading.Event()

        def blocking_release(
            service,
            use,
            *,
            now,
            stop_if_final=False,
        ):
            release_entered.set()
            allow_release.wait()
            self.controller.releases.append(
                (
                    service.service_id,
                    use.lease.lease_id,
                    now,
                    stop_if_final,
                )
            )

        self.controller.release_profile_use = blocking_release
        channel = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(channel)
        channel.close()
        self.assertTrue(release_entered.wait(2))
        self.supervisor.stop()

        replacement = ModelLabSupervisor(
            controller=FakeController(),
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime,
            maintenance_interval_seconds=3600,
        )
        try:
            with self.assertRaises(ModelLabError) as caught:
                replacement._acquire_singleton()
            self.assertEqual(
                caught.exception.code,
                "supervisor_already_running",
            )
        finally:
            allow_release.set()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

        replacement._acquire_singleton()
        replacement._close_runtime()


class ExpiredClaimPiRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.authored = root / "authored"
        self.state = root / "state"
        self.runtime_root = root / "runtime"
        (self.authored / "profiles" / "chat").mkdir(
            mode=0o700,
            parents=True,
        )
        self.socket_path = root / "inference.sock"
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.socket_path))
        self.hosts = QuarantinedHosts()
        self.service_runtime = FakeRuntime(self.socket_path)
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.service_runtime,
            deployments=DeploymentStore(self.state),
            bindings=ProfileBindingStore(self.authored),
            lab=parse_lab_toml(lab_toml()),
        )
        self.service = parse_service_toml(service_toml())
        self.route = FakeProfile()
        self.profile = SimpleNamespace(
            contract=SimpleNamespace(
                profile_id=self.route.profile_id,
                project_id=self.route.project_id,
                service_id=self.route.service_id,
                endpoint=SimpleNamespace(
                    required_input_modalities=(
                        self.route.required_input_modalities
                    )
                ),
            ),
        )
        self.supervisor = ModelLabSupervisor(
            controller=self.controller,
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime_root,
            maintenance_interval_seconds=3600,
        )
        self.patches = (
            mock.patch(
                "model_lab.supervisor.load_profile_route",
                return_value=self.route,
            ),
            mock.patch(
                "model_lab.supervisor.load_service_id",
                return_value=self.service,
            ),
            mock.patch(
                "model_lab.supervisor.load_profile",
                return_value=self.profile,
            ),
        )
        for patch in self.patches:
            patch.start()
        self.thread = threading.Thread(
            target=self.supervisor.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.assertTrue(self.supervisor.ready_event.wait(2))
        self.client = SupervisorClient(
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime_root,
            launcher=lambda *_: self.fail("running supervisor must be reused"),
        )

    def _acquire_pi(
        self,
        *,
        profile_id: str = FIXTURE_PROFILE_ID,
        host_name: str | None = None,
        stop_on_release: bool = False,
        startup_timeout_seconds: int = 300,
    ) -> PiLeaseChannel:
        self.assertEqual(profile_id, FIXTURE_PROFILE_ID)
        return self.client.acquire_pi(
            **fixture_pi_identity(
                project_id=self.route.project_id,
                service_sha256=self.service.service_sha256,
                workload_sha256=self.service.workload_sha256,
                required_input_modalities=tuple(
                    sorted(self.route.required_input_modalities)
                ),
            ),
            host_name=host_name,
            stop_on_release=stop_on_release,
            startup_timeout_seconds=startup_timeout_seconds,
        )

    def tearDown(self) -> None:
        self.supervisor.stop()
        self.thread.join(2)
        for patch in reversed(self.patches):
            patch.stop()
        self.listener.close()
        self.temporary.cleanup()

    @staticmethod
    def _admit(channel: PiLeaseChannel) -> dict:
        send_document(
            channel.connection,
            {
                "schema": SESSION_USE_ADMIT_SCHEMA,
                "profile_id": "chat",
                "service_id": "fixture-chat",
                "pid": os.getpid(),
                "start_time": process_start_time(os.getpid()),
            },
        )
        return receive_document(channel.connection)

    def test_down_now_is_idempotent_when_immediate_release_wins(self):
        channel = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=True,
        )
        self._admit(channel)
        release_entered = threading.Event()
        allow_release = threading.Event()
        down_fenced = threading.Event()
        original_release = self.controller.release_profile_use
        original_begin_down = self.supervisor._begin_immediate_down

        def blocked_release(*arguments, **keywords):
            release_entered.set()
            if not allow_release.wait(2):
                raise AssertionError("immediate release barrier timed out")
            return original_release(*arguments, **keywords)

        def observed_begin_down(service_id):
            fence = original_begin_down(service_id)
            down_fenced.set()
            return fence

        down_results = []
        down_errors = []

        def run_down():
            try:
                down_results.append(
                    self.client.request(
                        "down",
                        {
                            "service_id": "fixture-chat",
                            "now": True,
                        },
                    )
                )
            except BaseException as error:
                down_errors.append(error)

        with (
            mock.patch.object(
                self.controller,
                "release_profile_use",
                side_effect=blocked_release,
            ),
            mock.patch.object(
                self.supervisor,
                "_begin_immediate_down",
                side_effect=observed_begin_down,
            ),
        ):
            channel.close()
            self.assertTrue(release_entered.wait(2))
            down_worker = threading.Thread(target=run_down)
            down_worker.start()
            try:
                self.assertTrue(down_fenced.wait(2))
            finally:
                allow_release.set()
            down_worker.join(2)

        self.assertFalse(down_worker.is_alive())
        self.assertEqual(down_errors, [])
        self.assertEqual(len(down_results), 1)
        self.assertEqual(
            down_results[0]["deployment"]["phase"],
            "released",
        )
        self.assertEqual(
            self.controller.deployments.load("fixture-chat").phase,
            "released",
        )
        self.assertEqual(self.service_runtime.stops, 1)
        self.assertEqual(self.service_runtime.lost_claim_cleanups, 0)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_down_now_resumes_stopping_without_repeating_runtime_stop(self):
        self.client.request_up(
            service_id="fixture-chat",
            host_name=None,
            startup_timeout_seconds=300,
        )
        self.hosts.crash_before_release = True
        with self.assertRaisesRegex(SystemExit, "host release"):
            self.controller.down(self.service, now=True)
        stopping = self.controller.deployments.load("fixture-chat")
        self.assertEqual(stopping.phase, "stopping")
        self.assertEqual(self.service_runtime.stops, 1)

        result = self.client.request(
            "down",
            {
                "service_id": "fixture-chat",
                "now": True,
            },
        )

        self.assertEqual(result["deployment"]["phase"], "released")
        self.assertEqual(self.service_runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_down_now_closes_use_acquired_before_channel_registration(self):
        acquire_entered = threading.Event()
        allow_acquire_return = threading.Event()
        down_fenced = threading.Event()
        down_finished = threading.Event()
        mapped_when_closed = []
        original_acquire = self.controller.acquire_for_profile
        original_begin_down = self.supervisor._begin_immediate_down
        original_close_connections = (
            self.supervisor._close_service_connections
        )

        def blocked_acquire(*arguments, **keywords):
            use = original_acquire(*arguments, **keywords)
            acquire_entered.set()
            if not allow_acquire_return.wait(2):
                raise AssertionError("Pi acquire barrier timed out")
            return use

        def observed_begin_down(service_id):
            fence = original_begin_down(service_id)
            down_fenced.set()
            return fence

        def observed_close_connections(service_id):
            with self.supervisor._connections_lock:
                mapped_when_closed.append(
                    sum(
                        current_service == service_id
                        for current_service in (
                            self.supervisor._connection_services.values()
                        )
                    )
                )
            original_close_connections(service_id)

        def wait_after_registration(_connection):
            if not down_finished.wait(2):
                raise AssertionError("immediate down barrier timed out")

        pi_channels = []
        pi_errors = []

        def run_pi():
            try:
                pi_channels.append(
                    self._acquire_pi(
                        profile_id="chat",
                        host_name=None,
                        stop_on_release=False,
                    )
                )
            except BaseException as error:
                pi_errors.append(error)

        down_results = []
        down_errors = []

        def run_down():
            try:
                down_results.append(
                    self.client.request(
                        "down",
                        {
                            "service_id": "fixture-chat",
                            "now": True,
                        },
                    )
                )
            except BaseException as error:
                down_errors.append(error)
            finally:
                down_finished.set()

        with (
            mock.patch.object(
                self.controller,
                "acquire_for_profile",
                side_effect=blocked_acquire,
            ),
            mock.patch.object(
                self.supervisor,
                "_begin_immediate_down",
                side_effect=observed_begin_down,
            ),
            mock.patch.object(
                self.supervisor,
                "_close_service_connections",
                side_effect=observed_close_connections,
            ),
            mock.patch(
                "model_lab.supervisor.enable_sender_credentials",
                side_effect=wait_after_registration,
            ),
        ):
            pi_worker = threading.Thread(target=run_pi)
            pi_worker.start()
            self.assertTrue(acquire_entered.wait(2))
            down_worker = threading.Thread(target=run_down)
            down_worker.start()
            try:
                self.assertTrue(down_fenced.wait(2))
            finally:
                allow_acquire_return.set()
            down_worker.join(2)
            pi_worker.join(2)

        self.assertFalse(down_worker.is_alive())
        self.assertFalse(pi_worker.is_alive())
        self.assertEqual(down_errors, [])
        self.assertEqual(len(down_results), 1)
        self.assertEqual(mapped_when_closed, [1])
        self.assertEqual(pi_channels, [])
        self.assertEqual(len(pi_errors), 1)
        self.assertIsInstance(pi_errors[0], ModelLabError)
        self.assertEqual(
            pi_errors[0].code,
            "supervisor_channel_closed",
        )
        self.assertEqual(
            self.controller.deployments.load("fixture-chat").phase,
            "released",
        )
        self.assertEqual(self.service_runtime.starts, 1)
        self.assertEqual(self.service_runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_down_now_fences_preexisting_waiter_but_later_pi_restarts(self):
        self.client.request_up(
            service_id="fixture-chat",
            host_name=None,
            startup_timeout_seconds=300,
        )
        first_deployment = self.controller.deployments.load(
            "fixture-chat"
        )
        pi_ticketed = threading.Event()
        down_fenced = threading.Event()
        original_begin_pi = self.supervisor._begin_pi_operation
        original_begin_down = self.supervisor._begin_immediate_down

        def observed_begin_pi(service_id):
            operation = original_begin_pi(service_id)
            pi_ticketed.set()
            return operation

        def observed_begin_down(service_id):
            fence = original_begin_down(service_id)
            down_fenced.set()
            return fence

        pi_errors = []

        def run_pi():
            try:
                self._acquire_pi(
                    profile_id="chat",
                    host_name=None,
                    stop_on_release=False,
                )
            except BaseException as error:
                pi_errors.append(error)

        down_results = []
        down_errors = []

        def run_down():
            try:
                down_results.append(
                    self.client.request(
                        "down",
                        {
                            "service_id": "fixture-chat",
                            "now": True,
                        },
                    )
                )
            except BaseException as error:
                down_errors.append(error)

        with (
            mock.patch.object(
                self.supervisor,
                "_begin_pi_operation",
                side_effect=observed_begin_pi,
            ),
            mock.patch.object(
                self.supervisor,
                "_begin_immediate_down",
                side_effect=observed_begin_down,
            ),
        ):
            with self.supervisor._service_mutation("fixture-chat"):
                pi_worker = threading.Thread(target=run_pi)
                pi_worker.start()
                self.assertTrue(pi_ticketed.wait(2))
                down_worker = threading.Thread(target=run_down)
                down_worker.start()
                self.assertTrue(down_fenced.wait(2))
            pi_worker.join(2)
            down_worker.join(2)

        self.assertFalse(pi_worker.is_alive())
        self.assertFalse(down_worker.is_alive())
        self.assertEqual(len(pi_errors), 1)
        self.assertIsInstance(pi_errors[0], ModelLabError)
        self.assertEqual(
            pi_errors[0].code,
            "service_startup_superseded",
        )
        self.assertEqual(down_errors, [])
        self.assertEqual(len(down_results), 1)
        self.assertEqual(
            self.controller.deployments.load("fixture-chat").phase,
            "released",
        )
        self.assertEqual(self.service_runtime.starts, 1)
        self.assertEqual(self.service_runtime.stops, 1)

        later = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        accepted = self._admit(later)
        self.assertNotEqual(
            accepted["deployment_id"],
            first_deployment.deployment_id,
        )
        self.assertEqual(self.service_runtime.starts, 2)
        later.close()

    def test_second_pi_closes_old_channel_before_replacing_expired_claim(self):
        first = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        self.hosts.active = False

        second = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertNotEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(self.service_runtime.lost_claim_cleanups, 1)
        self.assertEqual(self.service_runtime.starts, 2)
        accepted = self._admit(second)
        self.assertEqual(
            accepted["deployment_id"],
            second.pending.deployment_id,
        )
        first.close()
        second.close()

    def test_second_pi_replaces_a_terminated_exact_host_operation(self):
        first = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        self.hosts.gone_code = "host_claim_host_changed"
        self.hosts.active = False

        second = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertNotEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(self.service_runtime.lost_claim_cleanups, 1)
        self._admit(second)
        first.close()
        second.close()

    def test_sibling_expiry_drains_claim_before_second_pi_reacquires(self):
        first = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        self.hosts.quarantined = True

        second = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertNotEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(self.service_runtime.stops, 1)
        self.assertEqual(self.service_runtime.lost_claim_cleanups, 0)
        self.assertEqual(self.hosts.releases, [(1, True)])
        self._admit(second)
        first.close()
        second.close()

    def test_second_pi_closes_old_channel_before_replacing_dead_runtime(self):
        first = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        replacement_runtime = NotReadyRuntime(self.socket_path)
        self.controller.runtime = replacement_runtime

        second = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertNotEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(replacement_runtime.stops, 1)
        self.assertEqual(replacement_runtime.starts, 1)
        self._admit(second)
        first.close()
        second.close()

    def test_second_pi_rebinds_active_sessions_after_transport_replacement(self):
        first = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        original_attest = self.service_runtime.attest_ready
        replacement_reported = False

        def attest_after_transport_replacement(
            service,
            claim,
            deployment,
            *,
            startup_deadline=None,
        ):
            nonlocal replacement_reported
            if not replacement_reported:
                replacement_reported = True
                raise ModelLabError(
                    "controlled transport replacement",
                    code="service_transport_replaced",
                )
            return original_attest(
                service,
                claim,
                deployment,
                startup_deadline=startup_deadline,
            )

        self.service_runtime.attest_ready = (
            attest_after_transport_replacement
        )

        second = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(self.service_runtime.starts, 1)
        self.assertEqual(self.hosts.releases, [])
        self._admit(second)
        first.close()
        second.close()

    def test_early_now_exit_is_honored_by_the_final_normal_user(self):
        immediate = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=True,
        )
        self._admit(immediate)
        normal = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(normal)

        immediate.close()
        deadline = time.monotonic() + 2
        deployment = self.controller.deployments.load("fixture-chat")
        while (
            len(deployment.use_leases) != 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            deployment = self.controller.deployments.load("fixture-chat")

        self.assertEqual(deployment.phase, "ready")
        self.assertEqual(deployment.host_release_mode, "now")
        self.assertEqual(len(deployment.use_leases), 1)
        self.assertEqual(self.service_runtime.stops, 0)
        self.assertEqual(self.hosts.releases, [])

        normal.close()
        deadline = time.monotonic() + 2
        deployment = self.controller.deployments.load("fixture-chat")
        while deployment.phase != "released" and time.monotonic() < deadline:
            time.sleep(0.01)
            deployment = self.controller.deployments.load("fixture-chat")

        self.assertEqual(deployment.phase, "released")
        self.assertEqual(self.service_runtime.stops, 1)
        self.assertTrue(self.hosts.releases[-1][1])

    def test_failed_normal_admission_does_not_latch_shared_service_now(self):
        active = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(active)
        failed_admission = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        failed_admission.close()
        deadline = time.monotonic() + 2
        deployment = self.controller.deployments.load("fixture-chat")
        while (
            len(deployment.use_leases) != 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            deployment = self.controller.deployments.load("fixture-chat")

        self.assertEqual(deployment.phase, "ready")
        self.assertIsNone(deployment.host_release_mode)
        self.assertEqual(len(deployment.use_leases), 1)
        self.assertEqual(self.service_runtime.stops, 0)
        self.assertEqual(self.hosts.releases, [])

        active.close()
        deadline = time.monotonic() + 2
        deployment = self.controller.deployments.load("fixture-chat")
        while deployment.phase != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)
            deployment = self.controller.deployments.load("fixture-chat")

        self.assertEqual(deployment.phase, "idle")
        self.assertIsNone(deployment.host_release_mode)
        self.assertEqual(self.service_runtime.stops, 0)
        self.assertEqual(self.hosts.releases, [])

    def test_up_cannot_stop_a_service_held_by_an_active_pi(self):
        active = self._acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(active)
        before = self.controller.deployments.load("fixture-chat")

        with self.assertRaises(ModelLabError) as caught:
            self.client.request_up(
                service_id="fixture-chat",
                host_name=None,
                startup_timeout_seconds=300,
            )

        self.assertEqual(caught.exception.code, "service_in_use")
        after = self.controller.deployments.load("fixture-chat")
        self.assertEqual(after.deployment_id, before.deployment_id)
        self.assertEqual(after.phase, "ready")
        self.assertEqual(after.use_leases, before.use_leases)
        self.assertEqual(self.service_runtime.stops, 0)
        self.assertEqual(self.hosts.releases, [])
        active.connection.settimeout(0.05)
        with self.assertRaises(TimeoutError):
            active.connection.recv(1)

        active.close()
        deadline = time.monotonic() + 2
        after = self.controller.deployments.load("fixture-chat")
        while after.phase != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)
            after = self.controller.deployments.load("fixture-chat")
        self.assertEqual(after.phase, "idle")


class ModelSessionSubprocessTest(unittest.TestCase):
    @staticmethod
    def _channel(client: socket.socket, *, startup_deadline: float) -> PiLeaseChannel:
        return PiLeaseChannel(
            pending=PendingPiUse(
                **fixture_pi_identity(),
                deployment_id="deployment-one",
                use_lease_id="use-one",
            ),
            connection=client,
            startup_deadline=startup_deadline,
        )

    def test_child_receives_unix_stream_fd_at_least_three(self) -> None:
        client, supervisor = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        channel = self._channel(client, startup_deadline=42.0)
        captured = {}

        def popen(arguments, *, close_fds, pass_fds):
            self.assertTrue(close_fds)
            self.assertEqual(len(pass_fds), 1)
            descriptor = pass_fds[0]
            self.assertGreaterEqual(descriptor, 3)
            metadata = os.fstat(descriptor)
            self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
            captured["arguments"] = arguments
            captured["descriptor"] = descriptor
            return SimpleNamespace(wait=lambda: 17)

        try:
            with mock.patch(
                "model_lab.supervisor_client.subprocess.Popen",
                side_effect=popen,
            ):
                result = subprocess_model_session(
                    pathlib.Path("/mnt/dev/model-lab/profiles/chat"),
                    ["resume", "session-one"],
                    channel,
                    monotonic=lambda: 10.0,
                )
            self.assertEqual(result, 17)
            self.assertEqual(
                captured["arguments"][1:7],
                [
                    "--model-lab-use-fd",
                    str(captured["descriptor"]),
                    "--model-lab-use-deadline",
                    "42",
                    "--profile",
                    "/mnt/dev/model-lab/profiles/chat",
                ],
            )
            with self.assertRaises(OSError):
                os.fstat(captured["descriptor"])
            supervisor.settimeout(1)
            self.assertEqual(supervisor.recv(1), b"")
        finally:
            supervisor.close()

    def test_expired_pending_channel_cannot_spawn_model_session(self) -> None:
        client, supervisor = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        channel = self._channel(client, startup_deadline=42.0)
        try:
            with mock.patch(
                "model_lab.supervisor_client.subprocess.Popen"
            ) as popen:
                with self.assertRaises(ModelLabError) as caught:
                    subprocess_model_session(
                        pathlib.Path("/mnt/dev/model-lab/profiles/chat"),
                        [],
                        channel,
                        monotonic=lambda: 42.0,
                    )
            self.assertEqual(caught.exception.code, "service_startup_timeout")
            popen.assert_not_called()
            supervisor.settimeout(1)
            self.assertEqual(supervisor.recv(1), b"")
        finally:
            supervisor.close()


if __name__ == "__main__":
    unittest.main()
