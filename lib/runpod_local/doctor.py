"""Read-only local and live diagnostics for the Runpod control plane."""

from __future__ import annotations

import datetime
import os
import pathlib
import shutil
import stat
from typing import Any

from .allocation import verify_allocated_pod
from .api import RunpodApi
from .auth import CredentialStore
from .errors import RunpodLocalError
from .instances import InstanceStore, lease_expiry_reasons
from .profile import (
    ProfileStore,
    validate_profile_ssh_files,
)
from .remote import (
    endpoint_from_record_pod,
    validate_known_hosts_file,
)
from .state import StateStore
from .timeutil import utc_timestamp


class CheckCollector:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(
        self,
        identifier: str,
        status: str,
        message: str,
        **details: Any,
    ) -> None:
        check = {"id": identifier, "status": status, "message": message}
        if details:
            check["details"] = details
        self.checks.append(check)

    def error(self, identifier: str, error: RunpodLocalError) -> None:
        self.add(
            identifier,
            "error",
            str(error),
            error_code=error.code,
        )

    def result(self) -> dict[str, Any]:
        statuses = {check["status"] for check in self.checks}
        overall = (
            "error"
            if "error" in statuses
            else "warning" if "warning" in statuses else "ok"
        )
        return {
            "schema_version": "runpod.doctor.v1",
            "generated_at": utc_timestamp(),
            "status": overall,
            "checks": self.checks,
        }


def _check_state_root(
    state: StateStore, collector: CheckCollector
) -> None:
    try:
        metadata = state.root.lstat()
    except FileNotFoundError:
        collector.add(
            "state_root",
            "warning",
            f"state root does not exist yet: {state.root}",
        )
        return
    if (
        state.root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or metadata.st_mode & 0o077
    ):
        collector.add(
            "state_root",
            "error",
            f"state root is not a private owned mode-0700 directory: {state.root}",
        )
        return
    collector.add(
        "state_root",
        "ok",
        f"private state root is mode 0700: {state.root}",
    )


def _check_local_configuration(
    *,
    state: StateStore,
    credential_store: CredentialStore,
    collector: CheckCollector,
) -> tuple[list[dict[str, Any]], Any | None]:
    for command in ("ssh", "scp"):
        path = shutil.which(command)
        collector.add(
            f"command_{command}",
            "ok" if path else "error",
            (
                f"{command} is available at {path}"
                if path
                else f"{command} is not on PATH"
            ),
        )
    credential = None
    try:
        credential = credential_store.load(required=False)
        if credential is None:
            collector.add(
                "credential",
                "error",
                "no Runpod credential is configured",
            )
        else:
            source = credential.source
            path = str(credential.path) if credential.path is not None else None
            collector.add(
                "credential",
                "ok",
                f"credential source is {source}",
                path=path,
            )
    except RunpodLocalError as error:
        collector.error("credential", error)

    _check_state_root(state, collector)
    profiles = []
    try:
        profiles = ProfileStore(state).list()
        collector.add(
            "profiles",
            "ok" if profiles else "warning",
            (
                f"{len(profiles)} launch profile(s) validate"
                if profiles
                else "no local launch profiles exist"
            ),
        )
        for profile in profiles:
            try:
                validate_profile_ssh_files(profile)
                collector.add(
                    f"profile_identity_{profile['name']}",
                    "ok",
                    f"profile {profile['name']} SSH identity is private",
                )
            except RunpodLocalError as error:
                collector.error(
                    f"profile_identity_{profile['name']}", error
                )
    except RunpodLocalError as error:
        collector.error("profiles", error)

    instances = []
    try:
        instances = InstanceStore(state).list()
        collector.add(
            "instances",
            "ok",
            f"{len(instances)} local instance receipt(s) validate",
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        for record in instances:
            reasons = lease_expiry_reasons(record, now=now)
            if reasons:
                collector.add(
                    f"lease_{record['name']}",
                    "warning",
                    f"instance {record['name']} is overdue: "
                    + ", ".join(reasons),
                )
    except RunpodLocalError as error:
        collector.error("instances", error)

    known_hosts_directory = state.root / "ssh" / "known-hosts"
    try:
        try:
            metadata = known_hosts_directory.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if (
                known_hosts_directory.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or (
                    hasattr(os, "getuid")
                    and metadata.st_uid != os.getuid()
                )
                or metadata.st_mode & 0o077
            ):
                raise RunpodLocalError(
                    "known-hosts directory is not a private owned "
                    f"mode-0700 directory: {known_hosts_directory}",
                    code="unsafe_known_hosts",
                )
            known_hosts_paths = sorted(known_hosts_directory.iterdir())
        else:
            known_hosts_paths = []
        for path in known_hosts_paths:
            validate_known_hosts_file(path)
        collector.add(
            "known_hosts",
            "ok",
            f"{len(known_hosts_paths)} dedicated known-hosts file(s) validate",
        )
    except (OSError, RunpodLocalError) as error:
        if isinstance(error, RunpodLocalError):
            collector.error("known_hosts", error)
        else:
            collector.add(
                "known_hosts",
                "error",
                f"cannot inspect dedicated known-hosts files: {error}",
            )
    return instances, credential


def _check_live(
    *,
    api: RunpodApi,
    state: StateStore,
    instances: list[dict[str, Any]],
    collector: CheckCollector,
) -> None:
    pods = None
    volumes = None
    try:
        pods = api.list_pods()
        collector.add(
            "provider_pods",
            "ok",
            f"Runpod returned {len(pods)} Pod(s)",
        )
    except RunpodLocalError as error:
        collector.error("provider_pods", error)
    try:
        volumes = api.list_network_volumes()
        collector.add(
            "provider_volumes",
            "ok",
            f"Runpod returned {len(volumes)} network volume(s)",
        )
    except RunpodLocalError as error:
        collector.error("provider_volumes", error)
    try:
        stock = api.stock(gpu_count=1, secure_cloud=True)
        quoted = sum(
            gpu.get("on_demand_price_per_gpu_hour") is not None
            for gpu in stock["gpus"]
        )
        collector.add(
            "provider_stock",
            "ok",
            f"Runpod returned {len(stock['gpus'])} GPU types; "
            f"{quoted} have on-demand quotes",
        )
    except RunpodLocalError as error:
        collector.error("provider_stock", error)

    if pods is None:
        return
    pods_by_id = {
        pod.get("id"): pod for pod in pods if isinstance(pod.get("id"), str)
    }
    names: dict[str, list[str]] = {}
    for pod in pods:
        if isinstance(pod.get("name"), str):
            names.setdefault(pod["name"], []).append(str(pod.get("id")))
    managed_remote_names = {
        record.get("remote_name")
        for record in instances
        if isinstance(record.get("remote_name"), str)
    }
    for name in sorted(managed_remote_names):
        pod_ids = names.get(name, [])
        if len(pod_ids) > 1:
            collector.add(
                f"duplicate_remote_name_{name}",
                "error",
                f"remote name {name} belongs to multiple Pods",
                pod_ids=sorted(pod_ids),
            )

    managed_ids = set()
    for record in instances:
        phase = record["phase"]
        pod_id = record.get("pod_id")
        if isinstance(pod_id, str):
            managed_ids.add(pod_id)
        pod = pods_by_id.get(pod_id)
        name_matches = [
            candidate
            for candidate in pods
            if candidate.get("name") == record["remote_name"]
        ]
        if pod_id is None and phase in {"intent", "submitting", "conflict"}:
            for candidate in name_matches:
                candidate_id = candidate.get("id")
                if isinstance(candidate_id, str):
                    managed_ids.add(candidate_id)
            if len(name_matches) == 1:
                pod = name_matches[0]

        if phase == "active":
            if pod is None:
                collector.add(
                    f"active_pod_{record['name']}",
                    "error",
                    f"active receipt {record['name']} has no live Pod",
                )
                continue
            try:
                endpoint = endpoint_from_record_pod(
                    record, pod=pod, state=state
                )
                collector.add(
                    f"active_pod_{record['name']}",
                    "ok",
                    f"active Pod {pod_id} is allocation- and SSH-ready",
                    endpoint={
                        "host": endpoint.host,
                        "port": endpoint.port,
                        "host_key_alias": endpoint.host_key_alias,
                    },
                )
            except RunpodLocalError as error:
                status = "warning" if error.code == "pod_not_ready" else "error"
                collector.add(
                    f"active_pod_{record['name']}",
                    status,
                    str(error),
                    error_code=error.code,
                )
        elif phase == "submitting":
            collector.add(
                f"submitting_pod_{record['name']}",
                "warning",
                (
                    f"submission {record['name']} has one Pod awaiting "
                    "receipt reconciliation"
                    if pod is not None
                    else f"submission {record['name']} has no visible Pod yet"
                ),
                pod_id=pod.get("id") if pod is not None else None,
            )
        elif phase == "provisioning":
            if pod is None:
                collector.add(
                    f"provisioning_pod_{record['name']}",
                    "error",
                    f"provisioning receipt {record['name']} has no live Pod",
                )
            else:
                violations, pending = verify_allocated_pod(record, pod)
                if violations:
                    collector.add(
                        f"provisioning_pod_{record['name']}",
                        "error",
                        "provisioning Pod violates allocation policy",
                        violations=violations,
                    )
                else:
                    collector.add(
                        f"provisioning_pod_{record['name']}",
                        "warning",
                        (
                            "provisioning Pod is still missing provider fields"
                            if pending
                            else "provisioning Pod is ready for launch "
                            "reconciliation"
                        ),
                        pending=pending,
                    )
        elif phase in {"termination_pending", "rollback_required"}:
            collector.add(
                f"cleanup_pod_{record['name']}",
                "error" if pod is not None else "warning",
                (
                    f"{phase} receipt still has live Pod {pod_id}; "
                    "TTL enforcement will retry exact-ID deletion"
                    if pod is not None
                    else f"{phase} receipt needs local absence reconciliation"
                ),
            )
        elif pod is not None and phase in {
            "rolled_back",
            "terminated",
            "aborted",
        }:
            collector.add(
                f"terminal_pod_{record['name']}",
                "error",
                f"terminal receipt {record['name']} still has live Pod {pod_id}",
            )
        elif phase == "intent" and pod is not None:
            collector.add(
                f"intent_pod_{record['name']}",
                "error",
                f"unsubmitted intent {record['name']} unexpectedly has a Pod",
            )
        elif phase == "conflict":
            collector.add(
                f"conflict_pod_{record['name']}",
                "error",
                f"instance {record['name']} has an unresolved Pod-name conflict",
                pod_ids=sorted(
                    str(candidate.get("id")) for candidate in name_matches
                ),
            )

    unmanaged = [
        pod for pod in pods if pod.get("id") not in managed_ids
    ]
    collector.add(
        "unmanaged_pods",
        "warning" if unmanaged else "ok",
        (
            f"{len(unmanaged)} Pod(s) are unmanaged by this state root"
            if unmanaged
            else "no unmanaged Pods are visible"
        ),
        pod_ids=sorted(
            str(pod.get("id")) for pod in unmanaged
        ),
    )
    if volumes is not None:
        volume_ids = {
            volume.get("id")
            for volume in volumes
            if isinstance(volume.get("id"), str)
        }
        for record in instances:
            expected_volume = record["expected"]["network_volume_id"]
            if (
                record["phase"] == "active"
                and expected_volume is not None
                and expected_volume not in volume_ids
            ):
                collector.add(
                    f"volume_{record['name']}",
                    "error",
                    f"active instance {record['name']} references missing "
                    f"network volume {expected_volume}",
                )


def run_doctor(
    *,
    state: StateStore,
    credential_store: CredentialStore,
    live: bool,
) -> dict[str, Any]:
    collector = CheckCollector()
    instances, credential = _check_local_configuration(
        state=state,
        credential_store=credential_store,
        collector=collector,
    )
    if live:
        if credential is None:
            collector.add(
                "provider",
                "error",
                "live checks require a valid Runpod credential",
            )
        else:
            _check_live(
                api=RunpodApi(credential),
                state=state,
                instances=instances,
                collector=collector,
            )
    return collector.result()
