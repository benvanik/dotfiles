"""The complete model-lab dependency on the generic RunPod control plane.

RunPod owns hosts, provider billing, generic resource claims, and retirement.
Model-lab supplies opaque resource requirements and owns all interpretation of
models, Hugging Face, vLLM, caches, and inference endpoints.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .errors import ModelLabError


@dataclasses.dataclass(frozen=True)
class HostClaimRequest:
    owner_system: str
    owner_instance: str
    operation_id: str
    host_name: str | None
    allowed_profile_names: tuple[str, ...]
    create_if_missing: bool
    mode: str
    gpu_device_count: int
    gpu_memory_bytes: int
    cpu_count: int
    memory_bytes: int
    ephemeral_disk_bytes: int
    endpoint_names: tuple[str, ...]
    minimum_remaining_seconds: int
    acquisition_timeout_seconds: int
    acquisition_expires_at: str | None
    renewal_ttl_seconds: int
    new_host_hard_ttl_seconds: int
    new_host_retention: str

    def normalized(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["allowed_profile_names"] = list(self.allowed_profile_names)
        value["endpoint_names"] = list(self.endpoint_names)
        return value


def parse_host_claim_request(value: Any) -> HostClaimRequest:
    fields = {field.name for field in dataclasses.fields(HostClaimRequest)}
    if not isinstance(value, dict) or set(value) != fields:
        raise ModelLabError(
            "preparation claim request has unsupported fields",
            code="invalid_preparation_intent",
        )
    try:
        request = HostClaimRequest(
            **{
                **value,
                "allowed_profile_names": tuple(value["allowed_profile_names"]),
                "endpoint_names": tuple(value["endpoint_names"]),
            }
        )
    except (TypeError, ValueError) as error:
        raise ModelLabError(
            "preparation claim request is malformed",
            code="invalid_preparation_intent",
        ) from error
    if request.normalized() != value:
        raise ModelLabError(
            "preparation claim request is not canonical",
            code="invalid_preparation_intent",
        )
    return request


@dataclasses.dataclass(frozen=True)
class HostClaim:
    host_name: str
    claim_id: str
    generation: int
    operation_id: str
    provider_resource_id: str
    profile_name: str
    remote_root: str
    endpoints: Mapping[str, int]
    hard_expires_at: str


@dataclasses.dataclass(frozen=True)
class ClaimReleaseResult:
    host_name: str
    claim_id: str
    released: bool
    final_claim: bool
    retirement: str
    empty_deadline: str | None


@runtime_checkable
class HostControl(Protocol):
    """Duck-typed generic host facade supplied by ``runpod_local``."""

    def acquire(
        self,
        request: HostClaimRequest,
        *,
        startup_deadline: float,
        cleanup_deadline_factory: Callable[[], float] | None = None,
    ) -> HostClaim: ...

    def wait_ready(
        self,
        claim: HostClaim,
        *,
        renewal_ttl_seconds: int,
        startup_deadline: float,
    ) -> HostClaim:
        """Attest provider routing and SSH for the exact claimed operation."""
        ...

    def find(self, request: HostClaimRequest) -> HostClaim | None: ...

    def cancel(
        self,
        request: HostClaimRequest,
        *,
        cleanup_deadline: float | None = None,
    ) -> None: ...

    def renew(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        renewal_ttl_seconds: int,
        *,
        startup_deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> HostClaim: ...

    def release(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        *,
        now: bool = False,
        cleanup_deadline: float | None = None,
    ) -> ClaimReleaseResult: ...

    def get(
        self,
        host_name: str,
        claim_id: str,
        *,
        startup_deadline: float | None = None,
    ) -> HostClaim: ...

    def list(self, host_name: str | None = None) -> Sequence[HostClaim]: ...

    def status(self, host_name: str) -> object: ...

    def enforce_retirement(self, *, execute: bool) -> object: ...
