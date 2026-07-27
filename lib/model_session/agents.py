"""Agent-facing operating contract for isolated model sessions."""

from __future__ import annotations


AGENTS_MD = """# `pi` model-session launcher

Each concrete model profile lives outside the dotfiles repository. Its
directory contains `profile.toml`, `AGENTS.md`, optional prompt files, and a
symlink named `pi` to `~/.dotfiles/bin/model-session`. Model-session state,
project memory/reports, the Pi installation, credentials, and provider
administration also remain outside dotfiles.

The intentionally small session interface is:

```sh
./pi
./pi resume
./pi resume SESSION_ID
./pi status
./pi status --json
```

`./pi` (equivalently `./pi new`) validates the complete current profile,
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

An administrator must publish a matching, short-lived inference attachment
before launch. That administration layer owns model placement, service and
tunnel setup, provider billing lifetime, and shutdown; this launcher neither
holds provider credentials nor mutates provider resources.

`status --json` emits `model-session.history.v1`. Display titles are
non-authoritative metadata, and malformed per-run Pi history is reported as
`history_error` without poisoning healthy siblings. A session ID selected from
a live history catalog, its exclusive run-directory lease, and the sandbox
plan remain retained for the entire child lifetime.
"""
