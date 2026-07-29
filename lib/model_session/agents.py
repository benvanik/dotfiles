"""Agent-facing operating contract for isolated model sessions."""

from __future__ import annotations


AGENTS_MD = """# `pi` model-session launcher

Each active profile is a `model-session.profile.v3` directory at
`<model-lab-root>/profiles/<profile-id>`. It contains `profile.toml`,
`AGENTS.md`, and optional prompt files. The profile names a model-lab service;
model identity, provider administration, credentials, and service lifetime do
not belong in the profile or this launcher.

The normal user surface is the model-lab front end, which acquires the service
before invoking this launcher and releases its use lease afterward:

```sh
model-lab pi PROFILE
model-lab pi PROFILE resume
model-lab pi PROFILE resume SESSION_ID
```

`model-session --profile DIRECTORY` remains the provider-neutral inner
launcher and diagnostic surface. `new` validates the complete current profile,
materializes an immutable prompt/runtime snapshot, and starts a new isolated
Pi session. `resume` reads only the current profile's stable state route and
launches the exact selected snapshot. Editing a prompt therefore affects new
sessions only. With a terminal and no session ID, `resume` presents a
descriptor-retained numbered picker; automation passes the exact session ID
and acquires only that run, so a malformed sibling cannot block it.

The launcher admits no arbitrary Pi arguments. It pins Pi 0.82.1, the locked
provider/model/tools/prompts, offline mode, the locked policy extension, and
the outer session ID. The model-facing sandbox receives `/workspace`,
read-only `/project` with only its own report/memory overlays writable,
private `/sessions`, private temporary/config/home filesystems, the admitted
inference Unix socket, and the exact Pi/runtime snapshot. It receives no
Runpod controls, cloud credentials, SSH authority, host network, or host
configuration.

Model-lab must publish a matching short-lived, service-scoped endpoint at
`$XDG_RUNTIME_DIR/model-lab/services/<service-id>.{json,sock}` before launch.
It owns model placement, service and tunnel setup, provider billing lifetime,
and shutdown; this launcher neither holds provider credentials nor mutates
provider resources.

`status --json` emits `model-session.history.v1`. Display titles are
non-authoritative metadata, and malformed per-run Pi history is reported as
`history_error` without poisoning healthy siblings. A session ID selected from
a live history catalog, its exclusive run-directory lease, and the sandbox
plan remain retained for the entire child lifetime.
"""
