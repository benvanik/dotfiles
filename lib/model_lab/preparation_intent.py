"""Durable pre-acquire intent closing the provider-claim crash window."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import secrets
import stat
from typing import Any

from .documents import canonical_json_bytes
from .errors import ModelLabError
from .lifecycle import format_timestamp, parse_timestamp, utc_now
from .paths import ensure_private_directory
from .runpod_backend import HostClaimRequest, parse_host_claim_request


PREPARATION_INTENT_SCHEMA = "model-lab.preparation-intent.v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_OPERATION = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclasses.dataclass(frozen=True)
class PreparationIntent:
    service_id: str
    deployment_id: str
    operation_id: str
    workload_sha256: str
    service_sha256: str
    claim_request: HostClaimRequest
    created_at: str

    def normalized(self) -> dict[str, Any]:
        return {
            "schema": PREPARATION_INTENT_SCHEMA,
            "service_id": self.service_id,
            "deployment_id": self.deployment_id,
            "operation_id": self.operation_id,
            "workload_sha256": self.workload_sha256,
            "service_sha256": self.service_sha256,
            "claim_request": self.claim_request.normalized(),
            "created_at": self.created_at,
        }


def parse_preparation_intent(value: Any) -> PreparationIntent:
    fields = {
        "schema",
        "service_id",
        "deployment_id",
        "operation_id",
        "workload_sha256",
        "service_sha256",
        "claim_request",
        "created_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema") != PREPARATION_INTENT_SCHEMA
        or not isinstance(value.get("service_id"), str)
        or not _IDENTIFIER.fullmatch(value["service_id"])
        or not isinstance(value.get("deployment_id"), str)
        or not _OPERATION.fullmatch(value["deployment_id"])
        or not isinstance(value.get("operation_id"), str)
        or not _OPERATION.fullmatch(value["operation_id"])
        or not isinstance(value.get("workload_sha256"), str)
        or not _SHA256.fullmatch(value["workload_sha256"])
        or not isinstance(value.get("service_sha256"), str)
        or not _SHA256.fullmatch(value["service_sha256"])
        or not isinstance(value.get("created_at"), str)
    ):
        raise ModelLabError(
            "preparation intent has unsupported fields or values",
            code="invalid_preparation_intent",
        )
    parse_timestamp(value["created_at"], "preparation intent created_at")
    claim_request = parse_host_claim_request(value["claim_request"])
    if (
        claim_request.operation_id != value["operation_id"]
        or claim_request.owner_system != "model-lab"
        or claim_request.owner_instance != value["service_id"]
    ):
        raise ModelLabError(
            "preparation intent claim request identity is inconsistent",
            code="invalid_preparation_intent",
        )
    return PreparationIntent(
        service_id=value["service_id"],
        deployment_id=value["deployment_id"],
        operation_id=value["operation_id"],
        workload_sha256=value["workload_sha256"],
        service_sha256=value["service_sha256"],
        claim_request=claim_request,
        created_at=value["created_at"],
    )


class PreparationIntentStore:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def path(self, service_id: str) -> pathlib.Path:
        return self.root / "preparation-intents" / f"{service_id}.json"

    def load(self, service_id: str) -> PreparationIntent | None:
        path = self.path(service_id)
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ModelLabError(
                f"cannot open preparation intent: {error}",
                code="unsafe_preparation_intent",
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o077
                or metadata.st_size > 64 * 1024
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise ModelLabError(
                    "preparation intent has an unsafe identity",
                    code="unsafe_preparation_intent",
                )
            payload = os.read(descriptor, 64 * 1024 + 1)
        finally:
            os.close(descriptor)
        if len(payload) > 64 * 1024:
            raise ModelLabError(
                "preparation intent exceeds its size bound",
                code="unsafe_preparation_intent",
            )
        try:
            return parse_preparation_intent(json.loads(payload))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelLabError(
                "preparation intent is not valid JSON",
                code="invalid_preparation_intent",
            ) from error

    def list(self) -> tuple[PreparationIntent, ...]:
        directory = self.root / "preparation-intents"
        try:
            paths = list(directory.iterdir())
        except FileNotFoundError:
            return ()
        intents: list[PreparationIntent] = []
        for path in sorted(paths, key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            if path.suffix != ".json" or not _IDENTIFIER.fullmatch(path.stem):
                raise ModelLabError(
                    f"unexpected preparation-intent entry: {path}",
                    code="unsafe_preparation_intent",
                )
            intent = self.load(path.stem)
            if intent is None:
                raise ModelLabError(
                    "preparation intent disappeared while listing",
                    code="unsafe_preparation_intent",
                )
            intents.append(intent)
        return tuple(intents)

    def begin(
        self,
        *,
        service_id: str,
        workload_sha256: str,
        service_sha256: str,
        claim_request_factory,
    ) -> PreparationIntent:
        current = self.load(service_id)
        if current is not None:
            expected_request = claim_request_factory(current.operation_id)
            if (
                current.workload_sha256 != workload_sha256
                or current.service_sha256 != service_sha256
                or not isinstance(expected_request, HostClaimRequest)
                or current.claim_request != expected_request
            ):
                raise ModelLabError(
                    "another preparation intent owns this service",
                    code="preparation_intent_conflict",
                )
            return current
        operation_id = f"model-lab-{secrets.token_hex(16)}"
        claim_request = claim_request_factory(operation_id)
        if (
            not isinstance(claim_request, HostClaimRequest)
            or claim_request.operation_id != operation_id
            or claim_request.owner_system != "model-lab"
            or claim_request.owner_instance != service_id
        ):
            raise ModelLabError(
                "preparation claim request factory returned a different identity",
                code="invalid_preparation_intent",
            )
        intent = PreparationIntent(
            service_id=service_id,
            deployment_id=f"deployment-{secrets.token_hex(16)}",
            operation_id=operation_id,
            workload_sha256=workload_sha256,
            service_sha256=service_sha256,
            claim_request=claim_request,
            created_at=format_timestamp(utc_now()),
        )
        directory = ensure_private_directory(self.root / "preparation-intents")
        temporary = directory / f".{service_id}.{secrets.token_hex(12)}.tmp"
        payload = canonical_json_bytes(intent.normalized())
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            position = 0
            while position < len(payload):
                position += os.write(descriptor, payload[position:])
            os.fsync(descriptor)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        os.replace(temporary, self.path(service_id))
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return intent

    def complete(self, intent: PreparationIntent) -> None:
        current = self.load(intent.service_id)
        if current is None:
            return
        if current != intent:
            raise ModelLabError(
                "preparation intent changed before completion",
                code="preparation_intent_conflict",
            )
        path = self.path(intent.service_id)
        path.unlink()
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
