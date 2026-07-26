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
}
