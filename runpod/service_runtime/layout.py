"""Fixed remote paths owned by the generic service runtime."""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from runpod_local.errors import RunpodLocalError


REMOTE_SESSION_ROOT = pathlib.PurePosixPath("/root/runpod-session")
REMOTE_WORKSPACE_ROOT = pathlib.PurePosixPath("/workspace")
REMOTE_SERVICES_ROOT = REMOTE_SESSION_ROOT / "services"
REMOTE_SNAPSHOTS_ROOT = REMOTE_SESSION_ROOT / "model-snapshots"
REMOTE_RUNTIME_CONTROL_ROOT = REMOTE_SESSION_ROOT / "control" / "runtime-verifier"
REMOTE_IMPLEMENTATIONS_ROOT = (
    REMOTE_SESSION_ROOT / "control" / "inference-service-runtime"
)


@dataclass(frozen=True)
class ServicePaths:
    """Canonical paths for one service instance."""

    service_root: pathlib.PurePosixPath
    manifest: pathlib.PurePosixPath
    process_state: pathlib.PurePosixPath
    service_log: pathlib.PurePosixPath
    lifecycle_lock: pathlib.PurePosixPath
    serving_lock: pathlib.PurePosixPath
    setup_receipt: pathlib.PurePosixPath
    snapshot_root: pathlib.PurePosixPath
    snapshot_receipt: pathlib.PurePosixPath


def canonical_service_paths(
    *,
    service_id: str,
    closure_sha256: str,
) -> ServicePaths:
    service_root = REMOTE_SERVICES_ROOT / service_id
    snapshot_root = REMOTE_SNAPSHOTS_ROOT / closure_sha256
    return ServicePaths(
        service_root=service_root,
        manifest=service_root / "deployment.json",
        process_state=service_root / "process.json",
        service_log=service_root / "service.log",
        lifecycle_lock=service_root / "lifecycle.lock",
        serving_lock=service_root / "serving.lock",
        setup_receipt=service_root / "setup.json",
        snapshot_root=snapshot_root,
        snapshot_receipt=REMOTE_SNAPSHOTS_ROOT / f"{closure_sha256}.stage.json",
    )


@dataclass(frozen=True)
class RuntimeLayout:
    """Translate canonical remote roots to test roots without manifest input."""

    session_root: pathlib.Path = pathlib.Path("/root/runpod-session")
    workspace_root: pathlib.Path = pathlib.Path("/workspace")

    def localize(self, canonical: pathlib.PurePosixPath | str) -> pathlib.Path:
        path = pathlib.PurePosixPath(canonical)
        if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
            raise RunpodLocalError(
                "runtime path is not absolute and normalized",
                code="invalid_service_runtime_path",
            )
        if path == REMOTE_SESSION_ROOT or REMOTE_SESSION_ROOT in path.parents:
            relative = path.relative_to(REMOTE_SESSION_ROOT)
            return self.session_root.joinpath(*relative.parts)
        if path == REMOTE_WORKSPACE_ROOT or REMOTE_WORKSPACE_ROOT in path.parents:
            relative = path.relative_to(REMOTE_WORKSPACE_ROOT)
            return self.workspace_root.joinpath(*relative.parts)
        raise RunpodLocalError(
            f"runtime path is outside owned roots: {path}",
            code="invalid_service_runtime_path",
        )

    def service_paths(
        self,
        *,
        service_id: str,
        closure_sha256: str,
    ) -> tuple[ServicePaths, dict[str, pathlib.Path]]:
        canonical = canonical_service_paths(
            service_id=service_id,
            closure_sha256=closure_sha256,
        )
        localized = {
            field: self.localize(getattr(canonical, field))
            for field in canonical.__dataclass_fields__
        }
        return canonical, localized
