"""Service-scoped model-lab endpoint publication and admission."""

from __future__ import annotations

import datetime
import os
import pathlib
import re
import secrets
from collections.abc import Mapping
from typing import Any

from .attachment import (
    MAX_ATTACHMENT_TTL_SECONDS,
    SERVICE_ENDPOINT_SCHEMA,
    SERVICE_WORKLOAD_SCHEMA,
    Clock,
    ServiceEndpoint,
    ServiceEndpointBinding,
    ServiceWorkload,
    _AttachmentDirectories,
    _BOOT_ID_PATTERN,
    _HASH_PATTERN,
    _PUBLICATION_ID_PATTERN,
    _SERVICE_BINDING_KEYS,
    _SERVICE_RECEIPT_KEYS,
    _SERVICE_WORKLOAD_KEYS,
    _attachment_lock,
    _canonical_json_bytes,
    _clock_time,
    _contract,
    _fail,
    _format_timestamp,
    _normalized_absolute_path,
    _open_private_child_directory,
    _open_runtime_root,
    _parse_timestamp,
    _read_boot_id,
    _read_receipt,
    _receipt_nonnegative_integer,
    _entry_metadata,
    _sha256,
    _validate_identifier,
    _validate_runtime_root_separation,
    _validate_socket,
    _validate_ttl,
    _write_atomic_receipt,
)
from .errors import ModelSessionError
from .profile import (
    COMMIT_PATTERN,
    INPUT_MODALITIES,
    KV_CACHE_DTYPES,
    PROFILE_SCHEMA_V3,
    PROVIDER_PATTERN,
    REPOSITORY_PATTERN,
    WEIGHT_FORMATS,
    Profile,
    ProfileContract,
)

_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$")
_RUNTIME_COMPATIBILITY_PATTERN = re.compile(r"^[a-z][a-z0-9._+-]{0,255}$")
_MAX_SERVICE_TOKENS = 2**24


def _service_runtime_root_path(
    runtime_root: os.PathLike[str] | str | None,
) -> pathlib.Path:
    if runtime_root is None:
        runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_directory is None:
            _fail(
                "XDG_RUNTIME_DIR is required for model-lab service endpoints",
                code="service_endpoint_runtime_unavailable",
            )
        base = _normalized_absolute_path(
            runtime_directory,
            label="XDG_RUNTIME_DIR",
            code="unsafe_service_endpoint_state",
        )
        path = base / "model-lab"
    else:
        path = _normalized_absolute_path(
            runtime_root,
            label="service endpoint runtime_root",
            code="unsafe_service_endpoint_state",
        )
    if path in {
        pathlib.Path("/"),
        pathlib.Path("/home"),
        pathlib.Path("/mnt"),
        pathlib.Path("/run"),
        pathlib.Path("/tmp"),
        pathlib.Path("/var"),
    }:
        _fail(
            "service endpoint runtime_root is dangerously broad",
            code="unsafe_service_endpoint_state",
        )
    return path


def _service_directories(
    runtime_root: pathlib.Path,
    *,
    create: bool,
) -> _AttachmentDirectories | None:
    runtime_descriptor = _open_runtime_root(runtime_root, create=create)
    if runtime_descriptor is None:
        return None
    services = runtime_root / "services"
    locks = services / ".locks"
    try:
        services_descriptor = _open_private_child_directory(
            runtime_descriptor,
            name="services",
            path=services,
            label="model-lab service endpoint directory",
            create=create,
        )
    finally:
        os.close(runtime_descriptor)
    if services_descriptor is None:
        return None
    try:
        locks_descriptor = _open_private_child_directory(
            services_descriptor,
            name=".locks",
            path=locks,
            label="model-lab service endpoint lock directory",
            create=create,
        )
        return _AttachmentDirectories(
            attachments_path=services,
            attachments_descriptor=services_descriptor,
            locks_path=locks,
            locks_descriptor=locks_descriptor,
        )
    except BaseException:
        os.close(services_descriptor)
        raise


def _service_string(
    value: Any,
    *,
    label: str,
    maximum_bytes: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        _fail(f"{label} is invalid", code="invalid_service_endpoint")
    return value


def _service_positive_integer(
    value: Any,
    *,
    label: str,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or (maximum is not None and value > maximum)
    ):
        _fail(f"{label} is invalid", code="invalid_service_endpoint")
    return value


def _service_modalities(
    value: Any,
    *,
    label: str,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        _fail(
            f"{label} must be a nonempty array",
            code="invalid_service_endpoint",
        )
    modalities: list[str] = []
    for modality in value:
        if not isinstance(modality, str) or modality not in INPUT_MODALITIES:
            _fail(
                f"{label} entries must be text or image",
                code="invalid_service_endpoint",
            )
        if modality in modalities:
            _fail(
                f"{label} contains duplicate entry {modality!r}",
                code="invalid_service_endpoint",
            )
        modalities.append(modality)
    if "text" not in modalities:
        _fail(
            f"{label} must include text",
            code="invalid_service_endpoint",
        )
    return tuple(modalities)


def parse_service_workload(value: Any) -> ServiceWorkload:
    """Parse one exact semantic workload contract."""

    if not isinstance(value, Mapping) or set(value) != _SERVICE_WORKLOAD_KEYS:
        _fail(
            "service endpoint workload has invalid fields",
            code="invalid_service_endpoint",
        )
    repository = _service_string(
        value["repository"],
        label="service workload repository",
        maximum_bytes=193,
    )
    if not REPOSITORY_PATTERN.fullmatch(repository):
        _fail(
            "service workload repository must be an exact owner/name",
            code="invalid_service_endpoint",
        )
    revision = _service_string(
        value["revision"],
        label="service workload revision",
        maximum_bytes=40,
    ).lower()
    if not COMMIT_PATTERN.fullmatch(revision):
        _fail(
            "service workload revision must be an immutable 40-hex commit",
            code="invalid_service_endpoint",
        )
    provider = _service_string(
        value["provider"],
        label="service workload provider",
        maximum_bytes=63,
    )
    if not PROVIDER_PATTERN.fullmatch(provider):
        _fail(
            "service workload provider is invalid",
            code="invalid_service_endpoint",
        )
    model_id = _service_string(
        value["model_id"],
        label="service workload model_id",
        maximum_bytes=256,
    )
    if not _MODEL_ID_PATTERN.fullmatch(model_id):
        _fail(
            "service workload model_id is invalid",
            code="invalid_service_endpoint",
        )
    context_tokens = _service_positive_integer(
        value["context_tokens"],
        label="service workload context_tokens",
        maximum=_MAX_SERVICE_TOKENS,
    )
    max_output_tokens = _service_positive_integer(
        value["max_output_tokens"],
        label="service workload max_output_tokens",
        maximum=_MAX_SERVICE_TOKENS,
    )
    if max_output_tokens > context_tokens:
        _fail(
            "service workload max_output_tokens exceeds context_tokens",
            code="invalid_service_endpoint",
        )
    weight_format = _service_string(
        value["weight_format"],
        label="service workload weight_format",
        maximum_bytes=63,
    )
    if weight_format not in WEIGHT_FORMATS:
        _fail(
            "service workload weight_format is unsupported",
            code="invalid_service_endpoint",
        )
    kv_cache_dtype = _service_string(
        value["kv_cache_dtype"],
        label="service workload kv_cache_dtype",
        maximum_bytes=4,
    )
    if kv_cache_dtype not in KV_CACHE_DTYPES:
        _fail(
            "service workload kv_cache_dtype is unsupported",
            code="invalid_service_endpoint",
        )
    runtime_compatibility = _service_string(
        value["runtime_compatibility"],
        label="service workload runtime_compatibility",
        maximum_bytes=256,
    )
    if not _RUNTIME_COMPATIBILITY_PATTERN.fullmatch(runtime_compatibility):
        _fail(
            "service workload runtime_compatibility is invalid",
            code="invalid_service_endpoint",
        )
    reasoning = value["reasoning"]
    if not isinstance(reasoning, bool):
        _fail(
            "service workload reasoning must be a boolean",
            code="invalid_service_endpoint",
        )
    if value["schema"] != SERVICE_WORKLOAD_SCHEMA:
        _fail(
            f"service workload schema must be {SERVICE_WORKLOAD_SCHEMA!r}",
            code="invalid_service_endpoint",
        )
    return ServiceWorkload(
        repository=repository,
        revision=revision,
        provider=provider,
        model_id=model_id,
        context_tokens=context_tokens,
        max_output_tokens=max_output_tokens,
        weight_format=weight_format,
        kv_cache_dtype=kv_cache_dtype,
        runtime_compatibility=runtime_compatibility,
        reasoning=reasoning,
    )


def service_workload_identity(workload: ServiceWorkload) -> str:
    """Return the capability-independent semantic workload identity."""

    if not isinstance(workload, ServiceWorkload):
        _fail(
            "service workload identity requires a validated workload",
            code="invalid_service_endpoint",
        )
    return _sha256(_canonical_json_bytes(workload.as_dict()))


def parse_service_endpoint_binding(
    value: Any,
) -> ServiceEndpointBinding:
    """Parse the immutable service requirement stored in a session lock."""

    if not isinstance(value, Mapping) or set(value) != _SERVICE_BINDING_KEYS:
        _fail(
            "service endpoint binding has invalid fields",
            code="invalid_service_endpoint",
        )
    service_id = _validate_identifier(
        value["service_id"],
        label="service_id",
    )
    service_sha256 = value["service_sha256"]
    if not isinstance(service_sha256, str) or not _HASH_PATTERN.fullmatch(
        service_sha256
    ):
        _fail(
            "service endpoint service_sha256 is invalid",
            code="invalid_service_endpoint",
        )
    workload = parse_service_workload(value["workload"])
    workload_sha256 = value["workload_sha256"]
    if (
        not isinstance(workload_sha256, str)
        or not _HASH_PATTERN.fullmatch(workload_sha256)
        or workload_sha256 != service_workload_identity(workload)
    ):
        _fail(
            "service endpoint workload_sha256 is invalid",
            code="invalid_service_endpoint",
        )
    return ServiceEndpointBinding(
        service_id=service_id,
        service_sha256=service_sha256,
        workload=workload,
        workload_sha256=workload_sha256,
        input_modalities=_service_modalities(
            value["input_modalities"],
            label="service endpoint input_modalities",
        ),
    )


def service_endpoint_receipt_path(
    service_id: str,
    *,
    runtime_root: os.PathLike[str] | str | None = None,
) -> pathlib.Path:
    """Return the canonical boot-local receipt path without creating state."""

    identifier = _validate_identifier(service_id, label="service_id")
    return _service_runtime_root_path(runtime_root) / "services" / f"{identifier}.json"


def service_endpoint_socket_path(
    service_id: str,
    *,
    runtime_root: os.PathLike[str] | str | None = None,
) -> pathlib.Path:
    """Return the canonical service socket path without creating state."""

    identifier = _validate_identifier(service_id, label="service_id")
    return _service_runtime_root_path(runtime_root) / "services" / f"{identifier}.sock"


def publish_service_endpoint(
    service_id: str,
    *,
    service_sha256: str,
    workload: ServiceWorkload,
    input_modalities: tuple[str, ...] | list[str],
    ttl_seconds: int,
    socket_path: os.PathLike[str] | str | None = None,
    clock: Clock | None = None,
    runtime_root: os.PathLike[str] | str | None = None,
) -> ServiceEndpoint:
    """Publish one model-lab-owned, service-scoped endpoint offer."""

    identifier = _validate_identifier(service_id, label="service_id")
    if not isinstance(service_sha256, str) or not _HASH_PATTERN.fullmatch(
        service_sha256
    ):
        _fail(
            "service_sha256 must be an exact lowercase SHA-256",
            code="invalid_service_endpoint",
        )
    if not isinstance(workload, ServiceWorkload):
        _fail(
            "workload must be a validated ServiceWorkload",
            code="invalid_service_endpoint",
        )
    workload = parse_service_workload(workload.as_dict())
    offered_modalities = _service_modalities(
        input_modalities,
        label="service endpoint input_modalities",
    )
    ttl_seconds = _validate_ttl(ttl_seconds)
    resolved_runtime_root = _service_runtime_root_path(runtime_root)
    directories = _service_directories(
        resolved_runtime_root,
        create=True,
    )
    if directories is None:
        raise AssertionError("created service endpoint directory is absent")
    receipt_name = f"{identifier}.json"
    receipt_path = directories.attachments_path / receipt_name
    expected_socket_path = directories.attachments_path / f"{identifier}.sock"
    selected_socket_path = (
        expected_socket_path
        if socket_path is None
        else _normalized_absolute_path(
            socket_path,
            label="service endpoint socket_path",
            code="unsafe_inference_socket",
        )
    )
    if selected_socket_path != expected_socket_path:
        directories.close()
        _fail(
            f"service endpoint socket must be {expected_socket_path}",
            code="unsafe_inference_socket",
        )
    try:
        socket_identity = _validate_socket(selected_socket_path)
        binding = ServiceEndpointBinding(
            service_id=identifier,
            service_sha256=service_sha256,
            workload=workload,
            workload_sha256=service_workload_identity(workload),
            input_modalities=offered_modalities,
        )
        with _attachment_lock(
            directories,
            identifier,
            exclusive=True,
            create=True,
        ):
            socket_identity = _validate_socket(socket_identity.path)
            published_at = _clock_time(clock)
            try:
                admission_expires_at = published_at + datetime.timedelta(
                    seconds=ttl_seconds
                )
            except OverflowError as error:
                raise ModelSessionError(
                    "service endpoint clock cannot represent the admission expiry",
                    code="invalid_inference_attachment_clock",
                ) from error
            payload: dict[str, Any] = {
                "schema": SERVICE_ENDPOINT_SCHEMA,
                "publication_id": secrets.token_hex(16),
                "boot_id": _read_boot_id(),
                **binding.as_dict(),
                "socket_path": str(socket_identity.path),
                "socket_device": socket_identity.device,
                "socket_inode": socket_identity.inode,
                "published_at": _format_timestamp(published_at),
                "admission_expires_at": _format_timestamp(admission_expires_at),
            }
            document = {
                **payload,
                "payload_sha256": _sha256(_canonical_json_bytes(payload)),
            }
            _write_atomic_receipt(
                directories.attachments_descriptor,
                receipt_name,
                receipt_path,
                _canonical_json_bytes(document),
            )
    finally:
        directories.close()
    return ServiceEndpoint(
        publication_id=document["publication_id"],
        binding=binding,
        socket_path=socket_identity.path,
        socket_device=socket_identity.device,
        socket_inode=socket_identity.inode,
        published_at=published_at,
        admission_expires_at=admission_expires_at,
        receipt_path=receipt_path,
    )


def _parse_service_endpoint_receipt(
    value: dict[str, Any],
    *,
    service_id: str,
    receipt_path: pathlib.Path,
    current_time: datetime.datetime | None,
    boot_id: str | None,
    require_live_socket: bool = True,
) -> ServiceEndpoint:
    if set(value) != _SERVICE_RECEIPT_KEYS:
        _fail(
            "service endpoint receipt has invalid fields",
            code="service_endpoint_tampered",
        )
    if value["schema"] != SERVICE_ENDPOINT_SCHEMA:
        _fail(
            f"service endpoint schema must be {SERVICE_ENDPOINT_SCHEMA!r}",
            code="service_endpoint_tampered",
        )
    publication_id = value["publication_id"]
    if not isinstance(publication_id, str) or not _PUBLICATION_ID_PATTERN.fullmatch(
        publication_id
    ):
        _fail(
            "service endpoint publication_id is invalid",
            code="service_endpoint_tampered",
        )
    payload_sha256 = value["payload_sha256"]
    payload = {key: child for key, child in value.items() if key != "payload_sha256"}
    if (
        not isinstance(payload_sha256, str)
        or not _HASH_PATTERN.fullmatch(payload_sha256)
        or _sha256(_canonical_json_bytes(payload)) != payload_sha256
    ):
        _fail(
            "service endpoint payload identity is invalid",
            code="service_endpoint_tampered",
        )
    receipt_boot_id = value["boot_id"]
    if (
        not isinstance(receipt_boot_id, str)
        or not _BOOT_ID_PATTERN.fullmatch(receipt_boot_id)
    ):
        _fail(
            "service endpoint boot identity is invalid",
            code="service_endpoint_tampered",
        )
    if boot_id is not None and receipt_boot_id != boot_id:
        _fail(
            "service endpoint belongs to a different machine boot",
            code="service_endpoint_wrong_boot",
        )
    binding = parse_service_endpoint_binding(
        {key: value[key] for key in _SERVICE_BINDING_KEYS}
    )
    if binding.service_id != service_id:
        _fail(
            "service endpoint belongs to another service",
            code="service_endpoint_mismatch",
        )
    published_at = _parse_timestamp(
        value["published_at"],
        label="published_at",
    )
    admission_expires_at = _parse_timestamp(
        value["admission_expires_at"],
        label="admission_expires_at",
    )
    lifetime = (admission_expires_at - published_at).total_seconds()
    if (
        lifetime <= 0
        or lifetime > MAX_ATTACHMENT_TTL_SECONDS
        or not lifetime.is_integer()
    ):
        _fail(
            "service endpoint admission lifetime is invalid",
            code="service_endpoint_tampered",
        )
    if current_time is not None and current_time < published_at:
        _fail(
            "service endpoint is not yet valid",
            code="service_endpoint_not_yet_valid",
        )
    if current_time is not None and current_time >= admission_expires_at:
        _fail(
            "service endpoint admission lease has expired",
            code="service_endpoint_expired",
        )
    socket_path = value["socket_path"]
    if not isinstance(socket_path, str):
        _fail(
            "service endpoint socket_path is invalid",
            code="service_endpoint_tampered",
        )
    expected_socket_path = receipt_path.parent / f"{binding.service_id}.sock"
    if socket_path != str(expected_socket_path):
        _fail(
            "service endpoint socket_path is not the canonical "
            f"service socket {expected_socket_path}",
            code="service_endpoint_tampered",
        )
    socket_device = _receipt_nonnegative_integer(
        value["socket_device"],
        label="socket_device",
    )
    socket_inode = _receipt_nonnegative_integer(
        value["socket_inode"],
        label="socket_inode",
    )
    resolved_socket_path = pathlib.Path(socket_path)
    if require_live_socket:
        socket_identity = _validate_socket(socket_path)
        if (
            socket_identity.device != socket_device
            or socket_identity.inode != socket_inode
        ):
            _fail(
                "service endpoint socket inode changed after publication",
                code="service_endpoint_unavailable",
            )
        resolved_socket_path = socket_identity.path
    return ServiceEndpoint(
        publication_id=publication_id,
        binding=binding,
        socket_path=resolved_socket_path,
        socket_device=socket_device,
        socket_inode=socket_inode,
        published_at=published_at,
        admission_expires_at=admission_expires_at,
        receipt_path=receipt_path,
    )


def _validate_service_endpoint_receipt(
    value: dict[str, Any],
    *,
    contract: ProfileContract,
    expected_binding: ServiceEndpointBinding | None,
    receipt_path: pathlib.Path,
    current_time: datetime.datetime,
    boot_id: str,
) -> ServiceEndpoint:
    if contract.service_id is None:
        _fail(
            "service endpoints require a profile-v3 contract",
            code="invalid_service_endpoint_binding",
        )
    endpoint = _parse_service_endpoint_receipt(
        value,
        service_id=contract.service_id,
        receipt_path=receipt_path,
        current_time=current_time,
        boot_id=boot_id,
    )
    binding = endpoint.binding
    if contract.service_id is None or contract.endpoint is None:
        _fail(
            "service endpoints require a profile-v3 contract",
            code="invalid_service_endpoint_binding",
        )
    missing_modalities = set(contract.endpoint.required_input_modalities) - set(
        binding.input_modalities
    )
    if missing_modalities:
        _fail(
            "service endpoint lacks required input modalities: "
            f"{', '.join(sorted(missing_modalities))}",
            code="service_endpoint_capability_mismatch",
        )
    if (
        expected_binding is not None
        and expected_binding.service_id != contract.service_id
    ):
        _fail(
            "session service binding belongs to another service",
            code="service_endpoint_mismatch",
        )
    if (
        expected_binding is not None
        and binding.workload_sha256 != expected_binding.workload_sha256
    ):
        _fail(
            "service endpoint workload differs from the session's frozen "
            f"requirement {expected_binding.workload_sha256}",
            code="service_endpoint_workload_mismatch",
        )
    if expected_binding is not None:
        missing_frozen_modalities = set(expected_binding.input_modalities) - set(
            binding.input_modalities
        )
        if missing_frozen_modalities:
            _fail(
                "service endpoint lacks capabilities frozen by the "
                "session: "
                f"{', '.join(sorted(missing_frozen_modalities))}",
                code="service_endpoint_capability_mismatch",
            )

    return endpoint


def revoke_service_endpoint(
    service_id: str,
    expected_publication_id: str,
    *,
    runtime_root: os.PathLike[str] | str | None = None,
) -> None:
    """Revoke exactly one model-lab endpoint receipt.

    The receipt is removed only while holding the service's exclusive
    publication lock and only when its complete, boot-local endpoint identity
    still matches ``expected_publication_id``.  The endpoint socket remains
    owned by the proxy that created it.
    """

    identifier = _validate_identifier(service_id, label="service_id")
    if not isinstance(
        expected_publication_id, str
    ) or not _PUBLICATION_ID_PATTERN.fullmatch(expected_publication_id):
        _fail(
            "expected_publication_id is invalid",
            code="invalid_service_endpoint",
        )
    resolved_runtime_root = _service_runtime_root_path(runtime_root)
    directories = _service_directories(
        resolved_runtime_root,
        create=False,
    )
    if directories is None:
        _fail(
            f"service endpoint publication is missing: {identifier}",
            code="service_endpoint_missing",
        )
    receipt_name = f"{identifier}.json"
    receipt_path = directories.attachments_path / receipt_name
    lock_name = f"{identifier}.lock"
    lock_path = directories.locks_path / lock_name
    try:
        receipt_metadata = _entry_metadata(
            directories.attachments_descriptor,
            receipt_name,
            path=receipt_path,
        )
        lock_metadata = (
            _entry_metadata(
                directories.locks_descriptor,
                lock_name,
                path=lock_path,
            )
            if directories.locks_descriptor is not None
            else None
        )
        if receipt_metadata is None:
            _fail(
                f"service endpoint publication is missing: {identifier}",
                code="service_endpoint_missing",
            )
        if lock_metadata is None:
            _fail(
                "service endpoint receipt exists without its persistent lock",
                code="unsafe_service_endpoint_state",
            )
        with _attachment_lock(
            directories,
            identifier,
            exclusive=True,
            create=False,
        ):
            value, _ = _read_receipt(
                directories.attachments_descriptor,
                receipt_name,
                receipt_path,
            )
            endpoint = _parse_service_endpoint_receipt(
                value,
                service_id=identifier,
                receipt_path=receipt_path,
                current_time=None,
                boot_id=None,
                require_live_socket=False,
            )
            if endpoint.publication_id != expected_publication_id:
                _fail(
                    "service endpoint publication changed before revocation",
                    code="service_endpoint_publication_mismatch",
                )
            try:
                os.unlink(
                    receipt_name,
                    dir_fd=directories.attachments_descriptor,
                )
            except FileNotFoundError as error:
                raise ModelSessionError(
                    "service endpoint publication disappeared during "
                    f"revocation: {receipt_path}",
                    code="service_endpoint_publication_mismatch",
                ) from error
            except OSError as error:
                raise ModelSessionError(
                    f"cannot revoke service endpoint {receipt_path}: {error}",
                    code="service_endpoint_revoke_failed",
                ) from error
            try:
                os.fsync(directories.attachments_descriptor)
            except OSError as error:
                raise ModelSessionError(
                    "service endpoint receipt was removed, but directory "
                    f"durability is unknown for {receipt_path}: {error}",
                    code="service_endpoint_revoke_durability_unknown",
                ) from error
    finally:
        directories.close()


def inspect_service_publication(
    service_id: str,
    *,
    runtime_root: os.PathLike[str] | str | None = None,
) -> ServiceEndpoint | None:
    """Authenticate one retained publication for administrative cleanup.

    Unlike ``load_service_endpoint``, this does not admit a user session. It
    intentionally ignores admission expiry, socket liveness, and machine boot
    age so the service owner can revoke exact retained state after a crash or
    reboot. The canonical receipt, payload hash, service identity, workload,
    capabilities, socket path, and publication identity remain fully
    validated under the service publication lock.
    """

    identifier = _validate_identifier(service_id, label="service_id")
    resolved_runtime_root = _service_runtime_root_path(runtime_root)
    directories = _service_directories(
        resolved_runtime_root,
        create=False,
    )
    if directories is None:
        return None
    receipt_name = f"{identifier}.json"
    receipt_path = directories.attachments_path / receipt_name
    lock_name = f"{identifier}.lock"
    lock_path = directories.locks_path / lock_name
    try:
        receipt_metadata = _entry_metadata(
            directories.attachments_descriptor,
            receipt_name,
            path=receipt_path,
        )
        if receipt_metadata is None:
            return None
        lock_metadata = (
            _entry_metadata(
                directories.locks_descriptor,
                lock_name,
                path=lock_path,
            )
            if directories.locks_descriptor is not None
            else None
        )
        if lock_metadata is None:
            _fail(
                "service endpoint receipt exists without its persistent lock",
                code="unsafe_service_endpoint_state",
            )
        with _attachment_lock(
            directories,
            identifier,
            exclusive=False,
            create=False,
        ):
            value, _ = _read_receipt(
                directories.attachments_descriptor,
                receipt_name,
                receipt_path,
            )
            return _parse_service_endpoint_receipt(
                value,
                service_id=identifier,
                receipt_path=receipt_path,
                current_time=None,
                boot_id=None,
                require_live_socket=False,
            )
    finally:
        directories.close()


def load_service_endpoint(
    profile: Profile | ProfileContract,
    *,
    expected_binding: ServiceEndpointBinding | None = None,
    clock: Clock | None = None,
    runtime_root: os.PathLike[str] | str | None = None,
) -> ServiceEndpoint:
    """Load a compatible service endpoint immediately before a launch."""

    contract = _contract(profile)
    if (
        contract.schema != PROFILE_SCHEMA_V3
        or contract.service_id is None
        or contract.endpoint is None
    ):
        _fail(
            "service-scoped endpoints require profile v3",
            code="invalid_service_endpoint_binding",
        )
    service_id = _validate_identifier(
        contract.service_id,
        label="service_id",
    )
    resolved_runtime_root = _validate_runtime_root_separation(
        contract,
        _service_runtime_root_path(runtime_root),
    )
    directories = _service_directories(
        resolved_runtime_root,
        create=False,
    )
    if directories is None:
        _fail(
            f"service {service_id} is not ready; administrator action: "
            f"model-lab up {service_id}",
            code="service_endpoint_missing",
        )
    receipt_name = f"{service_id}.json"
    receipt_path = directories.attachments_path / receipt_name
    lock_name = f"{service_id}.lock"
    lock_path = directories.locks_path / lock_name
    try:
        receipt_metadata = _entry_metadata(
            directories.attachments_descriptor,
            receipt_name,
            path=receipt_path,
        )
        lock_metadata = (
            _entry_metadata(
                directories.locks_descriptor,
                lock_name,
                path=lock_path,
            )
            if directories.locks_descriptor is not None
            else None
        )
        if receipt_metadata is None:
            _fail(
                f"service {service_id} is not ready; administrator action: "
                f"model-lab up {service_id}",
                code="service_endpoint_missing",
            )
        if lock_metadata is None:
            _fail(
                "service endpoint receipt exists without its persistent lock",
                code="unsafe_service_endpoint_state",
            )
        with _attachment_lock(
            directories,
            service_id,
            exclusive=False,
            create=False,
        ):
            value, _ = _read_receipt(
                directories.attachments_descriptor,
                receipt_name,
                receipt_path,
            )
            return _validate_service_endpoint_receipt(
                value,
                contract=contract,
                expected_binding=expected_binding,
                receipt_path=receipt_path,
                current_time=_clock_time(clock),
                boot_id=_read_boot_id(),
            )
    finally:
        directories.close()
