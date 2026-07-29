# Model lab

`model-lab` turns an authored model service and Pi profile into a private,
short-lived endpoint. It resolves exact Hugging Face revisions, stages shared
weights, manages vLLM and driver-specific compiled caches, obtains an opaque
claim on a compatible RunPod host, meters inference activity, and launches Pi
inside the model-session sandbox.

The everyday surface is intentionally small:

```sh
model-lab pi chat
model-lab pi chat resume
model-lab pi chat resume SESSION_ID
```

If no compatible host is available, the first command acquires and starts one.
If the exact service is already running, another profile or Pi session reuses
it. Exiting the final Pi user starts the model-service idle TTL. The explicit
immediate form is:

```sh
model-lab pi chat --now
```

That stops the model process and releases its RunPod claim after Pi exits.
When it was the final claim on an automatically retained host, RunPod then
retires the host immediately instead of applying its normal empty-host grace.
The request is latched in the deployment before Pi admission. If another Pi
user remains, its later final release still performs the immediate stop; a
supervisor restart cannot turn that request into ordinary idle grace.

Every command has `--help`; `model-lab --agents-md` emits the compact contract
intended for an autonomous administrator.

## Ownership boundary

Model-lab sits above, rather than replacing, generic
[`runpod`](../runpod/README.md) host control:

```text
RunPod provider
  └─ generic host and resource claims        runpod
       └─ exact model service and endpoint   model-lab
            └─ isolated project session      model-session / Pi
```

`runpod` owns Pods, volumes, SSH, price and hardware constraints, provider hard
TTL, opaque resource allocation, and empty-host retirement. It has no model,
Hugging Face, vLLM, cache, prompt, or project semantics.

`model-lab` owns model identity, metadata inspection, placement estimates,
snapshot closure, remote credential lease, runtime installation, vLLM
configuration, compiled-cache identity, service process, local metered proxy,
service-use leases, and service idle retirement. It never directly creates or
deletes a Pod; it asks the generic RunPod claim API for capacity.

`model-session` owns the Pi profile grammar, prompt snapshot, resumable
history, project/workspace mapping, service attachment validation, and bwrap
isolation. A profile has no cloud-administration authority.

This separation keeps one host reusable across CUDA development, benchmarks,
training jobs, and several model services. A project is never synonymous with
a model, and a model is never synonymous with a Pod.

## Namespaces

Instantiation state remains outside the dotfiles infrastructure repository:

```text
/mnt/dev/model-lab/
  lab.toml                         global placement and lifetime policy
  .migrations/                     bounded migration locks and staging
  services/MODEL.toml             one declarative file per model service
  profiles/PROFILE/
    profile.toml                   Pi/project/service route and sandbox policy
    AGENTS.md                      per-session workspace contract
    SYSTEM.md                      profile-owned system prompt
    service-binding.json           generated permanent workload binding
  projects/PROJECT/               shared reports, memory, and project inputs
  sessions/PROFILE/SESSION/       retained isolated session state
  runtimes/pi/VERSION/            exact Pi installation
  evidence/                       acceptance, benchmark, and migration records
  archive/                        preserved superseded instantiation material

~/.local/state/model-lab/
  deployments/                    service and use-lease receipts
  preparations/                   crash-recovery acquisition intents
  deployed-services/              exact service-definition snapshots
  service-installations/          host/materialization bindings
  service-materializations/       small content-addressed transfer closures
  closures/huggingface/           generated exact snapshot manifests
  cache/huggingface-metadata/     private metadata cache
  supervisor.log

$XDG_RUNTIME_DIR/model-lab/
  supervisor.sock                 same-user administration channel
  services/MODEL.sock             private OpenAI-compatible endpoint
  services/MODEL.json             short-lived endpoint admission receipt
  transports/                     private upstream SSH tunnels

~/.config/huggingface/token       private local Hugging Face credential
```

Directories carrying private state are mode 0700. Sensitive and authored files
are mode 0600. The authored model-lab root may be copied or backed up without
copying machine-local provider receipts.

## Configuration shape

`lab.toml` selects generic RunPod profiles and the three service/claim clocks:

```toml
schema = "model-lab.v1"
allowed_runpod_profiles = ["pro6000-is1"]

[lease]
hard_ttl_seconds = 46800
service_idle_ttl_seconds = 1800
renewal_ttl_seconds = 120
minimum_useful_seconds = 300
startup_timeout_seconds = 300
```

The hard TTL is the provider-enforced maximum for a newly acquired host. It
continues to bound billing if every local process disappears. The service idle
TTL starts only after the final consumer releases its use lease and is reset
by each completed inference response. Claim renewal keeps an active or
grace-period service admitted; it cannot extend the provider hard deadline.
The example hard deadline covers the profile's 12-hour Pi runtime ceiling,
its 30-minute idle grace, and a final 30-minute safety margin. It is a
fail-safe, not the normal shutdown path.

The startup timeout is one absolute command deadline, not a fresh timeout per
stage. It begins before supervisor autostart and covers queueing, generic host
acquisition, every RunPod provider preflight/create/verification request, SSH
readiness, Hugging Face staging, cache selection, vLLM readiness, tunnel and
proxy publication, and final Pi admission. It is hard-capped at five minutes.
Crossing it cancels the exact pending acquisition, rolls back unpublished
service authority, and never hands a late endpoint to Pi. That five-minute
bound governs endpoint delivery, not abandonment of ambiguous remote state:
the supervisor may continue one distinct, durable cleanup attempt for at most
60 seconds, then retains the exact recovery state for later reconciliation.
An unpublished local Pi session receives its own five-second cleanup grace;
once its directory is durably published it is preserved for explicit recovery.

One service TOML owns every model-specific serving difference and nothing
executable:

```toml
schema = "model-lab.service.v1"
service_id = "example-nvfp4"
driver = "vllm-openai.v1"
runtime_id = "vllm-cu129-v0.25.1"

[model]
source = "huggingface"
repository = "namespace/exact-model"
revision = "0123456789abcdef0123456789abcdef01234567"
checkpoint = "model.safetensors"
weight_format = "native"

[endpoint]
input_modalities = ["text", "image"]
reasoning = true
max_output_tokens = 32768

[compatibility]
minimum_compute_capability = "12.0"

[resources]
gpu_count = 1
gpu_memory_gib = 96
cpu_count = 8
memory_gib = 32
ephemeral_disk_gib = 50
claim_mode = "shared"

[vllm]
model_implementation = "vllm"
dtype = "bfloat16"
quantization = "modelopt_fp4"
tensor_parallel_size = 1
max_model_len = 65536
max_num_sequences = 8
max_num_batched_tokens = 8192
kv_cache_dtype = "bfloat16"
gpu_memory_utilization = 0.90
chunked_prefill = true
load_format = "safetensors"
safetensors_load_strategy = "lazy"
language_model_only = false
mamba_cache_mode = "none"
prefix_caching = false
reasoning_parser = "qwen3"
tool_call_parser = "qwen3_coder"
speculative_method = "mtp"
speculative_tokens = 1
generation_config = "auto"
```

The resource table is admission truth. A service configured to consume 90% of
a 96 GB GPU reserves the full 96 GB even when its serialized checkpoint is
only 20 GB; under-reporting would let the generic scheduler admit an unsafe
second consumer. Later multi-model placement requires measured lower memory
utilization and an honest smaller reservation, not a second per-model script.

A profile routes one isolated agent identity to one service:

```toml
schema = "model-session.profile.v3"
profile_id = "chat"
project_id = "qwen36-heretic"
service_id = "qwen36-heretic-nvfp4"

[endpoint]
required_input_modalities = ["text", "image"]

[pi]
version = "0.82.1"
tools = ["read", "write", "edit", "bash"]
system_prompt_file = "SYSTEM.md"

[storage]
max_sessions = 8
work_bytes = 2147483648
work_inodes = 32768
history_bytes = 536870912
history_inodes = 8192
checkpoint_bytes = 3221225472
max_file_bytes = 1073741824
max_logical_bytes = 2147483648

[sandbox]
memory_bytes = 8589934592
max_tasks = 256
max_runtime_seconds = 43200
idle_timeout_seconds = 3600
shutdown_grace_seconds = 30
```

The generated `service-binding.json` permanently binds that profile ID to the
service workload hash. Prompts and sandbox policy may evolve for new sessions;
changing the model workload requires a new profile ID. Existing sessions keep
their captured prompt and frozen endpoint requirement.

## Pi and sandbox behavior

`model-lab pi PROFILE` performs one transactional path:

1. validate the complete live profile for a new session, or the exact frozen
   run identity for a resume, plus the permanent service binding;
2. reuse or acquire an exact generic host claim;
3. resolve and stage the approved Hugging Face revision;
4. select, prove, or author the driver-specific compiled cache;
5. start vLLM bound to remote loopback;
6. open a private SSH Unix-socket tunnel and metered local proxy;
7. publish a short-lived service endpoint receipt;
8. acquire a refcounted service-use lease;
9. launch model-session and transfer that lease to the exact child PID;
10. release the use lease when the child channel closes.

Pi sees `/workspace` as its session-specific writable tree and `/project` as
the shared project view. The session report and memory directories under
`/project` are the only writable project locations. The model-lab sockets,
provider state, API key, Hugging Face token, SSH keys, home directory, and
unrelated projects are outside the sandbox.

In interactive Pi, paste an image with `Ctrl+V` or drag it into the terminal.
An image already inside the sandbox can be given to the model through Pi's
image-aware `read` tool:

```text
Read /project/reference.png and describe it.
```

Pi's `@file` syntax is a process-start argument, not interactive prompt syntax,
and the intentionally narrow `model-lab pi` surface does not pass arbitrary Pi
arguments through. The profile and endpoint must both advertise the `image`
modality. Text-only profiles fail before launch instead of silently dropping
the image.

## Administrative surface

All inspection below is provider-free:

```sh
model-lab service list
model-lab service validate qwen36-heretic-nvfp4
model-lab service show qwen36-heretic-nvfp4

model-lab profile list
model-lab profile show chat

model-lab plan qwen36-heretic-nvfp4
model-lab status
```

Service lifecycle commands are:

```sh
model-lab up qwen36-heretic-nvfp4
model-lab down qwen36-heretic-nvfp4
model-lab down qwen36-heretic-nvfp4 --now
```

`up` ensures the endpoint and immediately places an otherwise unused service
in idle grace. `down` begins the same grace. `--now` revokes the endpoint,
closes the tunnel, stops only the deployment-owned remote process, and
releases its exact host claim.

`--host HOST` on `plan`, `up`, or `pi` selects an already managed compatible
host. Without it, placement may reuse any compatible claimed/manual host or
create a new while-claimed host.

The local metadata estimator and static hardware comparison are:

```sh
model-lab model namespace/model \
  --revision COMMIT --context 65536 --sequences 8 --kv-dtype bf16

model-lab place namespace/model \
  --revision COMMIT --context 65536 --sequences 8 \
  --gpu pro6000 --gpu h200 --gpu b200
```

These commands fetch metadata, never weight blobs. `native` uses exact
serialized tensor bytes. `bf16`, `fp8`, `int8`, and `q8` are explicitly
hypothetical uniform storage projections unless an authored service names an
actual converted checkpoint and supported loader. Static placement is a
candidate estimate; measured runtime evidence remains authoritative.

One-time cutover from the pre-separation profile layout is provider-free and
source-preserving:

```sh
model-lab migrate /absolute/old/profile \
  --service qwen36-heretic-nvfp4 \
  --target-profile-id chat
```

It holds the source production materialization and run locks, verifies exact
model/service agreement, copies mutable session history, rebuilds the v3
immutable envelope, validates every production loader, writes a durable
migration receipt, and publishes the active profile last. Source files are
never renamed or removed.

## Credentials and privacy

The pinned `hf` wrapper owns local authentication:

```sh
hf auth login
hf auth whoami
```

It rejects token argv, token-printing commands, inherited token environments,
and broad file modes. Browser/OAuth state remains beside the private token
file. Metadata clients read that file directly through a bounded no-follow
operation.

During model staging, the credential is streamed over reconciled SSH stdin to:

```text
/root/runpod-session/secrets/huggingface/token
```

The path is ephemeral container storage. It is cleared before vLLM starts and
never appears in argv, environment, logs, receipts, or the persistent network
volume. Manual inspection of that lease is available for an active managed
host:

```sh
model-lab hf-auth status HOST
model-lab hf-auth push HOST
model-lab hf-auth clear HOST
```

The RunPod network volume contains public/reconstructible model snapshots and
explicitly accepted compiled caches. Prompts, Pi histories, project data,
credentials, service logs, and transient outputs stay local or on ephemeral
container storage. vLLM binds remote loopback with request, output, and access
logging disabled.

## Cache and startup contract

The upstream vLLM image is used directly at an immutable digest. RunPod and the
registry distribute its layers; this repository builds no base image. The only
launch overlay installs SSH in ephemeral storage.

Model weights live once on the network volume under an exact repository and
revision closure. A serving snapshot is verified and staged onto local
container disk so model load does not perform thousands of metadata operations
over the network filesystem.

Small Torch/vLLM/XDG caches stay on local container storage. A compiled cache
is retained on the volume only after a two-boot proof:

```text
absent -> author candidate on one Pod
candidate -> prove load on a distinct Pod/boot
accepted -> reusable for matching future starts
```

Its identity includes the model closure, complete launch plan, implementation
bundle, runtime image and packages, GPU, compute capability, CUDA, and NVIDIA
driver. Driver changes therefore select another cache instead of gambling on
binary compatibility. Interrupted or failed proof attempts cannot be retried
as clean evidence on the same boot.

## Recovery model

The detached same-user supervisor is the only writer for service deployments
and use leases. Clients start it convergently through the boot-local socket.
Kernel peer credentials and process start time bind a Pi lease to the exact
model-session child; an inherited file-descriptor number is not authority.

Preparation intent is durable before a generic host claim. Startup reconciles
incomplete acquisition, preparing, quiescing, stopping, and failed states.
The intent and generic acquisition journal retain the caller's original
absolute startup expiration; retries cannot reset it. A v1 acquisition journal
is transitioned once under the claim-controller lock, with its complete source
and computed v2 result retained under the private `migrations/` state namespace
before the active record is replaced.
Endpoint publication and revocation use exact publication IDs. Shutdown
attempts endpoint, proxy/tunnel, remote runtime, claim release, and host
retirement independently. Each service cleanup attempt has one fresh absolute
60-second budget spanning installation attestation, ephemeral Hugging Face
credential removal, local transport teardown, and the remote process stop.
Local endpoint and proxy/tunnel authority is revoked even when remote state
cannot be read or stopped. An unreadable installation or a stop that reaches
its deadline leaves the deployment quiescing and retains the exact host claim;
later reconciliation retries the idempotent cleanup instead of treating
ambiguous remote process authority as stopped.

The supervisor renews active claims, retires due idle services, and asks
RunPod to enforce empty-host retirement. Provider hard TTL remains the final
billable backstop if repeated cleanup cannot resolve the remote process, or if
the supervisor and machine both disappear.

A Pi lease remains a pending admission until ownership transfers to the exact
session PID. That durable pending record carries the original startup
expiration and its ordinary, stop-if-final, or immediate release policy.
Exact rollback fsync-confirms the lease is absent; maintenance reaps only an
expired pending admission if local persistence prevented the synchronous
rollback.

## Extension pressure

The current vertical slice implements OpenAI-compatible vLLM services and Pi
consumers. The ownership model intentionally leaves room for:

- finite LoRA/fine-tuning jobs with resumable retained checkpoints;
- exclusive-host CUDA and NVIDIA gold-reference benchmarks;
- llama.cpp, PyTorch, custom Python, and ComfyUI services;
- many isolated agents sharing one endpoint;
- several honestly placed model processes sharing one GPU;
- embedding, ranking, generation, and tool-server constellations.

Those additions need measured vertical slices through their hardest ownership
and cleanup boundary. They fit above generic RunPod claims; none requires
making host lifetime part of a model profile or copying executable machinery
per model.
