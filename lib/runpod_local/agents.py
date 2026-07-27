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
- `runpod-volume`: plan/reconcile persistent cache-volume creation.
- `runpod-template`: inspect account-visible templates without environment data.
- `runpod-profile`: author validated reusable launch policy.

Session commands:

- `runpod-up`: plan or execute one crash-reconcilable Pod launch.
- `runpod-status`: reconcile receipts against exact live Pod identities.
- `runpod-down`: plan or delete one Pod while preserving its volume.
- `runpod-ttl`: inspect/mutate leases or run the foreground cleanup watcher.
- `runpod-ssh`: open a validated session or one quoted remote command.
- `runpod-tunnel`: open one loopback-only tunnel without faking activity.
- `runpod-copy`: copy persistent cache/tool data or ephemeral session data.
- `runpod-hf-auth`: lease one local Hugging Face token to ephemeral Pod storage.
- `runpod-doctor`: read-only local/provider integrity audit.

Paid mutations always require `--execute`. `runpod-ttl set`, `extend`, and
`touch` are immediate local lease mutations; they do not contact Runpod and
cannot move a lease beyond the provider-owned launch deadline. Network volumes
outlive Pods and are never deleted by this suite.
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
runpod-place --list-gpus
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
runpod-volume create model-cache --size-gb 250 \\
  --data-center DATA_CENTER_ID --json
```

Network volumes pin Pods to one Secure Cloud datacenter and survive Pod
termination. Creation validates the live datacenter, reports the dated standard
storage estimate, and reconciles one exact existing name/size/datacenter match
before any POST. Same-state-root creates for one name are serialized, and the
returned ID/name/size/datacenter must match before success is reported. Never
blindly retry an ambiguous create; inspect `list` first.
This command intentionally has no volume-delete action: model-cache deletion is
a separate console/API action, not session cleanup.
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
network-volume identity, cache paths, price cap, SSH identity, and a hard-TTL
default no greater than 30 minutes.

```sh
runpod-profile create nvidia-dev \\
  --image IMAGE@sha256:DIGEST --network-volume-id VOLUME_ID \\
  --gpu pro6000-server --gpu h200 \\
  --max-hourly 4.50 --ttl 30m \\
  --identity-file ~/.ssh/id_ed25519_runpod \\
  --public-key-file ~/.ssh/id_ed25519_runpod.pub --json
```

Explicit profile images require immutable digests; provider template IDs remain
available for sessions that do not receive credential leases. Environment
names containing TOKEN, KEY, SECRET, PASSWORD, or CREDENTIAL are rejected, as
are all Runpod-secret environment references. The official image shell-sources
a serialized Pod environment, so controls and shell expansion/quoting
characters are also rejected in every value. `HF_TOKEN_PATH` is the one
constrained non-secret exception and is fixed to ephemeral
`/root/runpod-session/secrets/huggingface/token`; credentials never belong in a
profile. Shell-startup controls such as `BASH_ENV`, `ENV`, and `ZDOTDIR` are
reserved by the reconciled SSH control plane, as are dynamic-loader controls
such as `LD_*`, `GLIBC_TUNABLES`, and `GCONV_PATH`. `PUBLIC_KEY` is
provider-owned; the tool validates and injects one profile-specific
`SSH_PUBLIC_KEY` as profile/receipt identity, not as proof of full-TCP
authorization. Immediately before a fresh billable create, the controller
requires the same algorithm and key body among the newline-separated keys in
Runpod account `myself.pubKey`; comments are ignored. The private/public pair
is also revalidated. Local profiles are advisory across machines; provider
state and exact Pod IDs remain authoritative.
""",
    "up": """# `runpod-up`

Plan by default. `--execute` fsyncs a unique local launch intent before the
first create request, reconciles an ambiguous request by exact UUID-bearing
remote name, verifies the actual GPU/count/datacenter/volume/image/security/
ports/total price, and rolls back a contradictory allocation.

```sh
runpod-up compiler --profile pro-h200 --model Qwen/Qwen3-32B \\
  --context 32768 --ttl 30m --json
runpod-up compiler --profile pro-h200 --model Qwen/Qwen3-32B \\
  --context 32768 --ttl 30m --execute --json
```

Static model placement admits only `candidate` by default.
`--allow-indeterminate-fit` is explicit and never admits `tight` or
`impossible`. Omitting `--model` means the profile/operator owns fit.

The hard deadline starts when the launch intent is durably written, so
credential attestation and provisioning count. That one absolute timestamp is
hashed into the receipt and sent in Runpod's create mutation as
`terminateAfter`; Runpod terminates the Pod even if this controller, terminal,
or UI disappears. Profiles default to 30 minutes, and an omitted launch TTL is
capped at 30 minutes even when a stale profile from another machine contains a
longer default. This is a hard lifetime, not inactivity detection: an active
session also ends at 30 minutes. Longer sessions require an explicit per-launch
TTL and deliberately increase lost-controller billing exposure.

A submission with an ambiguous response and no visible matching Pod is never
re-submitted automatically. Retry the same command only to reconcile. Once the
provider deadline has elapsed, an exact absence check may close the receipt
because Runpod owns the hard lifetime. If the exact Pod appears while that
terminal receipt remains current, status reports it and TTL enforcement deletes
it as a terminal leak. Local locks coordinate only one machine. A second
machine with a split state root can launch another Pod; `runpod-status` exposes
it as unmanaged here.

An `intent` receipt proves no create request was sent. Its preflight requires
the remote name to be absent; a match is an unmanaged collision, atomically
aborts that operation, and is never adopted or deleted by it. A retry mints a
distinct UUID name. Aborting or expiring any other unsubmitted intent likewise
does not change its ownership proof.

Before entering the ambiguous submission state, `--execute` checks the
profile's exact SSH algorithm and key body against Runpod account
`myself.pubKey`. A missing or mismatched account key returns
`account_ssh_key_not_authorized`, leaves the receipt in retryable `intent`, and
sends no Pod create request. Add the configured `.pub` line to the Runpod
account's **SSH Public Keys** field and retry the same command.
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
Network-volume model caches survive termination. A duplicate-name conflict is
deleted only by explicit `--execute`, using its durably recorded Pod-ID set
after every ID and name agree with live provider reads. Duplicates first seen
during teardown are saved before the first delete, and the cleanup authorization
is durable so the watcher retries partial failures. A newly observed ID expands
the conflict set and revokes that authorization; no remaining member is deleted
until the expanded set receives another explicit `--execute`. Ambiguous
submissions fail closed before their provider deadline instead of guessing
which Pod to delete. At or after that deadline, an exact absence check closes
the receipt without issuing another create request.
""",
    "ttl": """# `runpod-ttl`

Hard TTL is an absolute billing guard anchored to the durable launch intent and
embedded in the Pod creation request as Runpod `terminateAfter`. Idle TTL means
no explicit heartbeat from these local tools; it does not inspect GPU
utilization, Pi, or vLLM requests through a tunnel. Long-running attached SSH
commands heartbeat while the process remains attached, even if the human is
idle. Heartbeats never move the hard deadline.

```sh
runpod-ttl show compiler --json
runpod-ttl set compiler 20m --json
runpod-ttl extend compiler 5m --json
runpod-ttl touch compiler --source benchmark_driver --json
runpod-ttl enforce --json
runpod-ttl enforce --execute --json
runpod-ttl watch --execute --interval 30s
```

`set` changes local total lifetime while retaining the original intent anchor;
`extend` moves a previously shortened local deadline. Neither may pass the
immutable provider deadline. `enforce` is one-shot and plan-only without
`--execute`; `watch` is a foreground enforcer and requires `--execute`. With
`--json`, watcher output is one compact JSON object per line. Provider
termination owns the hard fleet deadline; the credentialed local watcher is
still required for idle or deliberately shortened expiry, and this suite does
not install that watcher as a user service. The default provider hard lifetime
is 30 minutes and terminates active sessions too. An explicit longer
`runpod-up --ttl` raises the lost-controller billing bound. Expired leases
cannot be touched or extended. Pending cleanup is retried, stale scans recheck
operation identity and expiry, and exact deletion always preserves the network
volume.
""",
    "ssh": """# `runpod-ssh`

Resolve an active local receipt against the exact live Pod, validate its
allocation and mapped SSH endpoint, then use one dedicated identity and one
per-Pod known-hosts file.

```sh
runpod-ssh compiler
runpod-ssh compiler -- nvidia-smi --query-gpu=name,memory.total --format=csv
runpod-ssh --json compiler
```

Remote arguments after `--` are encoded as one `exec` command with POSIX shell
quoting; they are never appended as raw OpenSSH arguments. Endpoint inspection
is mutually exclusive with a remote command so command arguments are not
printed accidentally. First connection uses explicit per-Pod TOFU
(`accept-new`); a later key change fails and is never silently removed.

The subprocess receives neither Runpod/Hugging Face credentials nor an SSH
agent socket. Attached SSH commands emit explicit idle heartbeats bound to the
exact operation/Pod, but never move the hard deadline.
""",
    "tunnel": """# `runpod-tunnel`

Open one foreground SSH tunnel to remote loopback. The local listener is
either loopback TCP or a private Unix-domain socket.

```sh
runpod-tunnel compiler --local-port 8000 --remote-port 8000
runpod-tunnel compiler \\
  --local-socket /run/user/1000/model-session/gemma4.sock \\
  --remote-port 8000
runpod-tunnel --json compiler --local-port 8000 --remote-port 8000
```

TCP uses `127.0.0.1:LOCAL:127.0.0.1:REMOTE`; there is no public-bind option.
Unix mode requires a normalized absolute path below an owned mode-0700 real
directory and creates missing private parent directories. It emits a
mode-0600 socket, refuses active, foreign, permissive, symlink, and nonsocket
paths, and removes only an unchanged owned socket after both a refused AF_UNIX
stream connection and absence from the Linux kernel socket table. If that
proof is unavailable, cleanup fails closed. OpenSSH itself is forbidden from
unlinking the path. Run vLLM on remote `127.0.0.1` and expose only `22/tcp`.
The foreground process checks the lease but does not refresh idle activity
merely because a tunnel exists. The request/benchmark driver should call
`runpod-ttl touch` after real work.
""",
    "copy": """# `runpod-copy`

Copy in either direction through the reconciled direct SSH endpoint.

```sh
runpod-copy push compiler ./bench.py /workspace/tools/bench.py
runpod-copy pull compiler /workspace/results/profile.json ./profile.json
runpod-copy push --recursive compiler ./loom /workspace/src/loom
```

Remote operands must be canonical absolute paths beneath persistent
`/workspace` or ephemeral `/root/runpod-session` and use a conservative
literal-segment grammar: no traversal, whitespace, globs, colons, shell syntax,
backslashes, or tildes. Local operands are made absolute, OpenSSH
config/proxies/agents are disabled, and every transfer uses the per-Pod
known-hosts file. `--json` or `--print` inspects without copying.
""",
    "hf-auth": """# `runpod-hf-auth`

Lease only the local active Hugging Face token to one active Pod:

```sh
runpod-hf-auth push compiler
runpod-hf-auth status compiler --json
runpod-hf-auth clear compiler
```

`push` opens `${HF_TOKEN_PATH:-~/.config/huggingface/token}` only after proving
it is a bounded, owned, non-symlink, private regular file. A first non-secret
SSH probe establishes the dedicated per-Pod host key; a second connection
streams the already-open file as stdin to a fixed remote program. Token bytes
never enter argv, environment, provider metadata, profile, receipt, JSON,
logs, or `/workspace`.

The remote program accepts one token, creates only real owner-controlled 0700
directories below `/root/runpod-session`, and atomically installs a mode-0600
token through absolute isolated system Python with an empty environment. An
orphaned atomic-install temporary makes `status` fail unsafe; a valid `push` or
`clear` removes it. The profile sets `HF_TOKEN_PATH` there before Hugging Face
libraries import. Pod deletion removes it; `clear` removes it earlier. Neither
action revokes the source token at Hugging Face, and browser-OAuth refresh state
is never copied. Push the current active token again if it expires. Pod-root
code can read the lease; this boundary prevents persistence and accidental
disclosure rather than hiding a credential from the selected workload.
`push` requires an explicit digest-pinned image and refuses a mutable tag or
template; `status` and `clear` remain available for cleanup.

These explicit credential actions execute immediately. `--json` formats the
safe result; it is not a plan mode.
""",
    "doctor": """# `runpod-doctor`

Run a read-only integrity audit over command availability, credential and state
permissions, profile/receipt schemas, SSH identities, dedicated host keys, and
overdue leases.

```sh
runpod-doctor
runpod-doctor --live --json
```

`--live` additionally lists Pods once, volumes once, and stock once, then joins
active receipts to immutable Pod IDs and validates allocation, price cap,
volume, and SSH readiness. Missing endpoint mappings during initialization are
warnings; identity/policy drift and terminal receipts with live Pods are
errors. Pods owned by a split-state controller are reported as unmanaged and
never mutated.

Doctor never creates, starts, stops, or deletes provider resources and never
prints credential or remote environment values. Its exit status is nonzero
when any check is an error.
""",
}
