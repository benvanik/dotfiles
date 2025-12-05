# Claude Code Configuration

## Files

| File | Purpose |
|------|---------|
| `settings.json` | Global Claude Code settings (sandbox, permissions) |
| `settings-admin.json` | Admin/managed settings |
| `CLAUDE.md` | Global instructions for all projects |
| `statusline.sh` | Status line script |
| `bwrap-gpu-wrapper` | GPU passthrough wrapper for Linux sandbox |

## GPU Passthrough (Linux)

Claude Code's sandbox uses bubblewrap (bwrap) but doesn't pass through GPU devices by default. The `bwrap-gpu-wrapper` script intercepts bwrap calls and injects `--dev-bind-try` flags for GPU access.

### How It Works

1. `~/.local/bin/bwrap` symlinks to this wrapper
2. `~/.local/bin` must be before `/usr/bin` in PATH (it is by default)
3. Wrapper injects GPU device binds **after** `--dev /dev` (ordering matters!)
4. Uses `--dev-bind-try` so missing devices don't cause errors

### Devices Passed Through

| Device | Purpose |
|--------|---------|
| `/dev/dri` | DRM/Vulkan/OpenGL (all GPUs) |
| `/dev/kfd` | AMD ROCm/HIP kernel driver |
| `/dev/nvidia*` | NVIDIA CUDA driver |

### Prerequisites

#### 1. Device Permissions

ROCm requires `/dev/kfd` to be world-accessible:

```bash
# Check current permissions
ls -la /dev/kfd

# If not 0666, create udev rule:
sudo tee /etc/udev/rules.d/70-amdgpu.rules << 'EOF'
SUBSYSTEM=="kfd", KERNEL=="kfd", MODE="0666"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

#### 2. PATH Order

Verify `~/.local/bin` is before `/usr/bin`:

```bash
echo $PATH | tr ':' '\n' | grep -n local
# Should show ~/.local/bin before /usr/bin
```

### Testing

Start a new Claude session and run:

```bash
# GPU visibility
ls -la /dev/dri /dev/kfd

# Vulkan (should show real GPU, not llvmpipe)
vulkaninfo --summary | grep -E "GPU|deviceName"

# ROCm (AMD)
rocminfo | head -20

# NVIDIA
nvidia-smi
```

### Troubleshooting

**GPU not visible in sandbox:**
```bash
# Check wrapper is being used
which bwrap  # Should show ~/.local/bin/bwrap

# Trace actual bwrap invocation
strace -f -e execve claude 2>&1 | grep bwrap
```

**rocminfo permission denied:**
```bash
# Check /dev/kfd permissions
ls -la /dev/kfd  # Should be crw-rw-rw-

# Fix if needed
sudo chmod 666 /dev/kfd
```

**Vulkan shows llvmpipe only:**
- GPU devices not passed through, or
- Mesa can't find GPU (check `/dev/dri/renderD*` exists)
