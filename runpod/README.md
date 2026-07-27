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
- hard and explicit-heartbeat idle TTLs;
- exact-Pod SSH, loopback tunnels, and persistent/ephemeral file transfer;
- read-only local/live diagnostics.

They do not own Runpod account balance, secrets, SSH account settings, volume
deletion, image construction, vLLM package compatibility, model correctness, or
measured performance truth. Static placement produces `candidate`, never
`verified`; only a recorded run can establish the latter.

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
private-identity path. Profile creation and every fresh billable submission
reject a missing, rotated, mismatched, broadly readable, or interactive private
key. `PUBLIC_KEY` remains provider-owned and cannot be supplied as a second
authorization channel. SSH then disables user config, agents, proxies, password
authentication, and forwarding side channels. Each Pod has its own mode-0600
known-hosts file and `runpod-POD_ID` host-key alias. The first connection is
TOFU via `accept-new`; a changed key fails.

Pods expose only `22/tcp`. vLLM or another service should bind remote
`127.0.0.1` and be reached through `runpod-tunnel`, which binds both ends to
loopback.

Runpod network volumes are persistent model-cache storage, not a secrets or
prompt-data vault. They are not encrypted by this tool. With a volume-backed
profile, the whole `/workspace` tree persists—not only the cache directories:

```text
/workspace/.cache/huggingface
/workspace/.cache/torch
/workspace/.cache/vllm
```

Use `/root/runpod-session` for private inputs, outputs, logs, and transient
environments. That path is on container storage and is lost when the Pod is
restarted or deleted. `runpod-copy` admits both roots while rejecting path
traversal and shell syntax. The model cache is operationally writable (Hugging
Face needs locks and metadata); this tool does not pretend the weight files are
filesystem-read-only.

## Live July 26, 2026 snapshot

The authenticated account currently has no Pods, volumes, or user templates.
The latest read-only Secure Cloud query returned:

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
| `EUR-IS-2` | RTX Pro 6000 Server + H200 |
| `EU-RO-1` | RTX Pro 6000 Server/Workstation + B200 |
| `US-NC-2` | RTX Pro 6000 Server + B200 |
| `EU-NL-1` | RTX Pro 6000 Server + B300 |

A network volume pins a Pod to its datacenter. A single cache cannot currently
cover the complete Pro/H200/B200/B300 ladder. The clean initial experiment is
one Pro+H200 volume in `EUR-IS-2`, because that is the main 96-vs-141 GB
decision. Add a B200 or B300 cache only after a measured experiment justifies
its recurring cost.

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

- [Runpod Pod API](https://docs.runpod.io/api-reference/pods/POST/pods)
- [Runpod network volumes](https://docs.runpod.io/storage/network-volumes)
- [Runpod SSH](https://docs.runpod.io/pods/configuration/use-ssh)
- [Current Runpod PyTorch image tags](https://hub.docker.com/r/runpod/pytorch/tags)
- [Current vLLM serve CLI](https://docs.vllm.ai/en/stable/cli/serve/)

## First setup

On a new machine, authenticate through the no-echo prompt. Never put the key in
shell history, dotfiles, or chat:

```sh
runpod-auth login
runpod-auth status --check --json
runpod-doctor --live
```

Authentication is already configured on this machine; the first line is the
portable bootstrap path.

For unattended agent-driven sessions, use a dedicated non-interactive Ed25519
identity rather than a general personal key:

```sh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_runpod
chmod 600 ~/.ssh/id_ed25519_runpod
chmod 644 ~/.ssh/id_ed25519_runpod.pub
```

The public key is not secret. The private key never enters dotfiles or Runpod
state.

Plan a 250 GB Pro+H200 cache:

```sh
runpod-volume create model-cache-pro-h200 \
  --size-gb 250 --data-center EUR-IS-2 --json
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

The account currently exposes no saved templates through the REST API, so a
profile can use an explicit official image. As of this snapshot,
`runpod/pytorch:1.1.0-cu1290-torch291-ubuntu2404` is a current CUDA 12.9 /
PyTorch 2.9.1 / Ubuntu 24.04 tag. Image tags are mutable operational inputs;
record the pulled image identity in benchmark results and move to a custom
digest-pinned image once the runtime stack stabilizes. The Pod API currently
reports the requested tag, not proof of the digest actually pulled.

After volume creation, author the datacenter-specific profile:

```sh
runpod-profile create pro-h200 \
  --image runpod/pytorch:1.1.0-cu1290-torch291-ubuntu2404 \
  --network-volume-id VOLUME_ID \
  --gpu pro6000-server --gpu h200 \
  --max-hourly 4.50 --ttl 4h --cuda 12.9 \
  --identity-file ~/.ssh/id_ed25519_runpod \
  --public-key-file ~/.ssh/id_ed25519_runpod.pub \
  --json
```

For a gated/private Hugging Face model, create the token as a Runpod secret in
the console and add `--secret-env HF_TOKEN=secret_name` to the profile. Local
inspection and remote download are separate credential contexts; no local
token is copied into a Pod or profile.

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
  --context 32768 --weight-format native \
  --ttl 4h --idle-ttl 30m --json
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
  --context 32768 --weight-format native \
  --ttl 4h --idle-ttl 30m --execute --json
```

The create path:

1. fsyncs a unique local intent;
2. re-proves the private key against the injected public-key snapshot;
3. fixes the absolute hard deadline immediately before submission;
4. submits at most once;
5. reconciles exactly one UUID-bearing remote name after a crash/timeout;
6. persists the immutable Pod ID;
7. verifies allocation and total actual price;
8. deletes the exact Pod on any contradictory result.

Runpod Pod names are not unique. If a submission response is ambiguous and no
matching Pod is visible yet, the tool does not gamble with a second POST.
Retry the same `runpod-up ... --execute` later. One exact match is adopted;
multiple matches become a fail-closed conflict.

A normal create response can also remain `provisioning` while Runpod populates
machine, price, or port fields. Rerun the same pinned execute command until the
receipt becomes `active`; it does not issue a second POST.

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
volume.

## TTL enforcement

Hard TTL is absolute and includes provisioning. Idle TTL means no explicit
heartbeat from these local tools or the benchmark driver. It is not inferred
from GPU utilization or server request traffic.

```sh
runpod-ttl show compiler --json
runpod-ttl touch compiler --source benchmark_driver --json
runpod-ttl set compiler 4h --json
runpod-ttl extend compiler 30m --json
runpod-ttl enforce --json
runpod-ttl enforce --execute --json
```

`set` changes total lifetime relative to the original submission; `extend`
explicitly moves the current deadline. Both, and `touch`, mutate local lease
state immediately and do not need `--execute`.

Before the first paid Pod launch, start the foreground watcher in a separate
terminal or user-service supervisor on an awake credentialed machine:

```sh
runpod-ttl watch --execute --interval 30s
```

With `--json`, watcher output is NDJSON: one compact enforcement result per
line. Pending deletes are retried. Every destructive retry rechecks the current
operation ID and current expiry under its instance lock, so a stale scan cannot
delete a refreshed or replacement Pod.

This is deliberately honest: `~/.local/runpod` plus a sleeping laptop is not a
provider-side lease service. If the watcher, machine, credential, or local state
is lost, the Pod can continue billing past its local TTL; use the Runpod console
as the emergency source of truth and terminate the exact Pod there. Local locks
coordinate processes on one filesystem, not split controllers on multiple
machines. `runpod-status` and `runpod-doctor --live` expose other Pods as
unmanaged here and never auto-delete them. `--max-hourly` is a rate cap, not a
total budget, and excludes recurring volume cost.

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

runpod-tunnel compiler --local-port 8000 --remote-port 8000
```

`/workspace` is the persistent network volume; `/root/runpod-session` is
ephemeral container storage. Tunnels are foreground-only and loopback-only.
Merely keeping a tunnel open does not refresh idle activity. The benchmark
driver should call `runpod-ttl touch NAME --source LABEL` after real work.

The placement receipt now retains the exact resolved commit, selected index,
checkpoint shard identities, context/KV estimate, and memory policy. A vLLM
launch must consume those values rather than mutable defaults. After installing
a pinned, verified vLLM/CUDA/PyTorch build into the image or ephemeral session
environment, the native-safetensors shape is:

```sh
runpod-ssh compiler -- \
  /root/runpod-session/venv/bin/vllm serve Qwen/Qwen3-32B \
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
bound either way.

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
