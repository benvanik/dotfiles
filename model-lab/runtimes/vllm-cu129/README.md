# Model-lab pinned upstream vLLM CUDA 12.9 runtime

This contract selects the exact official `vllm/vllm-openai` amd64 image for
vLLM 0.25.1 and CUDA 12.9. The digest, upstream source revision, package
versions, OCI defaults, and compressed image size come from the published OCI
metadata and direct inspection of that digest.

There is deliberately no Dockerfile and no derived image. Runpod or the
upstream registry owns distribution and host-side caching of the 11.74 GB
runtime. Our only launch overlay is the small, content-identified SSH bootstrap
in `runpod/bootstrap/ssh`; model profiles and Hugging Face caches remain
external state.

The model-lab service definition selects this reviewed runtime by catalog ID.
Its compatible generic RunPod host profile independently references the same
immutable image digest:

```sh
runpod template create generic-vllm-cu129 \
  --image 'vllm/vllm-openai@sha256:fb463d6a216c7ee82bf947f321cae7dd7105bfb5084ea35827c2ceb816994b15'
```

After SSH becomes ready, the administration layer copies this manifest and
`verify-runtime.py` into ephemeral container storage and runs the verifier
before starting a model. The verification report distinguishes the requested
image identity from the package, CUDA, GPU, and source-feature observations
made inside the running Pod.
