"""Model-service installation receipt tests."""

from __future__ import annotations

import contextlib
import datetime
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from model_lab.errors import ModelLabError
from runpod_local.instances import InstanceStore
from runpod_local.remote import SshEndpoint
from model_lab.runtime_catalog import load_runtime
from model_lab.service_definition import load_service
from model_lab.service_huggingface import (
    HuggingFaceClosure,
    HuggingFaceClosureFile,
)
from model_lab.service_installation import (
    ServiceInstallationStore,
    build_service_deployment_request,
    require_current_instance,
)
from model_lab.service_materialization import (
    build_service_materialization_plan,
    materialize_service,
)
from runpod_local.state import StateStore
from runpod_local.timeutil import utc_timestamp
from model_lab_service_test_fixture import SERVICE_FIXTURE

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = SERVICE_FIXTURE
OPERATION_ID = "11111111-1111-4111-8111-111111111111"


def closure(identity_digit: str = "7") -> HuggingFaceClosure:
    definition = load_service(FIXTURE)
    model = definition.normalized_plan()["model"]
    checkpoint = model["checkpoint"]
    if not isinstance(checkpoint, str):
        raise TypeError("fixture requires one exact checkpoint")
    return HuggingFaceClosure(
        repository=model["repository"],
        revision=model["revision"],
        requested_selector=checkpoint,
        resolved_index=None,
        weight_files=(checkpoint,),
        files=(
            HuggingFaceClosureFile(
                path=checkpoint,
                bytes=4096,
                role="checkpoint-weight",
                identity_algorithm="sha256",
                identity_digest=identity_digit * 64,
            ),
        ),
    )


def endpoint(
    root: pathlib.Path,
    *,
    operation_id: str = OPERATION_ID,
    pod_id: str = "pod-1",
) -> SshEndpoint:
    return SshEndpoint(
        instance_name="active-instance",
        operation_id=operation_id,
        pod_id=pod_id,
        host="203.0.113.42",
        port=22022,
        user="root",
        identity_file=root / "identity",
        known_hosts_file=root / "known-hosts",
        host_key_alias=f"runpod-{pod_id}",
    )


def materialized_service(
    state_root: pathlib.Path,
    *,
    identity_digit: str = "7",
):
    definition = load_service(FIXTURE)
    plan = build_service_materialization_plan(
        definition,
        source_root=ROOT,
        state_root=state_root,
        runtime=load_runtime("vllm-cu129-v0.25.1"),
        closure=closure(identity_digit),
        remote_port=8123,
    )
    return materialize_service(plan)


def publish_installation(
    store: ServiceInstallationStore,
    *,
    materialized,
    target: SshEndpoint,
):
    instances = InstanceStore(store.state)
    with mock.patch.object(
        instances,
        "locked_active_lease",
        return_value=contextlib.nullcontext({}),
    ):
        return store.publish(
            materialization=materialized,
            endpoint=target,
            instances=instances,
        )


class ServiceInstallationTest(unittest.TestCase):
    def test_request_binds_config_closure_and_port(self):
        definition = load_service(FIXTURE)

        request = build_service_deployment_request(
            definition,
            closure=closure(),
            remote_port=8123,
        )

        self.assertEqual(request.service_id, "fixture-dense-text")
        self.assertEqual(request.service_plan_sha256, definition.plan_sha256)
        self.assertEqual(
            request.huggingface_closure_sha256,
            closure().closure_sha256,
        )
        self.assertEqual(request.remote_port, 8123)
        self.assertEqual(len(request.request_sha256), 64)

    def test_publish_is_private_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = StateStore(root / "state")
            store = ServiceInstallationStore(state)
            materialized = materialized_service(state.root)
            target = endpoint(root)

            installed, changed = publish_installation(
                store,
                materialized=materialized,
                target=target,
            )
            repeated, repeated_changed = publish_installation(
                store,
                materialized=materialized,
                target=target,
            )
            receipt_path = store.receipt_path(
                instance_name=target.instance_name,
                service_id=installed.request.service_id,
            )

            self.assertIs(changed, True)
            self.assertIs(repeated_changed, False)
            self.assertEqual(repeated, installed)
            self.assertEqual(
                installed.materialization.materialization_sha256,
                materialized.materialization_sha256,
            )
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(receipt_path.parent.stat().st_mode),
                0o700,
            )

    def test_publish_recovers_after_receipt_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = StateStore(root / "state")
            store = ServiceInstallationStore(state)
            materialized = materialized_service(state.root)
            target = endpoint(root)

            with (
                mock.patch.object(
                    state,
                    "write",
                    side_effect=OSError("simulated receipt publication failure"),
                ),
                self.assertRaises(OSError),
            ):
                publish_installation(
                    store,
                    materialized=materialized,
                    target=target,
                )
            self.assertIsNone(
                store.load(
                    instance_name=target.instance_name,
                    service_id="fixture-dense-text",
                    required=False,
                )
            )

            installed, changed = publish_installation(
                store,
                materialized=materialized,
                target=target,
            )

            self.assertIs(changed, True)
            self.assertEqual(
                installed.materialization.materialization_sha256,
                materialized.materialization_sha256,
            )

    def test_fresh_install_replaces_an_unusable_old_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = StateStore(root / "state")
            store = ServiceInstallationStore(state)
            materialized = materialized_service(state.root)
            target = endpoint(root)
            receipt_path = store.receipt_path(
                instance_name=target.instance_name,
                service_id="fixture-dense-text",
            )
            state.write(
                "service-installations",
                receipt_path.stem,
                {"schema_version": "interrupted-old-receipt"},
            )

            installed, changed = publish_installation(
                store,
                materialized=materialized,
                target=target,
            )

            self.assertIs(changed, True)
            self.assertEqual(installed.instance["pod_id"], "pod-1")

    def test_receipt_rejects_a_replaced_pod_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = StateStore(root / "state")
            store = ServiceInstallationStore(state)
            installed, _ = publish_installation(
                store,
                materialized=materialized_service(state.root),
                target=endpoint(root),
            )
            replacement = endpoint(
                root,
                operation_id="22222222-2222-4222-8222-222222222222",
                pod_id="pod-2",
            )

            with self.assertRaises(ModelLabError) as caught:
                require_current_instance(installed, endpoint=replacement)

            self.assertEqual(
                caught.exception.code,
                "service_installation_instance_changed",
            )

    def test_publish_checks_expiry_after_acquiring_the_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = StateStore(root / "state")
            store = ServiceInstallationStore(state)
            target = endpoint(root)
            installed, _ = publish_installation(
                store,
                materialized=materialized_service(state.root),
                target=target,
            )
            receipt_path = store.receipt_path(
                instance_name=target.instance_name,
                service_id=installed.request.service_id,
            )
            receipt_before = receipt_path.read_bytes()
            instances = InstanceStore(state)
            deadline = datetime.datetime(
                2035,
                1,
                1,
                tzinfo=datetime.timezone.utc,
            )
            before_deadline = deadline - datetime.timedelta(microseconds=1)
            after_deadline = deadline
            inside_instance_lock = False
            clock_observations: list[bool] = []
            original_locked = state.locked

            @contextlib.contextmanager
            def observed_lock(scope: str, **lock_arguments: object):
                nonlocal inside_instance_lock
                self.assertEqual(
                    set(lock_arguments),
                    {"deadline", "monotonic", "deadline_error_code"},
                )
                with original_locked(scope, **lock_arguments):
                    is_instance_lock = scope.startswith("instance-")
                    if is_instance_lock:
                        inside_instance_lock = True
                    try:
                        yield
                    finally:
                        if is_instance_lock:
                            inside_instance_lock = False

            def crossing_clock() -> datetime.datetime:
                clock_observations.append(inside_instance_lock)
                return after_deadline if inside_instance_lock else before_deadline

            active_record = {
                "name": target.instance_name,
                "phase": "active",
                "operation_id": target.operation_id,
                "pod_id": target.pod_id,
                "lease": {
                    "expires_at": utc_timestamp(deadline),
                    "idle_timeout_seconds": None,
                },
            }

            with (
                mock.patch.object(state, "locked", side_effect=observed_lock),
                mock.patch.object(instances, "load", return_value=active_record),
                self.assertRaises(ModelLabError) as caught,
            ):
                store.publish(
                    materialization=materialized_service(
                        state.root,
                        identity_digit="8",
                    ),
                    endpoint=target,
                    instances=instances,
                    clock=crossing_clock,
                )

            self.assertEqual(caught.exception.code, "lease_expired")
            self.assertEqual(clock_observations, [True])
            self.assertEqual(receipt_path.read_bytes(), receipt_before)

    def test_exact_selector_recovers_without_a_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = StateStore(root / "state")
            store = ServiceInstallationStore(state)
            materialized = materialized_service(state.root)
            target = endpoint(root)

            self.assertIsNone(
                store.load(
                    instance_name=target.instance_name,
                    service_id="fixture-dense-text",
                    required=False,
                )
            )
            selected = store.load_selector(materialized.materialization_sha256)
            selected_by_path = store.load_selector(str(materialized.root))
            inspected = store.inspect(
                materialization=selected,
                endpoint=target,
            )

            self.assertEqual(selected_by_path, selected)
            self.assertEqual(
                inspected.materialization.materialization_sha256,
                materialized.materialization_sha256,
            )
            self.assertFalse(
                store.receipt_path(
                    instance_name=target.instance_name,
                    service_id="fixture-dense-text",
                ).exists()
            )
            with self.assertRaises(ModelLabError) as caught:
                store.load_selector(str(root / materialized.materialization_sha256))
            self.assertEqual(
                caught.exception.code,
                "invalid_service_materialization_selector",
            )


if __name__ == "__main__":
    unittest.main()
