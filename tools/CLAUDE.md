# Tools Management Reference

## Quick Reference

| Command | Purpose |
|---------|---------|
| `project-init` | Bootstrap a project and its .envrc |
| `~/.dotfiles/tools/cuda/install.sh 12.9.1` | Install CUDA SDK to ~/tools/cuda/ |
| `ROCM_GPU_TARGET=gfx1150 ~/.dotfiles/tools/rocm/install.sh` | Install the latest flattened TheRock SDK |
| `use_cuda [version]` | In .envrc: load the CUDA SDK |
| `use_llvm ">=21.0.0"` | In .envrc: require LLVM 21+ |
| `use_rocm "debug"` | In .envrc: use TheRock debug build |
| `use_mold [version]` | In .envrc: explicitly select the mold linker |

## Directory Layout

```
~/.dotfiles/tools/<tool>/
└── env.sh          # Synced settings (CC, CXX, PATH, etc.)

~/tools/<tool>/
├── <version>/      # Machine-local installed version
└── latest -> ver   # Default symlink
```

## Version Syntax

- `"latest"` - Use latest symlink
- `"21.1.6"` - Exact version
- `">=21.0.0"` - Minimum version (finds highest match)

## Available Functions

In .envrc files:
- `use_llvm [version]` - Load LLVM/Clang
- `use_cmake [version]` - Load CMake
- `use_ninja [version]` - Load Ninja
- `use_mold [version]` - Load mold for an explicitly opted-in project
- `use_cuda [version]` - Load CUDA SDK (Linux only)
- `use_rocm [version]` - Load ROCm (Linux only, silent skip elsewhere)
- `source_local_envrc` - Load .envrc.local overrides

## Adding New Tool Version

1. Update the reviewed release identity in `tools/<tool>/install.sh`.
2. Run `tools/<tool>/install.sh <version>`.
3. Run `tools/<tool>/smoketest.sh` when the tool provides one.

## Platform Behavior

- CUDA and mold: Linux x86-64 or ARM64
- ROCm and the LunarG Vulkan SDK: Linux x86-64
- LLVM: Linux/WSL x86-64 or ARM64, and Apple Silicon macOS

An exact installer request fails on an unsupported target. A committed
`.envrc` silently skips a selected tool whose native artifact is unavailable on
the current machine. CUDA, ROCm, and mold activation also remain disabled by
policy on WSL; the Linux Vulkan and LLVM artifacts remain available there.

## Environment Variables Set

CUDA:
- `CUDA_ROOT`, `CUDA_HOME`, `CUDA_PATH` - CUDA toolkit root
- `CUDA_TOOLKIT_ROOT_DIR` - CMake CUDA toolkit path
- `CUDACXX` - CUDA compiler

LLVM:
- `CC`, `CXX` - Compiler paths
- `LLVM_ROOT`, `LLVM_DIR`, `CLANG_DIR`, `MLIR_DIR` - CMake paths

ROCm:
- `ROCM_HOME`, `HIP_PATH` - ROCm paths
- `CMAKE_PREFIX_PATH` - For TheRock builds

Mold (explicit projects only):
- `MOLD_ROOT` - Selected mold installation
- `LDFLAGS` - Adds `-fuse-ld=mold` once

## ROCm Setup

### Installing Release Versions

```bash
ROCM_GPU_TARGET=gfx1150 ~/.dotfiles/tools/rocm/install.sh
~/.dotfiles/tools/rocm/install.sh 7.14.0a20260612 gfx1150
```

The release installer extracts TheRock's conventional tarball directly into
`~/tools/rocm/<version>/`. It does not install or retain a Python environment.

Set default GPU target in `~/.shrc.local`:
```bash
export ROCM_GPU_TARGET=gfx1150  # Must match this machine's GFX ISA
```

### Development Builds

`therock-setup` owns the direct development selectors:

```bash
therock-setup --symlinks-only

# Equivalent selector targets:
# ~/tools/rocm/debug   -> ~/src/rocm/TheRock/build-debug
# ~/tools/rocm/release -> ~/src/rocm/TheRock/install
```

### Version Selection in .envrc

```bash
use_rocm                 # Latest release
use_rocm "7.14.0a20260612" # Specific version
use_rocm "debug"         # TheRock debug build
use_rocm "release"       # TheRock release build
```

### Per-Project Dev Override

In `.envrc.local` (not committed):
```bash
# Switch to debug build for this project
use_rocm "debug"
```

## TheRock Local Development

### Initial Setup

```bash
therock-setup              # Full setup: clone, build, symlinks
therock-setup --clone-only # Just clone and fetch sources
therock-setup --build-only # Just build (if already cloned)
```

### Rebuilding

```bash
therock-build debug
therock-build release
cmake --install ~/src/rocm/TheRock/build-release
therock-setup --symlinks-only
```

### Using in Projects

```bash
use_rocm "debug"    # TheRock debug build (for development)
use_rocm "release"  # TheRock release install
```

See `~/src/rocm/README.md` for more details on adding release builds.
