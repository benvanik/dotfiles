# Linux-specific shell configuration (synced).
# Sourced automatically on Linux systems.

# Snap packages.
_add_path "/snap/bin"

# PyTorch AMD GPU memory allocation (expandable segments for large models).
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True

# Electron apps in Wayland/headless environments.
export ELECTRON_NO_SANDBOX=1
