"""Agent-facing command contracts."""

from __future__ import annotations


AGENT_DOCS = {
    "root": """# Runpod local control plane

The `runpod-*` commands emit stable JSON with `--json`. Run `--agents-md` on
an individual command for its contract. Remote Runpod state is authoritative;
`~/.local/runpod` contains private local cache, intent, receipts, and leases.
Never pass API or Hugging Face tokens on a command line.

Planning commands:

- `runpod-model`: resolve an exact Hugging Face revision and checkpoint.
- `runpod-place`: apply the versioned static VRAM placement policy.
- `runpod-stock`: query live stock and on-demand price.
- `runpod-profile`: author validated reusable launch policy.

Lifecycle commands are documented by the installed command's own
`--agents-md` output.
""",
    "model": """# `runpod-model`

Resolve `namespace/model` once to a Hugging Face commit, select only the exact
root checkpoint referenced by its index, and report serialized tensor facts
separately from runtime estimates.

```sh
runpod-model Qwen/Qwen3-32B --context 32768 --sequences 1 --json
runpod-model openai/gpt-oss-120b --context 131072 --kv-dtype bf16 --json
```

The default `--weight-format native` uses the selected checkpoint's declared
tensor bytes. A non-native format is a low-confidence uniform projection; it
does not assert that a runnable converted checkpoint exists. `--offline` never
contacts Hugging Face and fails on a cache miss. Use `--index-file` when a
repository intentionally has multiple root checkpoints.

Exact byte counts are facts. KV cache values are architecture estimates and
say when a layout is unsupported. Training memory is outside this contract.
""",
    "place": """# `runpod-place`

Inspect a pinned Hugging Face checkpoint and compare it against the versioned
Runpod GPU catalog and explicit memory policy.

```sh
runpod-place Qwen/Qwen3-32B --gpu pro6000 --gpu h200 --gpu b200 --json
runpod-place openai/gpt-oss-120b --context 131072 --list-gpus
```

Statuses have strict meanings:

- `candidate`: the supported single-GPU estimate fits the policy envelope.
- `tight`: weights fit, but the requested workload exceeds that envelope.
- `impossible`: the weight residency basis alone exceeds physical VRAM.
- `indeterminate`: cache layout, format support, or multi-GPU partitioning is
  not modeled well enough to claim fit.
- `verified` is never produced by static placement; only a measured profile can
  establish it.

Provider memory, the 0.90 allocation fraction, 1.03 weight slack, and 4 GiB
framework reserve are visible and overrideable. Live price/stock is a separate
provider query and never changes these memory facts.
""",
    "auth": """# `runpod-auth`

`runpod-auth login` is a human-only credential bootstrap. It reads the key from
a no-echo terminal prompt, validates it with a read-only Pod-list request, and
then stores it at `~/.config/runpod-local/api-key` with mode 0600 inside a
mode-0700 directory. Never paste a key into chat, pass it as an argument, or
write it into a launch profile.

```sh
runpod-auth login
runpod-auth status --check --json
```

`RUNPOD_API_KEY` is an environment-only override. The key is sent in an
Authorization header, never in a URL. `logout` is plan-only unless
`--execute` is present and removes no Runpod account resources.
""",
    "stock": """# `runpod-stock`

Query live Runpod GraphQL stock and on-demand prices using header
authentication. Global price/stock is advisory; the launch receipt's actual
GPU, datacenter, and hourly price are authoritative.

```sh
runpod-stock --gpu pro --gpu h200 --gpu b200 --min-memory 96 \\
  --available-only --json
runpod-stock --data-centers --json
```

Filters are local and deterministic. `--max-hourly` applies to price per GPU
times `--gpu-count`.
""",
    "volume": """# `runpod-volume`

List, inspect, and create persistent network volumes. Creation is plan-only
without `--execute`.

```sh
runpod-volume list --json
runpod-volume create model-cache --size-gb 500 \\
  --data-center US-KS-2 --json
```

Network volumes pin Pods to one Secure Cloud datacenter and survive Pod
termination. This command intentionally has no volume-delete action: model
cache deletion is a separate high-risk operation, not session cleanup.
""",
    "template": """# `runpod-template`

List templates visible to the authenticated account while omitting all
environment values from output. Use the resulting exact template ID when
authoring a profile.

```sh
runpod-template list --search pytorch --json
```
""",
    "profile": """# `runpod-profile`

Profiles are non-secret, mode-0600 local policy records under
`~/.local/runpod/profiles`. They pin allowed GPU IDs, image or template,
network-volume identity, cache paths, price cap, SSH identity, and hard TTL.

```sh
runpod-profile create nvidia-dev \\
  --template-id TEMPLATE_ID --network-volume-id VOLUME_ID \\
  --gpu pro6000 --gpu h200 --gpu b200 --gpu b300 \\
  --max-hourly 8 --ttl 4h --json
```

Literal values for environment names containing TOKEN, KEY, SECRET, PASSWORD,
or CREDENTIAL are rejected. Use `--secret-env
HF_TOKEN=runpod_secret_name`; the profile stores only the Runpod secret
reference. Local profiles are advisory across machines; provider state and
exact Pod IDs remain authoritative.
""",
    "up": """# `runpod-up`

Plan by default. `--execute` fsyncs a unique local launch intent before the
first create request, reconciles an ambiguous request by exact UUID-bearing
remote name, verifies the actual GPU/count/datacenter/volume/image/security/
ports/total price, and rolls back a contradictory allocation.

```sh
runpod-up compiler --profile pro-h200 --model Qwen/Qwen3-32B \\
  --context 32768 --ttl 4h --idle-ttl 30m --json
runpod-up compiler --profile pro-h200 --model Qwen/Qwen3-32B \\
  --context 32768 --ttl 4h --idle-ttl 30m --execute --json
```

Static model placement admits only `candidate` by default.
`--allow-indeterminate-fit` is explicit and never admits `tight` or
`impossible`. Omitting `--model` means the profile/operator owns fit.

The hard deadline starts immediately before Pod submission, so provisioning
time counts. A submission with an ambiguous response and no visible matching
Pod is never re-submitted automatically: retry this same command later to
reconcile. Local locks coordinate only one machine. A second machine with a
split state root can launch another Pod; `runpod-status` exposes it as
unmanaged here.
""",
    "status": """# `runpod-status`

Join private local receipts to the live provider by immutable Pod ID and report
drift plus unmanaged Pods. Remote state is authoritative. No mutation occurs.

```sh
runpod-status --json
runpod-status compiler --json
runpod-status --local-only --json
```

`--local-only` needs no API credential and makes no claim that a locally active
Pod still exists. A UUID-prefixed Pod without a receipt can belong to another
controller and is never deleted automatically.
""",
    "down": """# `runpod-down`

Plan by default. `--execute` re-fetches the exact receipt Pod ID, requires its
remote name to match, persists termination intent, and deletes the Pod.

```sh
runpod-down compiler --json
runpod-down compiler --execute --json
```

Session cleanup never calls Pod stop and never deletes the network volume.
Network-volume model caches survive termination. Identity conflicts and
ambiguous submissions fail closed instead of guessing which Pod to delete.
""",
    "ttl": """# `runpod-ttl`

Hard TTL is an absolute billing guard anchored to submission. Idle TTL means no
explicit heartbeat from these local tools; it does not inspect GPU utilization
or vLLM requests. Heartbeats never move the hard deadline.

```sh
runpod-ttl show compiler --json
runpod-ttl set compiler 4h --json
runpod-ttl extend compiler 30m --json
runpod-ttl touch compiler --source benchmark_driver --json
runpod-ttl enforce --json
runpod-ttl enforce --execute --json
```

Enforcement is one-shot and plan-only without `--execute`. A local deadline is
not a fleet guarantee unless an awake credentialed process invokes enforcement
regularly. Expired leases cannot be touched or extended. Cleanup deletes only
the exact verified Pod and preserves its network volume.
""",
}
