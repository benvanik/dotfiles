"""Production dependency graph for the model-lab supervisor."""

from __future__ import annotations

import pathlib

from runpod_local.api import RunpodApi
from runpod_local.auth import CredentialStore
from runpod_local.errors import RunpodLocalError
from runpod_local.host_control import HostControl as GenericHostControl
from runpod_local.instances import InstanceStore
from runpod_local.lifecycle import LifecycleManager
from runpod_local.paths import runpod_root, state_root as runpod_state_root
from runpod_local.profile import ProfileStore
from runpod_local.state import StateStore

from .configuration import LabConfiguration
from .controller import ModelLabController
from .errors import ModelLabError
from .lifecycle import DeploymentStore
from .production_backend import (
    ProductionModelServiceBackend,
    RunpodHostControlAdapter,
    ServiceEndpointPublisher,
)
from .profile_binding import ProfileBindingStore
from .service_installation import ServiceInstallationStore
from .service_runtime import ProductionServiceRuntime


def _source_root() -> pathlib.Path:
    try:
        return pathlib.Path(__file__).resolve(strict=True).parents[2]
    except (OSError, IndexError) as error:
        raise ModelLabError(
            "cannot resolve the installed model-lab source root",
            code="unsafe_model_lab_installation",
        ) from error


def build_controller(
    *,
    authored_root: pathlib.Path,
    state_root: pathlib.Path,
    runtime_root: pathlib.Path,
    lab: LabConfiguration,
) -> ModelLabController:
    """Compose model ownership over, but never inside, generic RunPod state."""

    generic_state = StateStore(runpod_state_root())
    try:
        credential = CredentialStore().load(required=True)
    except RunpodLocalError as error:
        raise ModelLabError(str(error), code=error.code) from error
    if credential is None:
        raise AssertionError("required RunPod credential unexpectedly absent")
    api = RunpodApi(credential)
    lifecycle = LifecycleManager(api, generic_state)
    generic_control = GenericHostControl(
        state=generic_state,
        lifecycle=lifecycle,
        profiles=ProfileStore(runpod_root()),
    )
    hosts = RunpodHostControlAdapter(generic_control)
    instances = InstanceStore(generic_state)
    model_state = StateStore(state_root)
    deployments = DeploymentStore(state_root)
    backend = ProductionModelServiceBackend(
        source_root=_source_root(),
        state_root=state_root,
        runtime_root=runtime_root,
        runpod_state=generic_state,
        api=api,
        hosts=hosts,
        instances=instances,
        installations=ServiceInstallationStore(model_state),
    )
    runtime = ProductionServiceRuntime(
        backend=backend,
        publisher=ServiceEndpointPublisher(runtime_root),
        deployments=deployments,
        endpoint_ttl_seconds=lab.lease.hard_ttl_seconds,
        service_idle_ttl_seconds=lab.lease.service_idle_ttl_seconds,
    )
    return ModelLabController(
        hosts=hosts,
        runtime=runtime,
        deployments=deployments,
        bindings=ProfileBindingStore(authored_root),
        lab=lab,
    )
