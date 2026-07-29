"""Runtime dependency construction kept outside CLI parsing and schemas."""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Callable, Sequence

from .supervisor_client import (
    PiLeaseChannel,
    SupervisorClient,
    subprocess_model_session,
)


@dataclasses.dataclass(frozen=True)
class Dependencies:
    supervisor: SupervisorClient
    run_model_session: Callable[
        [pathlib.Path, Sequence[str], PiLeaseChannel],
        int,
    ] = subprocess_model_session


def build_dependencies(
    *,
    authored_root: pathlib.Path,
    state_root: pathlib.Path,
    runtime_root: pathlib.Path,
) -> Dependencies:
    return Dependencies(
        supervisor=SupervisorClient(
            authored_root=authored_root,
            state_root=state_root,
            runtime_root=runtime_root,
        )
    )
