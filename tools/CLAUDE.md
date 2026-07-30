# Tools Management - Claude Code Reference

## Quick Reference

| Command | Purpose |
|---------|---------|
| `project-init` | Initialize project with .envrc |
| `rocm-install 7.9.0` | Install ROCm version to ~/tools/rocm/ |
| `use_llvm ">=21.0.0"` | In .envrc: require LLVM 21+ |
| `use_rocm "debug"` | In .envrc: use TheRock debug build |

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
- `use_rocm [version]` - Load ROCm (Linux only, silent skip elsewhere)
- `source_local_envrc` - Load .envrc.local overrides

## Adding New Tool Version

1. Download/extract to ~/tools/<tool>/<version>/
2. Update latest symlink: `ln -sfn <version> ~/tools/<tool>/latest`
3. Verify: `ls -la ~/tools/<tool>/`

## Platform Behavior

- ROCm: Linux only, silent skip on macOS/WSL
- Other tools: Error if requested but not found

## Environment Variables Set

LLVM:
- `CC`, `CXX` - Compiler paths
- `LLVM_ROOT`, `LLVM_DIR`, `CLANG_DIR`, `MLIR_DIR` - CMake paths

ROCm:
- `ROCM_HOME`, `HIP_PATH` - ROCm paths
- `CMAKE_PREFIX_PATH` - For TheRock builds

## ROCm Setup

### Installing Release Versions

```bash
rocm-install 7.9.0            # Uses ROCM_GPU_TARGET from .shrc.local
rocm-install 7.9.0 gfx90a     # Override GPU target
```

Set default GPU target in `~/.shrc.local`:
```bash
export ROCM_GPU_TARGET=gfx1100  # RDNA3
```

### Development Builds

Set up symlinks to your TheRock checkout:

```bash
# Debug build (build/ directory)
ln -sfn ~/src/rocm/TheRock/build ~/tools/rocm/debug

# Release build (install/ directory)
ln -sfn ~/src/rocm/TheRock/install ~/tools/rocm/release
```

### Version Selection in .envrc

```bash
use_rocm                 # Latest release
use_rocm "7.9.0"         # Specific version
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
cd ~/src/rocm/TheRock
source .venv/bin/activate
cmake --build build-debug -j$(nproc)    # Rebuild debug
cmake --build build-release -j$(nproc)  # Rebuild release
cmake --install build-release           # Re-install
```

### Using in Projects

```bash
use_rocm "debug"    # TheRock debug build (for development)
use_rocm "release"  # TheRock release install
```

See `~/src/rocm/README.md` for more details on adding release builds.
