# Runpod local control plane

These commands turn short-lived Secure Cloud Pods plus persistent model-cache
volumes into a conservative local workflow. They are built for arbitrary
Hugging Face checkpoints, raw NVIDIA access, compiler/performance work, and
private SSH-tunneled services—not hosted inference.

Every command supports `--help`; every top-level `runpod-*` command supports
`--agents-md`; state-producing/reporting commands support stable versioned
`--json`.

## Current boundary

The tools own:

- exact Hugging Face revision/checkpoint inspection;
- conservative weight/KV/runtime memory estimates;
- model-aware GPU placement;
- live Secure Cloud stock, datacenter, and price inspection;
- private credentials and validated launch profiles;
- crash-reconcilable Pod create/delete with post-create rollback;
- provider-owned hard deadlines and local explicit-heartbeat idle TTLs;
- exact-Pod SSH, loopback tunnels, and persistent/ephemeral file transfer;
- ephemeral Hugging Face credential leasing over reconciled SSH;
- read-only local/live diagnostics.

They do not own Runpod account balance, secrets, SSH account settings, volume
deletion, model correctness, an OCI image, or measured performance truth. The
repository selects and verifies an exact official upstream runtime digest; it
does not build, derive, publish, or distribute that runtime. Its only launch
overlay is a small SSH bootstrap passed as Runpod template configuration.
Model revisions, prompts, launch arguments, and accepted compiled-cache
identities are instantiation state outside this repository. Static placement
produces `candidate`, never `verified`; only a recorded run can establish the
latter.

## Security boundary

`runpod-auth login` reads the API key from a no-echo terminal and stores it at:

```text
~/.config/runpod-local/api-key
```

The directory is mode 0700 and the file is mode 0600. The key is sent only in
an Authorization header. It never appears in URLs, command arguments, launch
profiles, receipts, JSON output, or SSH/SCP child environments.

The official Hugging Face CLI is independently pinned under `~/tools/hf`:

```sh
~/.dotfiles/tools/hf/install.sh 1.24.0
hf auth login
hf auth whoami
```

The wrapper sets `HF_TOKEN_PATH` to
`${XDG_CONFIG_HOME:-~/.config}/huggingface/token`; browser-OAuth refresh state
is stored beside it. Model/Xet caches remain under
`${XDG_CACHE_HOME:-~/.cache}/huggingface`. The local model inspector reads that
same owned, non-symlink, mode-0600 token file for gated metadata requests.
Tokens are never accepted as `hf` wrapper arguments or inherited environment
values.

Profiles inject one validated `SSH_PUBLIC_KEY` and snapshot its matching
private-identity path. That value is a durable profile/receipt identity; the
controller does not treat its presence in the Pod environment as proof that
full-TCP SSH will authorize it. Immediately before a fresh billable Pod create,
the controller reads Runpod's `myself.pubKey` account field and requires one of
its newline-separated keys to match the profile key's exact algorithm and key
body. OpenSSH comments do not participate in identity. The resulting
attestation is one-use and bound to the create payload. A missing or mismatched
account key leaves the receipt in its retryable `intent` phase and sends no Pod
create request.

Runpod deterministically adds its provider-owned `PUBLIC_KEY` field with the
exact `SSH_PUBLIC_KEY` bytes. The outbound payload retains only the requested
environment, while allocation attestation fingerprints the complete effective
environment including that exact mirror. A missing, changed, or additional
provider field still rejects and rolls back the allocation.

Profile creation and every fresh billable submission also reject a missing,
rotated, mismatched, broadly readable, or interactive private key. `PUBLIC_KEY`
remains provider-owned and cannot be supplied as a second authorization
channel. SSH then disables user config, agents, proxies, password
authentication, and forwarding side channels. Each Pod has its own mode-0600
known-hosts file and `runpod-POD_ID` host-key alias. The first connection is
TOFU via `accept-new`; a changed key fails.

Profile values cross the provider/container launch boundary and may later be
serialized by startup tooling or an interactive shell. Profiles therefore
reject controls and shell expansion/quoting characters in every value, reserve
shell-startup and dynamic-loader controls, and reject all Runpod-secret
environment references whose expanded bytes cannot be checked locally.
Complex configuration and credentials belong in ephemeral files, not Pod
environment values.

Pods expose only `22/tcp`. vLLM or another service should bind remote
`127.0.0.1` and be reached through `runpod-tunnel`, which binds both ends to
loopback.

Runpod network volumes are persistent model-cache storage, not a secrets or
prompt-data vault. They are not encrypted by this tool. With a volume-backed
profile, the whole `/workspace` tree persists—not only the model cache:

```text
/workspace/.cache/huggingface
/workspace/.cache/compiled/<explicit accepted cache identity>
```

Use `/root/runpod-session` for private inputs, outputs, logs, and transient
state, including generic Torch, vLLM, and XDG caches. That path is on
container storage and is lost when the Pod is restarted or deleted.
`runpod-copy` admits both roots while rejecting path traversal and shell
syntax. The model cache is operationally writable (Hugging Face needs locks
and metadata); this tool does not pretend the weight files are
filesystem-read-only.

Installed Python environments and unpacked dependency caches do not belong on
the network volume. The network filesystem is suitable for large sequential
model weights, but copying or importing a many-file vLLM environment from it
turns startup into a metadata-bound operation. The runtime boundary is:

- the official `vllm/vllm-openai` image at one immutable digest, distributed
  and cached by its upstream registry and Runpod;
- a configuration-only private Runpod template that replaces the image
  entrypoint with the exact SSH bootstrap under `runpod/bootstrap/ssh`;
- `openssh-server` installed in seconds into ephemeral container storage at
  boot; this is the only package overlay;
- Hugging Face weights on the persistent network volume;
- generic small-file runtime caches on ephemeral local storage;
- only model-specific compiled caches that have already proved a material
  startup benefit retained explicitly on the network volume.

Runpod caches Pod image layers opportunistically, but does not publish a cache
lifetime, host-affinity guarantee, or Pod equivalent of Serverless FlashBoot.
Every image must therefore pass a fresh-host allocated-to-healthy measurement;
a same-host warm start is not sufficient evidence. There is deliberately no
Dockerfile or publisher. The exact upstream identity and in-Pod verification
contract are in
[`runtimes/vllm-cu129/README.md`](runtimes/vllm-cu129/README.md); the launch
overlay is in [`bootstrap/ssh/README.md`](bootstrap/ssh/README.md).

## July 26, 2026 provider snapshot

These quotes and stock states are dated provider observations, not checked-in
account state. Re-run the read-only stock and volume commands before making a
placement decision. The Secure Cloud query returned:

| GPU | VRAM | Global on-demand quote | Stock |
|---|---:|---:|---|
| RTX Pro 6000 Server | 96 GB | $1.99/h | High |
| RTX Pro 6000 Workstation | 96 GB | $1.89/h | Low |
| H200 | 141 GB | $4.39/h | High |
| B200 | 180 GB | $5.89/h | Low |
| B300 | 288 GB | $7.39/h | Low |

`availableGpuCounts` is empty even when Runpod reports High/Low stock. The
client therefore treats High/Medium/Low as the live global signal and treats a
non-empty count list as an additional constraint. The actual created Pod's
GPU, count, datacenter, volume, Secure status, and total hourly price are
authoritative. Missing provider fields leave the receipt in `provisioning`;
rerunning the exact `runpod-up ... --execute` advances it only after all
allocation facts verify.

No current datacenter has all five target offers. Useful current pairings are:

| Datacenter | Current target stock |
|---|---|
| `US-NC-1` | RTX Pro 6000 Server + H200 |
| `EUR-IS-2` | RTX Pro 6000 Server + H200 |
| `EU-RO-1` | RTX Pro 6000 Server/Workstation + B200 |
| `US-NC-2` | RTX Pro 6000 Server + B200 |
| `EU-NL-1` | RTX Pro 6000 Server + B300 |

A network volume pins a Pod to its datacenter. A single cache cannot currently
cover the complete Pro/H200/B200/B300 ladder. A Pro+H200 volume is the clean
initial topology for the main 96-vs-141 GB decision. Add a B200 or B300 cache
only after a measured experiment justifies its recurring cost.

Runpod documents network-volume pricing as $0.07/GB-month through 1 TB and
$0.05/GB-month beyond it. A 250 GB cache is therefore about $17.50/month; a
500 GB cache is about $35/month. Volumes can grow but cannot shrink. Recheck
stock and pricing before creating anything:

```sh
runpod-stock \
  --gpu 'RTX PRO 6000' --gpu H200 --gpu B200 --gpu B300 \
  --available-only --data-centers --json
```

Sources:

- [Runpod GraphQL schema](https://graphql-spec.runpod.io/)
- [Runpod CLI Pod create reference](https://docs.runpod.io/runpodctl/reference/runpodctl-pod)
- [Runpod network volumes](https://docs.runpod.io/storage/network-volumes)
- [Runpod SSH](https://docs.runpod.io/pods/configuration/use-ssh)
- [Runpod Pod templates](https://docs.runpod.io/pods/templates/manage-templates)
- [Runpod template API](https://docs.runpod.io/api-reference/templates/POST/templates)
- [vLLM 0.25.1 Docker deployment](https://docs.vllm.ai/en/v0.25.1/deployment/docker/)
- [vLLM 0.25.1 Dockerfile](https://github.com/vllm-project/vllm/blob/v0.25.1/docker/Dockerfile)

## First setup

On a new machine, authenticate through the no-echo prompt. Never put the key in
shell history, dotfiles, or chat:

```sh
runpod-auth login
runpod-auth status --check --json
runpod-doctor --live
```

For unattended agent-driven sessions, use a dedicated non-interactive Ed25519
identity rather than a general personal key:

```sh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_runpod
chmod 600 ~/.ssh/id_ed25519_runpod
chmod 644 ~/.ssh/id_ed25519_runpod.pub
```

The public key is not secret. The private key never enters dotfiles or Runpod
state. Before the first launch, paste the complete public-key line into the
**SSH Public Keys** field in Runpod account settings. Multiple account keys use
one line each. `runpod-up --execute` verifies the configured profile key there
through a read-only account query; it never changes account SSH settings.

Plan a 250 GB Pro+H200 cache in a datacenter where both offers are live:

```sh
runpod-volume create CACHE_NAME \
  --size-gb 250 --data-center DATA_CENTER_ID --json
```

That command performs only read-only provider checks without `--execute`. It
validates the live datacenter, detects a conflicting/existing volume name, and
reports a dated standard-storage monthly estimate. After review, the same
command with `--execute` serializes same-state-root creation for that name,
creates the persistent paid resource, and verifies the returned
ID/name/size/datacenter. Capture the returned volume ID or recover it with:

```sh
runpod-volume list --json
```

If a create request reports a timeout, inspect that list before retrying. The
tool adopts one exact name/size/datacenter match, but the provider exposes no
cross-machine idempotency key. Volume charges survive every `runpod-down`.
Deletion is intentionally outside this suite; use Runpod's Storage console or
volume API only after separately reviewing the exact volume ID.

Create one private, non-Serverless Runpod template over the exact official
vLLM runtime. The template is configuration only: it contains no environment
values or credentials, allocates no template-local persistent volume, and
passes the repository's SSH bootstrap as the image's sole command argument.
First inspect the exact plan, then execute the same command:

```sh
runpod-template create upstream-vllm-cu129 \
  --runtime vllm-cu129-v0.25.1 --json

runpod-template create upstream-vllm-cu129 \
  --runtime vllm-cu129-v0.25.1 --execute --json
```

Record the returned template ID. Template reconciliation refuses a same-name
contract drift; profile creation and every billable launch independently fetch
and compare the exact image, entrypoint, command, ports, disk, privacy,
Serverless, environment, registry-auth, and volume fields. Public command
output reports Docker argument hashes and sizes rather than their bytes. A
launch repeats template attestation as its final provider read before Pod
creation, then verifies the resolved Pod and deletes it on contradiction.
Runpod exposes no template-version compare-and-swap, so the provider-side
interval between that final read and create cannot be made atomic.

The live REST implementation returns `201 Created` for template creation and
omits empty/false/zero template fields from subsequent GET responses. The
controller accepts both documented `200` and observed `201` creation success,
and normalizes omitted `env`, `isPublic`, `isServerless`, and `volumeInGb` to
their exact empty/false/false/zero states. Nonzero drift remains observable and
fails reconciliation.

Author the datacenter-specific profile against the selected volume:

```sh
runpod-profile create pro-h200 \
  --runtime vllm-cu129-v0.25.1 \
  --template-id TEMPLATE_ID \
  --network-volume-id VOLUME_ID \
  --gpu pro6000-server --gpu h200 \
  --max-hourly 4.50 --ttl 30m --cuda 12.9 \
  --env TORCH_HOME=/root/runpod-session/cache/torch \
  --env VLLM_CACHE_ROOT=/root/runpod-session/cache/vllm \
  --env XDG_CACHE_HOME=/root/runpod-session/cache \
  --identity-file ~/.ssh/id_ed25519_runpod \
  --public-key-file ~/.ssh/id_ed25519_runpod.pub \
  --json
```

New profile defaults cannot exceed 30 minutes. A deliberate longer session is
an explicit launch decision, not durable profile policy.

Profiles fix `HF_TOKEN_PATH` to
`/root/runpod-session/secrets/huggingface/token`, while `HF_HOME` and the model
cache remain on `/workspace`. `HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN` are
rejected even when written as Runpod-secret references; no credential is
stored in the profile. For a gated/private repository, authenticate locally
and lease only the active token after the Pod is ready:

```sh
hf auth login
runpod-hf-auth push compiler
runpod-hf-auth status compiler --json
```

`push` first makes a non-secret SSH connection to establish the dedicated
per-Pod host key, then streams the validated local token file as stdin on a
second connection. That connection starts an absolute isolated system Python
with an empty environment. The fixed remote installer writes atomically with
mode 0600 beneath mode-0700 ephemeral storage. It never reaches argv,
environment, provider metadata, profiles, receipts, JSON, logs, or
`/workspace`. An interrupted atomic-install temporary is treated as unsafe by
`status` and removed by the next valid `push` or `clear`.

Browser-OAuth refresh state is deliberately not copied; push the current token
again if the active token expires. `runpod-hf-auth clear compiler` removes the
Pod copy but does not revoke the source credential at Hugging Face. Code
running as Pod root can read the leased token; the boundary prevents accidental
persistence and disclosure, not access by the selected container workload.
`push` requires an immutable image digest and, for a template-backed Pod, an
exact match among the saved profile/runtime contract and the previously
attested live Pod allocation. It does not perform a fresh template fetch.
`status` and `clear` remain available so an existing lease can always be
inspected or removed.

The profile cap is total Pod cost, not price per GPU. Profile GPU order is
intentional fallback order. Because the volume is datacenter-pinned, putting
unavailable B200/B300 IDs in this profile only adds misleading dead choices;
give each storage topology its own profile.

## Model inspection and placement

Inspect exact serialized bytes and architecture-specific KV memory:

```sh
runpod-model Qwen/Qwen3-32B \
  --context 32768 --sequences 1 --kv-dtype bf16 --json

runpod-model openai/gpt-oss-120b \
  --context 131072 --sequences 1 --kv-dtype bf16 --json
```

Compare GPUs without touching Runpod:

```sh
runpod-place Qwen/Qwen3-32B \
  --gpu pro6000 --gpu h200 --gpu b200 --gpu b300 --json
```

The default envelope is:

- 90% runtime GPU-memory utilization;
- 3% weight-residency slack;
- 4 GiB fixed framework/workspace reserve per GPU;
- exact serialized tensor bytes for `native`;
- an explicitly hypothetical uniform projection for `bf16`, `fp8`, or `q8`.

Unknown KV layouts and multi-GPU partition behavior stay `indeterminate`.
`tight` means weights fit physical VRAM but the requested runtime envelope does
not. `impossible` means the weight basis alone exceeds physical VRAM.

## Launch and lifecycle

`runpod-up` is a read-only plan unless `--execute` is present:

```sh
runpod-up compiler \
  --profile pro-h200 \
  --model Qwen/Qwen3-32B \
  --context 32768 --weight-format native --json
```

Review model fit, exact GPU choice, current quote, volume/datacenter, TTL, and
the resolved Hugging Face commit. Replace `RESOLVED_SHA` below with the plan's
`model.resolved_revision`; pinning the commit prevents `main` from moving
between plan and execution:

```sh
runpod-up compiler \
  --profile pro-h200 \
  --model Qwen/Qwen3-32B \
  --revision RESOLVED_SHA \
  --context 32768 --weight-format native --execute --json
```

The omitted TTL resolves to the profile default, capped at a
provider-enforced 30-minute hard lifetime. It is not a 30-minute idle timeout:
an active session is terminated at the deadline too. A deliberate longer run
requires an explicit `runpod-up --ttl`, which also increases the
lost-controller billing bound. Local idle enforcement is a separate mechanism
described below.

The create path:

1. fixes one absolute deadline from the launch-intent clock and hashes it into
   the exact request;
2. fsyncs that unique local intent before any billable mutation;
3. re-proves the private key against the injected public-key snapshot;
4. submits the GraphQL create mutation at most once;
5. reconciles exactly one UUID-bearing remote name after a crash/timeout;
6. persists the immutable Pod ID;
7. verifies allocation and total actual price;
8. deletes the exact Pod on any contradictory result.

Runpod Pod names are not unique. If a submission response is ambiguous and no
matching Pod is visible yet, the tool does not gamble with a second mutation.
Retry the same `runpod-up ... --execute` later. One exact match is adopted;
multiple matches become a fail-closed conflict with their exact Pod IDs
recorded durably. Provider GraphQL errors remain ambiguous because an error
response does not prove that no allocation occurred.

An `intent` receipt proves no create request has been sent. Its preflight
therefore requires the remote name to be absent; any match is reported as an
unmanaged collision, atomically aborts that operation, and is never adopted or
deleted by it. A retry mints a distinct UUID name. Aborting or expiring any
other unsubmitted intent likewise does not change its ownership proof.

Before the hard deadline, an empty provider result leaves an ambiguous create
in `submitting` and blocks both local-name reuse and a second mutation. At or
after the deadline, an exact absence check may close the receipt because
Runpod's `terminateAfter` owns the hard lifetime. If an exact Pod appears while
that terminal receipt remains current, status reports it and TTL enforcement
deletes it as a terminal leak.

A normal create response can also remain `provisioning` while Runpod populates
machine, price, or port fields. Rerun the same pinned execute command until the
receipt becomes `active`; it does not issue a second create mutation.

Inspect and terminate:

```sh
runpod-status compiler --json
runpod-down compiler --json
runpod-down compiler --execute --json
```

Termination fsyncs deletion intent and re-fetches the exact ID/name before
DELETE. Transient failures remain cleanup-owned and the watcher retries them.
Even a terminal receipt with an exact live-Pod leak can be safely retried with
`runpod-down NAME --execute`. Pod cleanup never stops or deletes the network
volume. A conflict requires explicit `--execute`; every durably recorded Pod ID
must agree with both live ID and name lookups before any member of the set is
deleted. Duplicates first observed during teardown are captured to that exact-ID
set before the first delete. The cleanup authorization is saved with that set,
so a transient partial failure remains watcher-owned and retries without another
create or any volume mutation. If another exact-name Pod ID is discovered, the
tool expands the durable set, revokes the earlier authorization, and requires a
new explicit cleanup after review. TTL enforcement can retry an already
authorized set, but it cannot authorize an expanded set itself.

## TTL enforcement

Hard TTL is absolute, starts at durable intent creation, and includes
credential attestation plus provisioning. Its default is 30 minutes, and
Runpod enforces it from the request's absolute `terminateAfter` even when this
controller disappears. Idle TTL means no explicit local heartbeat. It is not
inferred from GPU utilization, Pi, or vLLM traffic through a tunnel.

```sh
runpod-ttl show compiler --json
runpod-ttl touch compiler --source benchmark_driver --json
runpod-ttl set compiler 20m --json
runpod-ttl extend compiler 5m --json
runpod-ttl enforce --json
runpod-ttl enforce --execute --json
```

`set` changes local total lifetime relative to the original intent; `extend`
moves a previously shortened local deadline. Neither can pass the immutable
provider deadline. Both, and `touch`, mutate local lease state immediately and
do not need `--execute`.

To use idle or deliberately shortened local expiry, start the foreground
watcher in a separate terminal or user-service supervisor on an awake
credentialed machine:

```sh
runpod-ttl watch --execute --interval 30s
```

With `--json`, watcher output is NDJSON: one compact enforcement result per
line. Pending deletes are retried. Every destructive retry rechecks the current
operation ID and current expiry under its instance lock, so a stale scan cannot
delete a refreshed or replacement Pod. This suite does not currently install a
persistent user service for the watcher.

The distinction matters: losing the watcher can miss idle or locally shortened
expiry, but it cannot move the provider-owned hard deadline for a new launch.
An explicit longer `runpod-up --ttl` deliberately increases that failure
exposure. Local locks coordinate processes on one filesystem, not split
controllers on multiple machines. `runpod-status` and `runpod-doctor --live`
expose other Pods as unmanaged here and never auto-delete them. `--max-hourly`
is a rate cap, not a total budget, and excludes recurring volume cost.

## SSH, transfer, and private services

All remote operations re-fetch and validate the exact live Pod first:

```sh
runpod-ssh compiler
runpod-ssh compiler -- nvidia-smi

runpod-ssh compiler -- mkdir -m 700 -p /root/runpod-session
runpod-copy push compiler ./private-input.json \
  /root/runpod-session/private-input.json
runpod-copy pull compiler /root/runpod-session/profile.json ./profile.json

runpod-copy push compiler ./nonsecret-tools /workspace/tools --recursive

runpod-hf-auth push compiler
runpod-hf-auth status compiler --json

runpod-tunnel compiler --local-port 8000 --remote-port 8000
```

`/workspace` is the persistent network volume; `/root/runpod-session` is
ephemeral container storage. Tunnels are foreground-only and loopback-only.
The Hugging Face token is always placed in the latter and is lost with the Pod.
Merely keeping a tunnel open does not refresh idle activity. The benchmark
driver should call `runpod-ttl touch NAME --source LABEL` after real work.

The placement receipt now retains the exact resolved commit, selected index,
checkpoint shard identities, context/KV estimate, and memory policy. A vLLM
launch must consume those values rather than mutable defaults. After the
in-Pod verifier accepts the upstream runtime, the native-safetensors shape is:

```sh
runpod-ssh compiler -- \
  /usr/local/bin/vllm serve Qwen/Qwen3-32B \
  --revision RESOLVED_SHA \
  --max-model-len 32768 \
  --kv-cache-dtype bfloat16 \
  --gpu-memory-utilization 0.90 \
  --load-format safetensors \
  --host 127.0.0.1 --port 8000
```

That foreground SSH command intentionally counts as activity. For meaningful
idle cleanup, launch the server detached with its PID/log under
`/root/runpod-session`, then let the non-heartbeating tunnel plus explicit
benchmark-driver touches own the idle signal. The hard TTL remains the final
bound either way. A genuine Pi-facing 30-minute inactivity contract therefore
belongs in the semantic session layer: completed model requests must emit
heartbeats and a persistent watcher must enforce their absence.

The estimator's `fp8`, `q8`, and other non-native weight formats are hypothetical
uniform projections. They do not select `--quantization`, construct converted
weights, or prove that vLLM can load them. Use `native` until an exact converted
checkpoint and loader contract have been inspected. Multi-GPU runs likewise
need an explicit tensor-parallel setting and remain `indeterminate` until
measured.

The vLLM/CUDA/PyTorch build, image digest, exact CLI, driver commit, warm/cold
cache state, and GPU allocation are benchmark identity. The control plane does
not silently install or upgrade that runtime.

## State and recovery

Private local state defaults to:

```text
~/.local/runpod/
├── cache/huggingface/       exact metadata cache
├── profiles/                non-secret launch policy
├── instances/               intent, receipt, events, leases
├── locks/                   same-host advisory locks
└── ssh/known-hosts/         one host-key file per Pod ID
```

Records and files are mode 0600 beneath mode-0700 directories. Provider state
is authoritative; local state is the reconciliation/audit controller.

The most useful failure checks are:

```sh
runpod-doctor --live --json
runpod-status --json
runpod-ttl enforce --json
```

Every error has a stable code in `--json`. Nothing automatically deletes an
unmanaged Pod, a conflicting identity, or a network volume.
