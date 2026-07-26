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
}
