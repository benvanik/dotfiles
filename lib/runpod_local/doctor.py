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
from .claim_acquisition import ClaimAcquisitionStore
from .claims import (
    ClaimStore,
    attest_claim_ledger_receipt,
)
from .errors import RunpodLocalError
from .instances import InstanceStore, lease_expiry_reasons
from .paths import runpod_config_file, volume_root
from .profile import (
    ProfileStore,
    validate_profile_ssh_files,
)
from .remote import (
    endpoint_from_record_pod,
    validate_known_hosts_file,
)
from .state import StateStore
from .timeutil import parse_utc_timestamp, utc_timestamp


TERMINAL_INSTANCE_PHASES = frozenset(
    {"aborted", "rolled_back", "terminated"}
)


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


def _check_reserved_authored_paths(
    *,
    root: pathlib.Path,
    collector: CheckCollector,
) -> None:
    """Report reserved authored paths without assigning them semantics."""

    config_path = runpod_config_file(root)
    try:
        metadata = config_path.lstat()
    except FileNotFoundError:
        collector.add(
            "authored_runpod_config",
            "info",
            f"reserved global configuration path is absent: {config_path}",
            parsed=False,
            consumer=None,
        )
    except OSError as error:
        collector.add(
            "authored_runpod_config",
            "error",
            f"cannot inspect reserved global configuration path: {error}",
        )
    else:
        if config_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            collector.add(
                "authored_runpod_config",
                "error",
                f"reserved global configuration path is not a regular file: "
                f"{config_path}",
            )
        else:
            collector.add(
                "authored_runpod_config",
                "info",
                f"reserved global configuration exists but has no consumer: "
                f"{config_path}",
                parsed=False,
                consumer=None,
            )

    definitions_root = volume_root(root)
    try:
        metadata = definitions_root.lstat()
    except FileNotFoundError:
        collector.add(
            "authored_volume_configs",
            "info",
            f"reserved volume-definition directory is absent: "
            f"{definitions_root}",
            parsed=False,
            consumer=None,
            file_count=0,
        )
        return
    except OSError as error:
        collector.add(
            "authored_volume_configs",
            "error",
            f"cannot inspect reserved volume-definition directory: {error}",
        )
        return
    if definitions_root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        collector.add(
            "authored_volume_configs",
            "error",
            f"reserved volume-definition path is not a real directory: "
            f"{definitions_root}",
        )
        return
    try:
        definitions = sorted(definitions_root.glob("*.toml"))
        invalid = [
            str(path)
            for path in definitions
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode)
        ]
    except OSError as error:
        collector.add(
            "authored_volume_configs",
            "error",
            f"cannot inspect reserved volume definitions: {error}",
        )
        return
    if invalid:
        collector.add(
            "authored_volume_configs",
            "error",
            "reserved volume definitions include non-regular files",
            paths=invalid,
        )
        return
    collector.add(
        "authored_volume_configs",
        "info",
        f"{len(definitions)} reserved volume definition(s) exist but have "
        "no consumer",
        parsed=False,
        consumer=None,
        file_count=len(definitions),
    )


def _check_local_configuration(
    *,
    state: StateStore,
    profiles: ProfileStore,
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
    _check_reserved_authored_paths(root=profiles.root, collector=collector)
    try:
        authored_profiles = profiles.list()
        collector.add(
            "profiles",
            "ok" if authored_profiles else "warning",
            (
                f"{len(authored_profiles)} authored host profile(s) validate"
                if authored_profiles
                else f"no authored host profiles exist under {profiles.root}"
            ),
            root=str(profiles.root),
        )
        for profile in authored_profiles:
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
    invalid_instances = 0
    now = datetime.datetime.now(datetime.timezone.utc)
    for scanned in InstanceStore(state).scan():
        if scanned.error is not None:
            invalid_instances += 1
            collector.error(
                f"instance_record_{scanned.name}",
                scanned.error,
            )
            continue
        if scanned.value is None:
            continue
        record = scanned.value
        instances.append(record)
        reasons = lease_expiry_reasons(record, now=now)
        if reasons:
            collector.add(
                f"lease_{record['name']}",
                "warning",
                f"instance {record['name']} is overdue: "
                + ", ".join(reasons),
            )
    collector.add(
        "instances",
        "error" if invalid_instances else "ok",
        (
            f"{len(instances)} local instance receipt(s) validate; "
            f"{invalid_instances} are invalid"
            if invalid_instances
            else f"{len(instances)} local instance receipt(s) validate"
        ),
    )

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


def _check_claim_state(
    *,
    state: StateStore,
    instances: list[dict[str, Any]],
    collector: CheckCollector,
) -> None:
    """Audit claim journals, ledgers, exact receipts, and retirement debt."""

    now = datetime.datetime.now(datetime.timezone.utc)
    instances_by_name = {
        instance["name"]: instance for instance in instances
    }
    ledger_store = ClaimStore(state)
    ledger_scans = ledger_store.scan()
    ledger_record_names = {scanned.name for scanned in ledger_scans}
    ledgers: dict[str, dict[str, Any]] = {}
    invalid_ledgers = 0
    for scanned in ledger_scans:
        if scanned.error is not None:
            invalid_ledgers += 1
            collector.error(
                f"claim_ledger_{scanned.name}",
                scanned.error,
            )
            continue
        if scanned.value is None:
            continue
        ledger = scanned.value
        ledgers[ledger["host_name"]] = ledger
        host = instances_by_name.get(ledger["host_name"])
        if host is None:
            collector.add(
                f"claim_receipt_{ledger['host_name']}",
                "error" if ledger["claims"] else "warning",
                (
                    f"current claim ledger {ledger['host_name']} has no valid "
                    "instance receipt"
                    if ledger["claims"]
                    else f"historical empty claim ledger "
                    f"{ledger['host_name']} has no current instance receipt"
                ),
            )
            continue
        try:
            attest_claim_ledger_receipt(host, ledger)
        except RunpodLocalError as error:
            if ledger["claims"]:
                collector.error(
                    f"claim_receipt_{ledger['host_name']}",
                    error,
                )
            else:
                collector.add(
                    f"claim_receipt_{ledger['host_name']}",
                    "warning",
                    f"empty claim ledger {ledger['host_name']} belongs to a "
                    "historical host operation",
                )
            continue
        if ledger["claims"] and host["phase"] not in {
            "provisioning",
            "active",
        }:
            collector.add(
                f"claim_live_host_{ledger['host_name']}",
                "error",
                f"unclaimable host {ledger['host_name']} in phase "
                f"{host['phase']} still has {len(ledger['claims'])} current "
                "claim(s)",
            )
        expired_claim_ids = [
            claim["claim_id"]
            for claim in ledger["claims"]
            if now >= parse_utc_timestamp(claim["renewal_deadline"])
        ]
        if expired_claim_ids:
            collector.add(
                f"claim_expiry_{ledger['host_name']}",
                "warning",
                f"{len(expired_claim_ids)} claim(s) await expiry sweep on "
                f"{ledger['host_name']}",
                claim_ids=expired_claim_ids,
            )
        quarantine = ledger["quarantine"]
        if (
            quarantine is not None
            and host["phase"] not in TERMINAL_INSTANCE_PHASES
        ):
            manual = ledger["retention"]["mode"] == "manual"
            collector.add(
                f"claim_quarantine_{ledger['host_name']}",
                "error" if manual else "warning",
                (
                    f"manual host {ledger['host_name']} is unsafe for reuse; "
                    "expired-claim consumer cleanup is unproven and the exact "
                    "host operation must be retired"
                    if manual
                    else f"while-claimed host {ledger['host_name']} blocks "
                    "new admission until its exact quarantined operation is "
                    "retired"
                ),
                claim_ids=quarantine["claim_ids"],
                host_operation_id=ledger["host_operation_id"],
                manual_action_required=manual,
            )
        retire_at = ledger.get("retire_at")
        if (
            not ledger["claims"]
            and isinstance(retire_at, str)
            and now >= parse_utc_timestamp(retire_at)
            and host["phase"] not in TERMINAL_INSTANCE_PHASES
        ):
            collector.add(
                f"claim_retirement_{ledger['host_name']}",
                "warning",
                f"empty host {ledger['host_name']} is due for retirement",
                retire_at=retire_at,
            )
    collector.add(
        "claim_ledgers",
        "error" if invalid_ledgers else "ok",
        (
            f"{len(ledgers)} host claim ledger(s) validate; "
            f"{invalid_ledgers} are invalid"
            if invalid_ledgers
            else f"{len(ledgers)} host claim ledger(s) validate"
        ),
    )

    for host in instances:
        if (
            host["phase"] not in {"provisioning", "active"}
            or not isinstance(host.get("pod_id"), str)
            or host["retention"]["mode"] != "while-claimed"
            or host["name"] in ledger_record_names
        ):
            continue
        due_at = parse_utc_timestamp(
            host["created_at"]
        ) + datetime.timedelta(
            seconds=host["retention"]["empty_grace_seconds"]
        )
        due = now >= due_at
        collector.add(
            f"claim_orphan_{host['name']}",
            "warning",
            (
                f"while-claimed host {host['name']} has no claim ledger and "
                + ("is due for recovery retirement" if due else "awaits recovery")
            ),
            due=due,
            retire_at=utc_timestamp(due_at),
        )

    acquisition_store = ClaimAcquisitionStore(state)
    acquisitions = []
    invalid_acquisitions = 0
    for scanned in acquisition_store.scan():
        if scanned.error is not None:
            invalid_acquisitions += 1
            collector.error(
                f"claim_acquisition_{scanned.name}",
                scanned.error,
            )
            continue
        if scanned.value is not None:
            acquisitions.append(scanned.value)
    acquisitions_by_owner = {
        (
            acquisition["owner_system"],
            acquisition["owner_instance"],
            acquisition["owner_operation_id"],
        ): acquisition
        for acquisition in acquisitions
    }

    def claim_matches_acquisition(
        claim: dict[str, Any],
        acquisition: dict[str, Any],
    ) -> bool:
        return (
            claim["request_sha256"] == acquisition["request_sha256"]
            and claim["owner_system"] == acquisition["owner_system"]
            and claim["owner_instance"] == acquisition["owner_instance"]
            and claim["owner_operation_id"]
            == acquisition["owner_operation_id"]
        )

    for acquisition in acquisitions:
        identifier = acquisition["record_name"]
        target = acquisition["target"]
        host_binding = acquisition["host"]
        claim_binding = acquisition["claim"]
        claim_closure = acquisition["claim_closure"]
        host = (
            instances_by_name.get(target["host_name"])
            if target is not None
            else None
        )

        if claim_closure is not None:
            ledger = (
                ledgers.get(claim_binding["host_name"])
                if claim_binding is not None
                else None
            )
            historical = (
                [
                    claim
                    for claim in ledger["closed_claims"]
                    if claim["claim_id"] == claim_binding["claim_id"]
                ]
                if ledger is not None and claim_binding is not None
                else []
            )
            if historical:
                closed_claim = historical[0]
                if (
                    len(historical) != 1
                    or not claim_matches_acquisition(
                        closed_claim,
                        acquisition,
                    )
                    or {
                        "reason": closed_claim["reason"],
                        "closed_at": closed_claim["closed_at"],
                        "generation": closed_claim["generation"],
                    }
                    != claim_closure
                ):
                    collector.add(
                        f"claim_acquisition_binding_{identifier}",
                        "error",
                        "historical claim closure differs from its permanent "
                        "acquisition journal",
                    )
            # Absence from the bounded ledger history is valid after pruning;
            # the permanent acquisition closure is the retained terminal
            # representation.
            continue

        if claim_binding is not None:
            ledger = ledgers.get(claim_binding["host_name"])
            current = (
                [
                    claim
                    for claim in ledger["claims"]
                    if claim["claim_id"] == claim_binding["claim_id"]
                ]
                if ledger is not None
                else []
            )
            historical = (
                [
                    claim
                    for claim in ledger["closed_claims"]
                    if claim["claim_id"] == claim_binding["claim_id"]
                ]
                if ledger is not None
                else []
            )
            current_binding = (
                ledger is not None
                and ledger["host_operation_id"]
                == claim_binding["host_operation_id"]
                and ledger["pod_id"] == claim_binding["pod_id"]
                and len(current) == 1
                and claim_matches_acquisition(current[0], acquisition)
            )
            if current_binding:
                exact_host = (
                    host is not None
                    and host_binding is not None
                    and host["operation_id"]
                    == host_binding["host_operation_id"]
                    and host.get("pod_id") == host_binding["pod_id"]
                    and target is not None
                    and host["profile"] == target["profile"]
                )
                if not exact_host:
                    collector.add(
                        f"claim_acquisition_host_{identifier}",
                        "error",
                        "current claim acquisition has lost its exact host "
                        "receipt or profile",
                    )
                continue
            if (
                len(historical) == 1
                and claim_matches_acquisition(
                    historical[0],
                    acquisition,
                )
            ):
                collector.add(
                    f"claim_acquisition_closure_{identifier}",
                    "warning",
                    "closed claim awaits acquisition-journal closure replay",
                )
                continue
            collector.add(
                f"claim_acquisition_binding_{identifier}",
                "error",
                "open claim acquisition does not have one exact current or "
                "recoverable closed ledger binding",
            )
            continue

        if target is None:
            collector.add(
                f"claim_acquisition_recovery_{identifier}",
                "warning",
                "claim acquisition request is durable but has no selected "
                "host target",
            )
            continue
        if host_binding is not None:
            exact_host = (
                host is not None
                and host["operation_id"]
                == host_binding["host_operation_id"]
                and host.get("pod_id") == host_binding["pod_id"]
                and host["profile"] == target["profile"]
            )
            healthy = (
                exact_host
                and host["phase"] in {"provisioning", "active"}
            )
            collector.add(
                f"claim_acquisition_recovery_{identifier}",
                "warning" if healthy else "error",
                (
                    "provider host is bound and awaits claim admission"
                    if healthy
                    else "unclaimed acquisition has lost a claimable exact "
                    "host receipt"
                ),
            )
            continue

        target_is_current = (
            host is not None
            and host["operation_id"] == target["host_operation_id"]
        )
        predecessor_is_current = (
            host is not None
            and target["predecessor_operation_id"] is not None
            and host["operation_id"]
            == target["predecessor_operation_id"]
            and host["phase"] in TERMINAL_INSTANCE_PHASES
        )
        if target_is_current:
            profile_matches = host["profile"] == target["profile"]
            recoverable = host["phase"] in {
                "intent",
                "submitting",
                "provisioning",
                "active",
            }
            collector.add(
                f"claim_acquisition_recovery_{identifier}",
                "warning" if profile_matches and recoverable else "error",
                (
                    "reserved target operation awaits exact host binding"
                    if profile_matches and recoverable
                    else "reserved target operation is terminal, unclaimable, "
                    "or has profile drift"
                ),
            )
        elif predecessor_is_current:
            collector.add(
                f"claim_acquisition_recovery_{identifier}",
                "warning",
                "exact terminal predecessor is retained; reserved target "
                "operation has not started",
            )
        elif (
            host is None
            and target["predecessor_operation_id"] is None
        ):
            collector.add(
                f"claim_acquisition_recovery_{identifier}",
                "warning",
                "reserved target operation has no receipt and awaits launch",
            )
        else:
            collector.add(
                f"claim_acquisition_recovery_{identifier}",
                "error",
                "claim acquisition launch boundary names neither its exact "
                "target nor its recorded predecessor",
            )

    for ledger in ledgers.values():
        for claim in ledger["claims"]:
            owner_key = (
                claim["owner_system"],
                claim["owner_instance"],
                claim["owner_operation_id"],
            )
            acquisition = acquisitions_by_owner.get(owner_key)
            if acquisition is None:
                collector.add(
                    f"claim_journal_{claim['claim_id']}",
                    "error",
                    "current ledger claim has no acquisition journal",
                )
                continue
            binding = acquisition["claim"]
            expected_binding = {
                "host_name": ledger["host_name"],
                "host_operation_id": ledger["host_operation_id"],
                "pod_id": ledger["pod_id"],
                "claim_id": claim["claim_id"],
            }
            if (
                acquisition["request_sha256"] != claim["request_sha256"]
                or (
                    binding is not None
                    and binding != expected_binding
                )
                or acquisition["claim_closure"] is not None
            ):
                collector.add(
                    f"claim_journal_{claim['claim_id']}",
                    "error",
                    "current ledger claim differs from its acquisition journal",
                )
            elif binding is None:
                target = acquisition["target"]
                host_binding = acquisition["host"]
                unselected_candidate = (
                    target is None and host_binding is None
                )
                exact_new_host = (
                    target is not None
                    and target["host_name"] == ledger["host_name"]
                    and target["host_operation_id"]
                    == ledger["host_operation_id"]
                    and target["profile"] == ledger["profile"]
                    and host_binding
                    == {
                        "host_name": ledger["host_name"],
                        "host_operation_id": ledger[
                            "host_operation_id"
                        ],
                        "pod_id": ledger["pod_id"],
                    }
                )
                if unselected_candidate or exact_new_host:
                    collector.add(
                        f"claim_journal_{claim['claim_id']}",
                        "warning",
                        "current ledger claim awaits recoverable acquisition "
                        "journal binding",
                    )
                else:
                    collector.add(
                        f"claim_journal_{claim['claim_id']}",
                        "error",
                        "unbound current claim differs from the acquisition "
                        "journal target or host",
                    )
    collector.add(
        "claim_acquisitions",
        "error" if invalid_acquisitions else "ok",
        (
            f"{len(acquisitions)} claim acquisition journal(s) validate; "
            f"{invalid_acquisitions} are invalid"
            if invalid_acquisitions
            else f"{len(acquisitions)} claim acquisition journal(s) validate"
        ),
    )


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
        submission_may_have_been_sent = (
            phase not in {"intent", "aborted"}
            or isinstance(record.get("submission_started_at"), str)
        )
        pod_id = record.get("pod_id")
        if isinstance(pod_id, str):
            managed_ids.add(pod_id)
        conflict_pod_ids = record.get("conflict_pod_ids") or []
        managed_ids.update(conflict_pod_ids)
        pod = pods_by_id.get(pod_id)
        conflict_id_matches = [
            pods_by_id[conflict_pod_id]
            for conflict_pod_id in conflict_pod_ids
            if conflict_pod_id in pods_by_id
        ]
        name_matches = [
            candidate
            for candidate in pods
            if candidate.get("name") == record["remote_name"]
        ]
        for candidate in name_matches:
            candidate_id = candidate.get("id")
            if (
                submission_may_have_been_sent
                and isinstance(candidate_id, str)
            ):
                managed_ids.add(candidate_id)
        changed_conflict_ids = [
            candidate.get("id")
            for candidate in conflict_id_matches
            if candidate.get("name") != record["remote_name"]
        ]
        if changed_conflict_ids:
            collector.add(
                f"conflict_identity_{record['name']}",
                "error",
                f"recorded conflict identities for {record['name']} changed",
                pod_ids=sorted(str(pod_id) for pod_id in changed_conflict_ids),
            )
        if pod_id is None and phase in (
            {"intent", "submitting", "conflict"} | TERMINAL_INSTANCE_PHASES
        ):
            if len(name_matches) == 1:
                pod = name_matches[0]

        if phase == "active":
            if pod is None:
                collector.add(
                    f"active_pod_{record['name']}",
                    "error",
                    f"active receipt {record['name']} has no live Pod",
                )
            else:
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
                    status = (
                        "warning"
                        if error.code == "pod_not_ready"
                        else "error"
                    )
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
        elif (
            pod is not None or name_matches or conflict_id_matches
        ) and submission_may_have_been_sent and phase in {
            "rolled_back",
            "terminated",
            "aborted",
        }:
            live_pod_ids = sorted(
                {
                    str(candidate.get("id"))
                    for candidate in name_matches + conflict_id_matches
                    if candidate.get("id") is not None
                }
                | ({str(pod.get("id"))} if pod is not None else set())
            )
            collector.add(
                f"terminal_pod_{record['name']}",
                "error",
                f"terminal receipt {record['name']} still has live Pod(s)",
                pod_ids=live_pod_ids,
            )
        elif (
            phase == "aborted"
            and not submission_may_have_been_sent
            and name_matches
        ):
            collector.add(
                f"unsubmitted_collision_{record['name']}",
                "error",
                f"unsubmitted receipt {record['name']} has a name collision",
                pod_ids=sorted(
                    str(candidate.get("id")) for candidate in name_matches
                ),
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
    profiles: ProfileStore,
    credential_store: CredentialStore,
    live: bool,
) -> dict[str, Any]:
    collector = CheckCollector()
    instances, credential = _check_local_configuration(
        state=state,
        profiles=profiles,
        credential_store=credential_store,
        collector=collector,
    )
    _check_claim_state(
        state=state,
        instances=instances,
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
