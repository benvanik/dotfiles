# RunPod host control

`runpod` manages generic, short-lived GPU hosts. It owns the RunPod API
credential, portable host profiles, Pods, network-volume attachment, opaque
resource claims, SSH access, and host retirement.

It deliberately knows nothing about Hugging Face, models, vLLM, prompts,
projects, or Pi. Those semantics belong to the sibling
[`model-lab`](../model-lab/README.md) layer. A manually launched Pod is just as
valid for CUDA development, benchmarking, training, ComfyUI, or an SSH
workstation as it is for model-lab.

Every command has `--help`. The top level and each command family also expose
an agent-oriented contract with `--agents-md`. Machine-readable output uses
versioned `--json` documents.

## Namespaces

The three state classes never overlap:

```text
/mnt/dev/runpod/                   portable authored host policy and evidence
  profiles/                       generic host profiles
  evidence/                       retained human-reviewed observations
  archive/                        superseded authored material

~/.local/state/runpod/             machine-local receipts, locks, and SSH state
  hostclaimops/                    durable owner-operation acquisitions
  migrations/                     source-preserving state-transition receipts
$XDG_RUNTIME_DIR/runpod/           boot-local coordination
~/.config/runpod-local/api-key     private RunPod API credential
~/.ssh/id_ed25519_runpod           dedicated private SSH identity
```

`runpod.toml` and authored volume documents are reserved extension points; the
current implementation does not invent or silently accept schemas for them.
Network volumes themselves are provider resources referenced by exact ID from
a host profile.

The default private modes are 0700 for directories and 0600 for sensitive
files. No credential belongs in `/mnt/dev/runpod`, dotfiles, argv, JSON output,
or a Pod environment. The control plane rejects an inherited
`RUNPOD_API_KEY`; provider authority comes only from the dedicated mode-0600
credential file.

## Setup

Store or verify the API credential through the no-echo prompt:

```sh
runpod auth login
runpod auth status --check
```

The SSH key is intentionally dedicated and non-interactive:

```sh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_runpod
chmod 600 ~/.ssh/id_ed25519_runpod
chmod 644 ~/.ssh/id_ed25519_runpod.pub
```

Add the public key to the RunPod account once. The launch path verifies that
the account key, authored profile snapshot, and local private key still agree
before a billable create request.

Inspect current stock, volumes, and private templates:

```sh
runpod stock --available-only --data-centers
runpod volume list
runpod template list
```

Create operations are plans until `--execute` is present. A template is a tiny
SSH-only launch overlay over an exact upstream image digest:

```sh
runpod template create generic-pytorch-cu129 \
  --image 'runpod/pytorch@sha256:EXACT_DIGEST'

runpod template create generic-pytorch-cu129 \
  --image 'runpod/pytorch@sha256:EXACT_DIGEST' --execute
```

The reviewed overlay in [`bootstrap/ssh`](bootstrap/ssh/README.md) installs
OpenSSH into ephemeral container storage and executes `sshd`. It is not a
derived image and carries no workload packages or secrets.

An authored host profile binds one exact template, GPU choice and capacity,
price ceiling, storage attachment, CUDA compatibility, SSH identity, and hard
TTL:

```sh
runpod profile create pro6000-is1 \
  --template-id TEMPLATE_ID \
  --network-volume-id VOLUME_ID \
  --gpu 'NVIDIA RTX PRO 6000 Blackwell Server Edition' \
  --gpu-count 1 \
  --max-hourly 2.25 \
  --ttl 30m \
  --container-disk-gb 50 \
  --min-vcpu-per-gpu 8 \
  --min-ram-per-gpu 32 \
  --cuda 12.9
```

Profile files contain only generic host facts. In particular they contain no
Hugging Face paths, tokens, model caches, vLLM settings, or service IDs.

## Manual hosts

The direct lifecycle is independent of model-lab:

```sh
runpod up cuda-dev --profile pro6000-is1
runpod up cuda-dev --profile pro6000-is1 --execute

runpod status cuda-dev
runpod ssh cuda-dev
runpod ssh cuda-dev -- nvidia-smi

runpod copy push cuda-dev ./input /root/runpod-session/input
runpod copy pull cuda-dev /root/runpod-session/output ./output

runpod down cuda-dev
runpod down cuda-dev --execute
```

`/workspace` is the persistent network volume. `/root/runpod-session` is
ephemeral container storage. Remote copies are confined to those two trees.
Network-volume deletion is intentionally absent; terminating a Pod preserves
the volume.

Direct `runpod up` hosts use manual retention. A model-lab workload may claim
spare resources on such a host, but releasing that claim cannot terminate the
manually retained host. This is the key separation that allows one machine to
be amortized over independent workstreams.

The provider-side `terminateAfter` deadline is the final billing bound. The
implicit profile hard TTL is at most 30 minutes; a longer explicit `--ttl`
deliberately increases exposure if every local controller disappears.

## Generic claims

A claim reserves opaque capacity on a compatible host. It contains resource
counts and named loopback endpoints, never consumer workload semantics:

```sh
runpod claim list
runpod claim show HOST CLAIM_ID

runpod claim acquire \
  --owner-system example \
  --owner-instance worker-1 \
  --operation-id STABLE_ID \
  --profile pro6000-is1 \
  --mode shared \
  --gpu-memory-gib 24 \
  --cpu-count 8 \
  --memory-gib 32 \
  --ephemeral-disk-gib 20 \
  --acquisition-timeout 5m \
  --endpoint api
```

Mutating claim commands are also plans without `--execute`. Acquisition is
idempotent by exact owner operation ID. Its timeout starts when the durable
acquisition journal is created, so restarting or retrying the owner operation
cannot reset the clock. An owning service may also supply an earlier absolute
expiration; the effective deadline is the earlier boundary. Volume, stock,
template, account-key, Pod-list, create, and allocation-verification calls each
consume only the remaining budget, and no later provider request begins after
expiration. Cleanup and exact Pod deletion remain available after it. A
timeout releases the exact claim immediately and retires its final
`while-claimed` host without empty-host grace; a manually retained host is
never retired by a consumer timeout. Claim generations are compare-and-swap
guards for renew and release:

```sh
runpod claim renew HOST CLAIM_ID --generation N --ttl 2m --execute
runpod claim release HOST CLAIM_ID --generation N --execute
runpod claim release HOST CLAIM_ID --generation N --now --execute
```

A claim may reuse a compatible active host or atomically launch one from the
allowed profiles. A claim-created host uses `while-claimed` retention. Releasing
its final claim starts the configured empty-host grace; `--now` bypasses the
grace and requests immediate exact-host retirement only for a `while-claimed`
host. Claim release, including `--now`, never retires a manually retained host.
That requires the separate operator `runpod down` authority, so a model
consumer cannot kill unrelated work sharing an administratively owned Pod.

Expiry is different from an orderly release. The claim ledger cannot prove
that an opaque consumer stopped its remote processes or removed credentials
when renewal authority vanished. The exact host operation is therefore
durably quarantined and cannot admit another claim. A `while-claimed` host
enters bounded drain: surviving claims remain inspectable and releasable until
their current deadlines, but no claim can be admitted, idempotently reacquired,
or renewed. It applies zero grace after its final claim ends and is exact-
retirement-due. A manually retained host remains visibly unsafe and
quarantined until the operator retires that exact operation; RunPod never
pretends consumer-specific cleanup happened or unquarantines it in place.

When the exact host receipt is already terminal or has been replaced through
the guarded lifecycle boundary, remote survival is no longer possible. The
ledger durably closes every claim as `host-operation-ended`, closes its
acquisition journals, and records the old operation end. That historical
ledger cannot be reused and cannot wedge placement or retirement for
unrelated healthy hosts. A corrupt historical closure journal remains visible,
but it protects only its exact provider operation and cannot shield a proven
replacement that reuses the local host name. A genuinely new exact operation
starts with clean admission state.

The acquisition journal is durable before provider launch. It records whether
the operation created its target or selected already-managed capacity.
Pre-claim cancellation may retire only an exact live target explicitly marked
as acquisition-created; it preserves reused and manually retained hosts. The
v1-to-v2 journal transition keeps a complete private migration receipt before
rewriting the active record and refuses ambiguous open-target ownership. The
claim ledger binds the exact host operation and resource allocation. Recovery
converges against those identities rather than issuing an untracked second
create.

## TTL and retirement

Host hard TTL, generic empty-host grace, and consumer service-idle TTL are
different clocks:

```text
provider hard TTL       absolute maximum lifetime of the Pod
RunPod empty grace      begins after orderly final release; expiry uses zero grace
model-lab service TTL   begins when the final service user exits
```

Only the first two belong here. Inspect or operate the host clock with:

```sh
runpod ttl show HOST
runpod ttl touch HOST --source cuda-benchmark
runpod ttl enforce
runpod ttl enforce --execute
runpod claim enforce
runpod claim enforce --execute
```

The foreground TTL watcher enforces both provider-bounded host TTL and
quarantined/empty `while-claimed` retirement, so claim expiry still retires an
exact host if its consumer supervisor disappears:

```sh
runpod ttl watch --execute --interval 30s
```

Provider hard expiry remains effective if the watcher or this machine
disappears. Tunnel existence is not activity.

## SSH and tunnels

Every remote operation first reconciles the exact live Pod identity and
allocation:

```sh
runpod ssh HOST
runpod ssh HOST -- COMMAND ARGUMENT...

runpod tunnel HOST --local-port 8000 --remote-port 8000
runpod tunnel HOST --local-socket /run/user/$UID/example.sock \
  --remote-port 8000
```

Tunnels are foreground and loopback-only. SSH ignores user config, agents,
proxies, password authentication, and forwarding side channels. Each Pod has
its own private known-hosts record; a changed host key fails closed.

## Failure and recovery

The billable launch path records intent before provider mutation, submits at
most one create request, reconciles exact Pod IDs and names, verifies hardware,
price, storage, template, environment, and hard expiry, and rolls back a
contradictory allocation.

RunPod Pod names are not unique. Ambiguous creates never authorize a second
mutation merely because a response timed out. Termination records the exact
owned Pod IDs before deletion and never adopts or removes an unmanaged name
collision.

The primary audits are:

```sh
runpod status --local-only
runpod doctor
runpod doctor --live
runpod claim list
runpod claim enforce
```

`doctor` checks authored profiles, SSH identities, lifecycle receipts, claim
acquisition journals, claim ledgers, orphaned while-claimed hosts, and due
retirement. It reports a quarantined manual host as an operator error because
generic RunPod control cannot attest consumer process or secret cleanup.
`--live` adds read-only provider reconciliation. It never deletes an unmanaged
Pod or volume.

## Extension boundary

The generic host and claim API is intentionally enough for several future
consumers:

- CUDA development and NVIDIA reference benchmarks;
- finite LoRA or fine-tuning jobs;
- vLLM, llama.cpp, or custom Python services;
- agent swarms and multiple concurrently placed services;
- embedding/ranking/LLM constellations;
- ComfyUI.

Those consumers own their own job, cache, process, endpoint, evidence, and
idle semantics. RunPod continues to own only provider resources and generic
capacity. Multi-model routing or a future fleet manager can therefore grow
above this layer without making a Pod synonymous with one model or project.
