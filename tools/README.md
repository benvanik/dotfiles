# Versioned Tools Management

Manage multiple versions of development tools with easy version switching via direnv.

## Overview

- **~/tools/**: Local tool installations (not synced)
- **~/.dotfiles/tools/**: Tool configuration (synced)
- **direnv**: Per-project version overrides via .envrc files

## Installation

Tools are installed in `~/tools/<tool>/<version>/` with a `latest` symlink:

```
~/tools/llvm/
├── 21.1.6/         # Installed version
├── 20.1.0/         # Another version
└── latest -> 21.1.6
```

Environment definitions live in `~/.dotfiles/tools/<tool>/env.sh`. Keeping
configuration in Git and payloads in `~/tools/` means a dotfiles pull updates
tool behavior without reinstalling toolchains.

Installers treat each version as one published generation. Native release
archives are checksum-attested before extraction. The Hugging Face and ROCm
Python closures instead pin their requested package identity and validate the
installed command/module surface; their resolver-selected wheel bytes are not
independently attested here. Every closure is prepared in private sibling
staging, checked through its real tool surface, and then renamed into place as a
transaction. Each producer holds a kernel-released child guard from staging
allocation through publication, and staging uses the exact
`.dotfiles-stage-<child>.<uuid>` namespace. A restart therefore reclaims a
pre-journal download only after excluding a live producer. The publication
journal is durable before either rename: recovery restores a displaced prior
generation or accepts a payload whose rename committed before reuse checks or
network work begin. Mounted, cross-filesystem, symlinked, foreign-hidden, and
ambiguous roots fail closed. `--force` never grants an in-place overlay and an
ambient `FORCE` variable has no authority.

## Usage

### Global Defaults

The shell automatically loads the stable `~/tools/<tool>/latest` selectors.
Repointing one selector updates existing exported roots and search paths without
restarting a login session or a long-lived tmux server. This is handled by
`~/.dotfiles/tools/tools.sh` (sourced from ~/.shrc).
`mold` is intentionally excluded from those ambient defaults.

### Per-Project Versions

Use `project-init` to set up a new project:

```bash
mkdir -p my-project/main
cd my-project/main
project-init
project-init --build --mold
```

The `<project>/main` form is worktree-ready: when no repository owns that
directory, `project-init` initializes Git on branch `main` and commits the
generated shared configuration. In an existing repository or an ordinary
directory it only converges project files. `--repository` requires bootstrap;
`--no-repository` keeps file generation explicit.

This creates:

- `.envrc` - Tool configuration (commit this)
- `.envrc.local` - Machine-specific overrides (gitignored)
- `.history/` - Per-project shell history (gitignored)

On rerun, `project-init` preserves supported directive arguments, including
exact tool versions and history choices. It refuses to rewrite an `.envrc`
containing unmanaged content; machine-only additions belong in `.envrc.local`.
Use `--none` for an explicit empty tool selection. Canceling the interactive
selector leaves the existing environment untouched. A successful run asks
direnv to execute the generated environment before reporting it validated.

### Manual .envrc

Create `.envrc` in your project directory:

```bash
# Abort the complete direnv evaluation if any selected environment is invalid.
set -o errexit -o pipefail

# Use specific LLVM version
use_llvm "21.1.6"

# Use minimum version (finds highest matching)
use_cmake ">=3.28.0"

# Use latest for tools without version requirements
use_ninja

# Link with mold only in a project that explicitly requests it
use_mold

# GPU SDKs - silent skip on non-Linux
use_cuda ">=12.9.0"
use_rocm

# Load machine-specific overrides
source_local_envrc
```

Then run `direnv allow` to activate.

## Installing New Tool Versions

### Hugging Face CLI

The committed wrapper pins the official CLI and keeps authentication material
outside the model cache:

```bash
~/.dotfiles/tools/hf/install.sh 1.24.0
hf auth login
hf auth whoami
```

The managed environment lives under `~/tools/hf/`. Model and Xet data use
`${XDG_CACHE_HOME:-~/.cache}/huggingface`; the active token and browser-OAuth
refresh state use `${XDG_CONFIG_HOME:-~/.config}/huggingface` with private
permissions. The wrapper rejects environment tokens, Git credential
duplication, token-printing commands, and `hf update` so the dotfiles pin and
private file remain authoritative.

### Managed release examples

```bash
~/.dotfiles/tools/llvm/install.sh 21.1.6
~/.dotfiles/tools/cmake/install.sh 3.31.7
~/.dotfiles/tools/mold/install.sh 2.41.0
```

The dispatcher accepts only exact supported names. `tools/install.sh mold`
works, while `tools/install.sh --all` deliberately leaves mold uninstalled.

## Machine-Specific Overrides

Create `.envrc.local` in a project (gitignored) to override versions:

```bash
# Use older LLVM on this machine
use_llvm "20.1.0"

# Extra environment variables
export MY_DEBUG_FLAG=1
```

## Files

| File | Location | Purpose |
|------|----------|---------|
| tools.sh | ~/.dotfiles/tools/ | Default loader for shells |
| direnvrc | ~/.dotfiles/tools/ | use_* functions for direnv |
| platform.sh | ~/.dotfiles/tools/ | Platform detection |
| versions.sh | ~/.dotfiles/tools/ | Version comparison |
| project-init | ~/.dotfiles/bin/ | `.envrc`, local overrides, and default history |
| `<tool>/env.sh` | ~/.dotfiles/tools/ | Tool-specific settings |
