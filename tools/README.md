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
├── latest -> 21.1.6
└── env.sh          # Common LLVM settings
```

## Usage

### Global Defaults

The shell automatically loads latest versions from ~/tools/ for interactive shells.
This is handled by `~/.dotfiles/tools/tools.sh` (sourced from ~/.shrc).

### Per-Project Versions

Use `project-init` to set up a new project:

```bash
cd my-project
project-init
```

This creates:
- `.envrc` - Tool configuration (commit this)
- `.envrc.local` - Machine-specific overrides (gitignored)

### Manual .envrc

Create `.envrc` in your project directory:

```bash
# Use specific LLVM version
use_llvm "21.1.6"

# Use minimum version (finds highest matching)
use_cmake ">=3.28.0"

# Use latest for tools without version requirements
use_ninja
use_mold

# ROCm - silent skip on non-Linux
use_rocm ">=6.0.0"

# Load machine-specific overrides
source_local_envrc
```

Then run `direnv allow` to activate.

### IREE Development

For IREE development, use the convenience function:

```bash
# In .envrc
use_iree_dev  # Uses sensible defaults
# Or specify versions:
use_iree_dev ">=21.0.0" ">=3.28.0"
```

## Installing New Tool Versions

### LLVM Example

```bash
# Download and extract
cd ~/tools/llvm
wget https://github.com/llvm/llvm-project/releases/download/llvmorg-21.1.6/clang+llvm-21.1.6-x86_64-linux-gnu.tar.xz
tar xf clang+llvm-21.1.6-x86_64-linux-gnu.tar.xz
mv clang+llvm-21.1.6-x86_64-linux-gnu 21.1.6

# Update latest symlink
ln -sfn 21.1.6 latest
```

### CMake Example

```bash
cd ~/tools/cmake
wget https://github.com/Kitware/CMake/releases/download/v3.31.7/cmake-3.31.7-linux-x86_64.tar.gz
tar xf cmake-3.31.7-linux-x86_64.tar.gz
mv cmake-3.31.7-linux-x86_64 3.31.7
ln -sfn 3.31.7 latest
```

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
| project-init | ~/.dotfiles/bin/ | Project setup script |
| env.sh | ~/tools/<tool>/ | Tool-specific settings |
