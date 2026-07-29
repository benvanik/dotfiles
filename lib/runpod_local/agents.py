"""Agent-facing contract for the generic Runpod host control plane."""

AGENT_DOCS = {
    "root": """\
# Runpod host control plane

`runpod` owns provider credentials, generic host profiles, Pods, volumes,
claims, SSH endpoints, and host retirement. It has no model, Hugging Face,
vLLM, inference-service, or project-profile semantics; those belong to the
sibling `model-lab` tool.

Authored host configuration lives under `/mnt/dev/runpod`. Machine-local
receipts and locks live under `~/.local/state/runpod`. Boot-local coordination
lives under `$XDG_RUNTIME_DIR/runpod`. Credentials remain separately private
under `~/.config/runpod-local`.

Core commands:

```sh
runpod stock --available-only
runpod volume list
runpod template list
runpod profile list
runpod up HOST --profile PROFILE
runpod status [HOST]
runpod claim list [HOST]
runpod ssh HOST
runpod copy push HOST SOURCE /workspace/DESTINATION
runpod down HOST
runpod ttl enforce
runpod claim enforce
runpod doctor
```

Provider mutations require `--execute`. Local TTL and claim mutations are
explicitly identified by their subcommands. Network volumes are preserved by
host termination and this tool has no volume-delete action.
""",
    "auth": """\
# `runpod auth`

Store, validate, inspect, or remove only the dedicated private Runpod API
credential. Inherited `RUNPOD_API_KEY` authority is rejected; secret bytes
never appear in argv or JSON.
""",
    "stock": """\
# `runpod stock`

Read current provider GPU stock and hourly prices. Filters never mutate
provider state.
""",
    "volume": """\
# `runpod volume`

List/get volumes or reconcile one exact name, size, and datacenter. Creation
requires `--execute`; deletion is intentionally absent.
""",
    "template": """\
# `runpod template`

List/get templates or create a private SSH-only overlay on an immutable
upstream image digest. The checked generic overlay installs OpenSSH; it carries
no model runtime.
""",
    "profile": """\
# `runpod profile`

Author portable generic host policy under `/mnt/dev/runpod/profiles`: exact
GPU IDs and capacities, price cap, immutable image or private template,
storage, SSH identity, and host retention.
""",
    "up": """\
# `runpod up`

Plan or execute one crash-reconcilable generic Pod launch from an authored host
profile. The host may later be shared by independent claims.
""",
    "status": """\
# `runpod status`

Inspect local receipts or reconcile them with exact live Pod identities.
""",
    "down": """\
# `runpod down`

Plan or terminate one exact Pod while preserving its network volume.
""",
    "ttl": """\
# `runpod ttl`

Inspect or enforce provider-bounded host lifetimes. The foreground watcher is
explicit, also enforces quarantined `while-claimed` host retirement, and
provider deletion still requires `--execute`.
""",
    "ssh": """\
# `runpod ssh`

Open a reconciled direct SSH session or execute one literal remote argv.
OpenSSH config, proxies, and agents are disabled.
""",
    "tunnel": """\
# `runpod tunnel`

Open one loopback-only TCP or Unix-socket tunnel to an explicitly selected
remote loopback port. Tunnel existence is not workload activity.
""",
    "copy": """\
# `runpod copy`

Copy through the reconciled SSH endpoint. Remote paths are restricted to
`/workspace` and `/root/runpod-session`.
""",
    "doctor": """\
# `runpod doctor`

Run a read-only integrity audit over generic configuration, receipts, SSH
identities, leases, claim acquisition journals, claim ledgers, orphaned
while-claimed hosts, expired-claim quarantine, due retirement, and optionally
live provider state.
""",
}
