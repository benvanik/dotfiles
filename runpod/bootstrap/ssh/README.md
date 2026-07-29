# Runpod SSH launch bootstrap

This is a launch-time control overlay, not a container image recipe. The
Runpod Pod API supplies:

```json
{
  "dockerEntrypoint": ["/bin/bash", "-c"],
  "dockerStartCmd": ["<exact contents of bootstrap.sh>"]
}
```

The script installs Ubuntu's `openssh-server` package into ephemeral container
storage, validates the account public key, creates a key-only SSH surface, and
executes `sshd` as PID 1. It does not inspect, install, or alter the model
runtime. Every bootstrap invocation creates a new Ed25519 host key under
`/run/sshd` and prints its SHA-256 fingerprint; host keys produced by the
package post-install step under `/etc/ssh` are never selected.

Machine-parseable `phase=apt-start`, `phase=apt-complete`,
`phase=authorized-key-ready`, `phase=host-key-ready`, and `phase=sshd-ready`
records expose the cold-start boundary without polling or wall-clock sleeps.
The key phases include SHA-256 fingerprints, never key material.

The generic template builder binds the exact script bytes and SHA-256 identity
into the private template contract, and every profile and allocation attests
that contract. The script is passed directly as one argument to the pinned
upstream image. It is never uploaded as a layer, fetched from a mutable URL,
or expanded into a derived base image.
