"""Private-mode copies of tracked inference-service test data.

Git records only executable permission bits. A checkout created with a
group-writable umask can therefore materialize the tracked TOML as mode 0664,
which the production loader correctly rejects. Tests that exercise the loader
use this mode-0600 copy instead of depending on ambient checkout permissions.
"""

from __future__ import annotations

import pathlib
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_SERVICE_FIXTURE = (
    ROOT / "test" / "fixtures" / "model-lab-services"
    / "dense-text-second-service.toml"
)
_FIXTURE_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="model-lab-service-test-fixture-"
)
SERVICE_FIXTURE = pathlib.Path(_FIXTURE_DIRECTORY.name) / SOURCE_SERVICE_FIXTURE.name
SERVICE_FIXTURE.write_bytes(SOURCE_SERVICE_FIXTURE.read_bytes())
SERVICE_FIXTURE.chmod(0o600)
