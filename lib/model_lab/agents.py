"""Compact operating contract emitted for autonomous callers."""

AGENTS_MD = """\
# model-lab

`model-lab` owns models, Hugging Face staging, inference services, compiled
caches, local endpoints, and isolated Pi attachments. It consumes generic
RunPod host claims; it never owns or directly terminates a provider Pod.

The shortest interactive workflow is:

```sh
model-lab pi PROFILE
model-lab pi PROFILE resume
model-lab pi PROFILE resume SESSION_ID
model-lab pi PROFILE --now
```

Those commands ensure the profile's exact service, reuse or atomically acquire
a compatible RunPod host claim, acquire a local service-use lease, and launch
model-session. Exiting Pi releases only that use lease. The final release starts
the configured model-service idle TTL. `--now` stops the service and releases
its RunPod claim after the final Pi user exits. That immediate-release request
is durable: another active Pi user or a supervisor restart cannot discard it.

Administrative commands are:

```sh
model-lab model ORG/MODEL [--revision REVISION]
model-lab place ORG/MODEL [--gpu GPU]
model-lab place --list-gpus
model-lab plan SERVICE
model-lab up SERVICE
model-lab status [SERVICE]
model-lab down SERVICE
model-lab down SERVICE --now
model-lab hf-auth push HOST
model-lab hf-auth status HOST
model-lab hf-auth clear HOST
model-lab migrate SOURCE_PROFILE_ROOT --service SERVICE
model-lab service list
model-lab service show SERVICE
model-lab profile list
model-lab profile show PROFILE
```

`model-lab down` starts the model idle grace. `--now` explicitly bypasses it.
RunPod applies its own empty-host retention after the model claim is released.
`model` and `place` may fetch metadata into model-lab's private cache, but they
never download model weights or mutate provider resources.
`hf-auth` streams a model-lab-owned token over SSH stdin to one active generic
host; token bytes never enter argv, RunPod state, service definitions, or
output. `migrate` is a provider-free copy-and-rebind transaction and never
contacts RunPod.

Authored configuration lives under `/mnt/dev/model-lab`. Controller receipts
live under `~/.local/state/model-lab`. Sockets live under
`$XDG_RUNTIME_DIR/model-lab`. RunPod credentials, host state, and provider
lifetime remain in their sibling RunPod namespaces.
"""
